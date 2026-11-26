import json
import logging
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
from django.utils.dateparse import parse_datetime
from django.db.models import Q


from .models import (
    ChargingSession,
    ChargerDevice,
    ChargerPort,
    DeviceLog,
    DeviceTelemetry,
    MqttAttempt,
    OTAJob,
    PaymentOrder,
)
from .ota_services import authorize_job_token, build_manifest
from .mqtt_config import mqtt_connection, publish_once

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

        selected_port = PaymentOrder.objects.filter(session=session).values("port_id")
        if selected_port.exists():
            ports = ports.filter(pk__in=selected_port)
        for port in ports:
            # OTA preparation takes the same device lock before assessing idle state.
            device = ChargerDevice.objects.select_for_update().get(pk=port.device_id)
            if OTAJob.objects.filter(device=device).exclude(status__in=OTAJob.TERMINAL_STATUSES).exists():
                continue
            has_active = ChargingSession.objects.filter(
                device=device,
                port_no=port.port_no,
                status__in=ACTIVE_STATUSES,
            ).exists()

            if not has_active:
                session.device = device
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
    connection = mqtt_connection()
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
        publish_once(
            MQTT_TOPIC_ORDER,
            payload,
            qos=1,
            retain=False,
            **connection,
        )
    except (OSError, ValueError):
        # A lost PUBACK is ambiguous. Firmware 1.0 cannot deduplicate START.
        ChargingSession.objects.filter(pk=session.pk, mqtt_start_sent_at=claimed_at).update(
            mqtt_start_delivery_unknown=True,
        )
        raise

    return True


# ====================== PHẦN THANH TOÁN SEPAY ======================

SEPAY_API_TOKEN = os.environ.get("SEPAY_API_TOKEN", "")
SEPAY_API_URL = "https://my.sepay.vn/userapi/transactions/list"


def payment_view(request):
    if settings.MQTT_PUBLIC_TEST_MODE:
        return redirect("myapp:devices_dashboard")
    if settings.SEPAY_WEBHOOK_API_KEY:
        return redirect("myapp:payment_order")
    return render(request, "myapp/base.html")


def payment_baseline(request):
    """
    Lấy 'mốc' giao dịch mới nhất hiện có cho số tiền đó.
    Frontend sẽ dùng baseline_id này để chỉ check giao dịch mới hơn.
    """
    if settings.MQTT_PUBLIC_TEST_MODE or settings.SEPAY_WEBHOOK_API_KEY:
        return JsonResponse({"error": "Amount-only payment polling is disabled."}, status=403)
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
    if settings.MQTT_PUBLIC_TEST_MODE or settings.SEPAY_WEBHOOK_API_KEY:
        return JsonResponse({"error": "Amount-only payment polling is disabled."}, status=403)
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
            except Exception:
                # Session vẫn tồn tại và lần polling sau có thể retry START an toàn.
                logging.getLogger(__name__).warning("MQTT START publish failed for session %s", session.id)
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
    health = device.mqtt_health
    if health.get("mode") in {"HEADLESS", "RECOVERY"}:
        telemetry = None
    activity = device.last_heartbeat_at or device.last_seen_at
    stale = (
        not activity
        or activity
        < now - timedelta(seconds=settings.DEVICE_STALE_SECONDS)
    )
    return {
        "device_id": device.logical_id,
        "routing_id": device.device_id,
        "display_label": device.display_label,
        "rx_count": device.mqtt_success_count + device.mqtt_failed_count + device.mqtt_duplicate_count,
        "name": device.name,
        "online": not stale,
        "firmware_version": (
            (health.get("firmware_version") if health.get("mode") == "RECOVERY" else device.running_version)
            or (telemetry.firmware_version if telemetry else "") or "—"
        ),
        "last_heartbeat": device.last_heartbeat_at,
        "minimum_heap": health.get("min_free_heap"),
        "uptime_ms": health.get("uptime_ms"),
        "reset_reason": health.get("reset_reason"),
        "relay_on": health.get("relay_on"),
        "sensor_status": health.get("sensor_status"),
        "display_status": health.get("display_status"),
        "mode": health.get("mode"),
        "success_count": device.mqtt_success_count,
        "failed_count": device.mqtt_failed_count,
        "duplicate_count": device.mqtt_duplicate_count,
        "last_message_id": device.mqtt_last_message_id,
        "last_error": device.mqtt_last_error,
        "port": telemetry.port if telemetry else None,
        "voltage_v": telemetry.voltage_v if telemetry else None,
        "current_a": telemetry.current_a if telemetry else None,
        "power_w": telemetry.power_w if telemetry else None,
        "energy_total_wh": telemetry.energy_total_wh if telemetry else None,
        "energy_delta_wh": telemetry.energy_delta_wh if telemetry else None,
        "charging_status": telemetry.charging_status if telemetry else "UNKNOWN",
        "heap_free_bytes": health.get("free_heap", telemetry.heap_free_bytes if telemetry else None),
        "rssi_dbm": health.get("rssi", telemetry.rssi_dbm if telemetry else None),
        "reported_firmware_version": telemetry.firmware_version if telemetry else "",
        "last_update": device.last_seen_at,
    }


@_staff_permission("myapp.view_devicetelemetry")
def devices_dashboard(request):
    now = timezone.now()
    devices = [_device_snapshot(device, now) for device in ChargerDevice.objects.all()]
    return render(request, "myapp/device_dashboard.html", {"devices": devices})


