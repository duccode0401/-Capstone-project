import json
import requests
import os
import time
import re
from datetime import timedelta
from dotenv import load_dotenv
from django.conf import settings

load_dotenv(settings.BASE_DIR / ".env")

from datetime import timedelta

from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.contrib.auth.decorators import permission_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.contrib.admin.views.decorators import staff_member_required
from django.db import OperationalError, transaction
from django.utils import timezone

import paho.mqtt.publish as mqtt_publish  # <--- THÊM DÒNG NÀY

from .models import (
    ChargingSession,
    ChargerDevice,
    ChargerPort,
    DeviceLog,
    DeviceTelemetry,
    OTAJob,
)
from .ota_services import authorize_job_token, build_manifest
from .mqtt_config import mqtt_connection

# ====================== MQTT CONFIG ======================
MQTT_TOPIC_ORDER = "iot/charge_station/v1/orders"

# ====================== DASHBOARD CHO ADMIN ======================

@staff_member_required  # chỉ user is_staff=True mới vào được
def session_dashboard(request):
    sessions = ChargingSession.objects.order_by('-created_at')[:50]
    return render(request, "myapp/session_dashboard.html", {"sessions": sessions})


# ====================== API CHO ESP32 TEST =======================

@csrf_exempt
def api_report_port(request):
    """
    ESP32 gửi POST:
    {
      "device_id": "ESP32_0001",
      "port_no": 1
    }
    => Tự tạo ChargerDevice + ChargerPort nếu chưa có.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    device_id = data.get("device_id")
    port_no = data.get("port_no")

    if not device_id or port_no is None:
        return JsonResponse({"ok": False, "error": "missing_device_or_port"}, status=400)

    device, _ = ChargerDevice.objects.get_or_create(device_id=device_id)
    port, created = ChargerPort.objects.get_or_create(device=device, port_no=port_no)

    return JsonResponse(
        {
            "ok": True,
            "device_id": device.device_id,
            "port_no": port.port_no,
            "created": created,  # True nếu vừa tạo mới cổng, False nếu đã tồn tại
        }
    )


# ============ HELPER: GÁN SESSION VÀO TRẠM/CỔNG TRỐNG ============

ACTIVE_STATUSES = ["assigned", "charging"]


def assign_session_to_free_slot(session: ChargingSession):
    """
    Tìm (device, port) đang TRỐNG, rồi gán session này vào.
    Trả về session đã gán, hoặc None nếu hết chỗ.
    """
    with transaction.atomic():
        session = (
            ChargingSession.objects.select_for_update()
            .select_related("device")
            .get(pk=session.pk)
        )

        if session.status in ACTIVE_STATUSES:
            return session
        if session.status == "done":
            return None

        ports = (
            ChargerPort.objects
            .select_for_update()
            .filter(is_active=True)
            .exclude(device_id__in=OTAJob.objects.exclude(
                status__in=OTAJob.TERMINAL_STATUSES
            ).values("device_id"))
            .order_by("device__device_id", "port_no")
        )

        for port in ports:
            has_active = ChargingSession.objects.filter(
                device=port.device,
                port_no=port.port_no,
                status__in=ACTIVE_STATUSES,
            ).exists()

            if not has_active:
                session.device = port.device
                session.port_no = port.port_no
                session.status = "assigned"
                session.save(update_fields=["device", "port_no", "status", "updated_at"])
                return session

        # không slot nào trống
        session.status = "waiting"
        session.device = None
        session.port_no = None
        session.save(update_fields=["device", "port_no", "status", "updated_at"])
        return None


def create_session_after_payment(amount_vnd: int, sepay_transaction_id):
    """
    Khi thanh toán thành công, gọi hàm này.
    Nó tạo ChargingSession + auto gán vào slot trống (nếu có).
    """
    transaction_id = str(sepay_transaction_id).strip()
    if not transaction_id:
        raise ValueError("SePay transaction ID is required")

    # Unique constraint là lớp bảo vệ cuối cùng nếu hai request đồng thời cùng xử lý.
    # SQLite khóa cả table khi ghi, nên retry ngắn thay vì trả 500 cho polling còn lại.
    for attempt in range(5):
        try:
            with transaction.atomic():
                session, _created = ChargingSession.objects.get_or_create(
                    sepay_transaction_id=transaction_id,
                    defaults={"amount_vnd": amount_vnd, "status": "paid"},
                )
            break
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))

    for attempt in range(5):
        try:
            assigned = assign_session_to_free_slot(session)
            break
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    if assigned is None:
        session.refresh_from_db()
    else:
        session = assigned
    return session


def publish_start_once(session: ChargingSession):
    """Gửi START tối đa một lần sau khi session đã được gán hợp lệ."""
    if settings.MQTT_PUBLIC_TEST_MODE:
        return False
    with transaction.atomic():
        session = (
            ChargingSession.objects.select_related("device").get(pk=session.pk)
        )
        if session.mqtt_start_sent_at is not None:
            return False
        if (
            session.status not in ACTIVE_STATUSES
            or session.device_id is None
            or session.port_no is None
        ):
            return False

        # Compare-and-set này chặn hai worker cùng claim một lệnh START, kể cả
        # trên SQLite nơi select_for_update() không khóa row như PostgreSQL.
        claimed_at = timezone.now()
        claimed = ChargingSession.objects.filter(
            pk=session.pk, mqtt_start_sent_at__isnull=True
        ).update(mqtt_start_sent_at=claimed_at, updated_at=claimed_at)
        if claimed != 1:
            return False

    payload = f"{session.device.device_id} {session.port_no} {session.amount_vnd}"
    try:
        mqtt_publish.single(
            MQTT_TOPIC_ORDER,
            payload,
            qos=1,
            **mqtt_connection(),
        )
    except Exception:
        # Cho phép retry nếu broker xác nhận publish thất bại. Nếu process chết
        # giữa publish và phản hồi thì giao thức hiện tại không thể đảm bảo exactly-once.
        ChargingSession.objects.filter(
            pk=session.pk, mqtt_start_sent_at=claimed_at
        ).update(mqtt_start_sent_at=None)
        raise

    return True


# ====================== PHẦN THANH TOÁN SEPAY ======================

SEPAY_API_TOKEN = os.environ.get("SEPAY_API_TOKEN", "")
SEPAY_API_URL = "https://my.sepay.vn/userapi/transactions/list"


def payment_view(request):
    if settings.MQTT_PUBLIC_TEST_MODE:
        return redirect("myapp:devices_dashboard")
    return render(request, "myapp/base.html")


def payment_baseline(request):
    if settings.MQTT_PUBLIC_TEST_MODE:
        return JsonResponse({"error": "Payment is disabled in public test mode."}, status=403)
    """
    Lấy 'mốc' giao dịch mới nhất hiện có cho số tiền đó.
    Frontend sẽ dùng baseline_id này để chỉ check giao dịch mới hơn.
    """
    try:
        amount = float(request.GET.get("amount", "0"))
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_amount"}, status=400)

    headers = {
        "Authorization": f"Bearer {SEPAY_API_TOKEN}",
        "Content-Type": "application/json",
    }
    params = {
        "amount_in": amount,
        "limit": 1,  # giao dịch mới nhất với số tiền này
    }

    try:
        resp = requests.get(SEPAY_API_URL, headers=headers, params=params, timeout=8)
        data = resp.json()
    except Exception as e:
        print("SePay baseline error:", e)
        return JsonResponse({"ok": False, "error": "sepay_request_failed"}, status=500)

    txs = data.get("transactions", [])
    baseline_id = None

    if txs:
        try:
            baseline_id = int(txs[0]["id"])
        except Exception:
            baseline_id = None

    return JsonResponse({"ok": True, "baseline_id": baseline_id})


def check_payment(request):
    """
    /api/check-payment/?amount=20000&baseline_id=49682

    Trả về:
    {
        ok: True,
        paid: True/False,
        count: n,
        session: {
            id, device_id, port_no, status
        } hoặc null
    }
    """
    if settings.MQTT_PUBLIC_TEST_MODE:
        return JsonResponse({"error": "Payment is disabled in public test mode."}, status=403)
    try:
        # luôn ép về int VND cho chắc
        amount = int(float(request.GET.get("amount", "0")))
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_amount"}, status=400)

    baseline_id_raw = request.GET.get("baseline_id")
    since_id = None
    if baseline_id_raw:
        try:
            since_id = int(baseline_id_raw) + 1  # > baseline
        except ValueError:
            since_id = None

    headers = {
        "Authorization": f"Bearer {SEPAY_API_TOKEN}",
        "Content-Type": "application/json",
    }
    params = {
        "amount_in": amount,
        "limit": 5,
    }
    if since_id is not None:
        params["since_id"] = since_id

    try:
        resp = requests.get(SEPAY_API_URL, headers=headers, params=params, timeout=8)
        data = resp.json()
    except Exception as e:
        print("SePay check_payment error:", e)
        return JsonResponse({"ok": False, "error": "sepay_request_failed"}, status=500)

    txs = data.get("transactions", [])
    paid = len(txs) > 0

    session_info = None

    if paid:
        processed_sessions = []
        for sepay_transaction in txs:
            transaction_id = sepay_transaction.get("id")
            if transaction_id is None:
                print("SePay transaction is missing id; skipped")
                continue

            session = create_session_after_payment(amount, transaction_id)
            try:
                publish_start_once(session)
            except Exception as e:
                # Session vẫn tồn tại và lần polling sau có thể retry START an toàn.
                print(f"MQTT START publish failed for session {session.id}: {e}")
            processed_sessions.append(session)

        if not processed_sessions:
            return JsonResponse(
                {"ok": False, "error": "sepay_transaction_missing_id"}, status=502
            )

        session = processed_sessions[0]
        session.refresh_from_db()
        session_info = {
            "id": session.id,
            "device_id": session.device.device_id if session.device else None,
            "port_no": session.port_no,
            "status": session.status,
        }
    return JsonResponse(
        {
            "ok": True,
            "paid": paid,
            "count": len(txs),
            "session": session_info,
        }
    )


def _staff_permission(permission):
    def decorator(view):
        return staff_member_required(permission_required(permission, raise_exception=True)(view))
    return decorator


def _device_snapshot(device, now):
    telemetry = (
        DeviceTelemetry.objects.filter(device=device)
        .order_by("-received_at")
        .first()
    )
    stale = (
        not device.last_seen_at
        or device.last_seen_at
        < now - timedelta(seconds=settings.DEVICE_STALE_SECONDS)
    )
    return {
        "device_id": device.device_id,
        "name": device.name,
        "online": not stale,
        "firmware_version": device.firmware_version or "—",
        "port": telemetry.port if telemetry else None,
        "voltage_v": telemetry.voltage_v if telemetry else None,
        "current_a": telemetry.current_a if telemetry else None,
        "power_w": telemetry.power_w if telemetry else None,
        "energy_total_wh": telemetry.energy_total_wh if telemetry else None,
        "energy_delta_wh": telemetry.energy_delta_wh if telemetry else None,
        "charging_status": telemetry.charging_status if telemetry else "UNKNOWN",
        "last_update": device.last_seen_at,
    }


@_staff_permission("myapp.view_devicetelemetry")
def devices_dashboard(request):
    now = timezone.now()
    devices = [_device_snapshot(device, now) for device in ChargerDevice.objects.all()]
    return render(request, "myapp/device_dashboard.html", {"devices": devices})


@_staff_permission("myapp.view_devicelog")
def device_logs_dashboard(request):
    queryset = DeviceLog.objects.select_related("device")
    selected_device = request.GET.get("device", "")
    selected_filter = request.GET.get("filter", "ALL").upper()
    if selected_device:
        queryset = queryset.filter(device__device_id=selected_device)
    if selected_filter in {"INFO", "WARNING", "ERROR"}:
        queryset = queryset.filter(level=selected_filter)
    elif selected_filter == "OTA":
        queryset = queryset.filter(event__startswith="OTA_")
    elif selected_filter == "CHARGING":
        queryset = queryset.filter(
            event__in=[
                "PAYMENT_RECEIVED", "CHARGING_STARTED", "CHARGE_COMPLETED",
                "RELAY_ON", "RELAY_OFF",
            ]
        )
    elif selected_filter != "ALL":
        selected_filter = "ALL"
    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "myapp/device_logs.html",
        {
            "page": page,
            "devices": ChargerDevice.objects.order_by("device_id"),
            "selected_device": selected_device,
            "selected_filter": selected_filter,
            "filters": ["ALL", "INFO", "WARNING", "ERROR", "OTA", "CHARGING"],
        },
    )


@require_GET
@_staff_permission("myapp.view_devicetelemetry")
def device_status_api(request):
    now = timezone.now()
    payload = []
    for device in ChargerDevice.objects.all():
        item = _device_snapshot(device, now)
        item["last_update"] = (
            item["last_update"].isoformat() if item["last_update"] else None
        )
        payload.append(item)
    return JsonResponse({"devices": payload, "server_time": now.isoformat()})


@require_GET
@_staff_permission("myapp.view_devicelog")
def recent_logs_api(request):
    queryset = DeviceLog.objects.select_related("device")
    device_id = request.GET.get("device")
    if device_id:
        queryset = queryset.filter(device__device_id=device_id)
    logs = [
        {
            "device": log.device.device_id,
            "level": log.level,
            "event": log.event,
            "message": log.message,
            "uptime_ms": log.uptime_ms,
            "received_at": log.received_at.isoformat(),
        }
        for log in queryset[:100]
    ]
    return JsonResponse({"logs": logs})


@require_GET
def firmware_manifest(request, request_id, token):
    job = authorize_job_token(request_id, token)
    response = JsonResponse(build_manifest(job, token), json_dumps_params={"separators": (",", ":")})
    response["Content-Length"] = str(len(response.content))
    response["Cache-Control"] = "private, no-store, no-transform"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _range_iterator(file_handle, start, length, block_size=64 * 1024):
    try:
        file_handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = file_handle.read(min(block_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file_handle.close()


@require_GET
def firmware_download(request, request_id, token):
    job = authorize_job_token(request_id, token)
    firmware = job.firmware
    try:
        if firmware.file.size != firmware.file_size:
            raise Http404("Firmware unavailable.")
        file_handle = firmware.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("Firmware unavailable.") from exc
    range_header = request.headers.get("Range", "")
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
    if range_header and not match:
        file_handle.close()
        return StreamingHttpResponse(status=416)
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else firmware.file_size - 1
        if start >= firmware.file_size or end < start:
            file_handle.close()
            response = StreamingHttpResponse(status=416)
            response["Content-Range"] = f"bytes */{firmware.file_size}"
            return response
        end = min(end, firmware.file_size - 1)
        length = end - start + 1
        response = StreamingHttpResponse(
            _range_iterator(file_handle, start, length),
            status=206,
            content_type="application/octet-stream",
        )
        response["Content-Range"] = f"bytes {start}-{end}/{firmware.file_size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(file_handle, content_type="application/octet-stream")
        response["Content-Length"] = str(firmware.file_size)
    response["Content-Disposition"] = (
        f'attachment; filename="smartcharge-{firmware.version}.bin"'
    )
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store, no-transform"
    response["X-Content-Type-Options"] = "nosniff"
    return response