LOG_CATEGORIES = ("BOOT", "WIFI", "MQTT", "HEARTBEAT", "TELEMETRY", "ACK", "ERROR", "OTA")
LOG_EVENT_PREFIXES = {
    "BOOT": ("BOOT", "HEALTHY", "REBOOT", "WATCHDOG"),
    "WIFI": ("WIFI",),
    "MQTT": ("MQTT",),
    "OTA": ("OTA", "RECOVER"),
}


def _event_prefixes(field, prefixes):
    condition = Q()
    for prefix in prefixes:
        condition |= Q(**{f"{field}__startswith": prefix})
    return condition


@require_GET
@_staff_permission("myapp.view_devicelog")
def device_logs_dashboard(request):
    selected_device = request.GET.get("device", "")
    category = request.GET.get("category", "")
    kind = request.GET.get("type", "")
    outcome = request.GET.get("outcome", "")
    view = request.GET.get("view", "events" if "filter" in request.GET else "mqtt")
    attempts = MqttAttempt.objects.filter(received_at__gte=timezone.now() - timedelta(days=7))
    events = DeviceLog.objects.select_related("device")
    if selected_device:
        attempts = attempts.filter(device_id=selected_device)
        events = events.filter(device__device_id=selected_device)
    if kind in {"heartbeat", "telemetry", "logs", "status", "ota"}:
        attempts = attempts.filter(message_type=kind)
        if kind != "logs":
            events = events.none()
    if outcome in {"SUCCESS", "FAILED", "DUPLICATE"}:
        attempts = attempts.filter(status=outcome)
        if outcome != "SUCCESS":
            events = events.none()

    prefixes = LOG_EVENT_PREFIXES.get(category)
    if prefixes:
        attempt_filter = _event_prefixes("payload__event", prefixes)
        if category == "OTA":
            attempt_filter |= Q(message_type="ota")
        attempts = attempts.filter(attempt_filter)
        events = events.filter(_event_prefixes("event", prefixes))
    elif category in {"HEARTBEAT", "TELEMETRY"}:
        attempts = attempts.filter(message_type=category.lower())
        events = events.none()
    elif category == "ACK":
        attempts = attempts.exclude(ack_status="NOT_SENT")
        events = events.none()
    elif category == "ERROR":
        attempts = attempts.filter(
            Q(status="FAILED") | Q(payload__level="ERROR")
            | Q(ack_status__in=("PUBLISH_FAILED", "BROKER_REJECTED"))
        )
        events = events.filter(level="ERROR")

    dates = {}
    for key, lookup in (("since", "gte"), ("until", "lte")):
        raw = request.GET.get(key, "")[:40]
        dates[key] = raw
        try:
            parsed = parse_datetime(raw)
            if parsed and timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
            if parsed:
                parsed = parsed.astimezone(timezone.get_default_timezone())
        except (ValueError, OverflowError):
            parsed = None
        if parsed:
            date_filter = {f"received_at__{lookup}": parsed}
            attempts = attempts.filter(**date_filter)
            events = events.filter(**date_filter)
            dates[key] = parsed.isoformat(timespec="seconds")[:19]

    legacy = view == "events"
    if legacy:
        level = request.GET.get("filter", "").upper()
        if level in {"INFO", "WARNING", "ERROR"}:
            events = events.filter(level=level)
        elif level == "OTA":
            events = events.filter(_event_prefixes("event", LOG_EVENT_PREFIXES["OTA"]))
        elif level == "CHARGING":
            events = events.filter(event__in=(
                "PAYMENT_RECEIVED", "CHARGING_STARTED", "CHARGE_COMPLETED", "RELAY_ON", "RELAY_OFF",
            ))
    page = Paginator((events if legacy else attempts)[:10000], 50).get_page(
        request.GET.get("page") or request.GET.get("mqtt_page")
    )
    query = request.GET.copy()
    for key in ("page", "mqtt_page", "view"):
        query.pop(key, None)
    return render(request, "myapp/device_logs.html", {
        "page": page, "legacy": legacy, "query": query.urlencode(),
        "category": category, "categories": LOG_CATEGORIES,
        "mqtt_type": kind, "mqtt_outcome": outcome,
        "outcomes": ["SUCCESS", "FAILED", "DUPLICATE"],
        "devices": ChargerDevice.objects.order_by("device_id"),
        "selected_device": selected_device, **dates,
    })


@require_GET
@_staff_permission("myapp.view_devicetelemetry")
def device_status_api(request):
    now = timezone.now()
    payload = []
    for device in ChargerDevice.objects.all():
        item = _device_snapshot(device, now)
        for key in ("last_update", "last_heartbeat"):
            local = timezone.localtime(item[key]) if item[key] else None
            item[key] = local.isoformat() if local else None
            item[f"{key}_display"] = local.strftime("%d/%m/%Y %H:%M:%S") if local else None
        payload.append(item)
    return JsonResponse({"devices": payload, "server_time": timezone.localtime(now).isoformat()})


@require_GET
@_staff_permission("myapp.view_devicelog")
def recent_logs_api(request):
    queryset = DeviceLog.objects.select_related("device")
    device_id = request.GET.get("device")
    if device_id:
        queryset = queryset.filter(device__device_id=device_id)
    logs = [
        {
            "device": log.device.logical_id,
            "routing_id": log.device.device_id,
            "level": log.level,
            "event": log.event,
            "message": log.message,
            "uptime_ms": log.uptime_ms,
            "received_at": timezone.localtime(log.received_at).isoformat(),
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
