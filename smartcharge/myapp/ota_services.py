import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ChargerDevice, ChargingSession, DeviceTelemetry, FirmwareArtifact, OTAJob


OTA_COMMAND_TOPIC = "iot/charge_station/v1/ota/command"
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,31}$")
ACTIVE_CHARGING_STATUSES = ("assigned", "charging")


def _https_base_url():
    base = settings.FIRMWARE_HTTPS_BASE_URL
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValidationError("FIRMWARE_HTTPS_BASE_URL phải là HTTPS URL hợp lệ.")
    return base.rstrip("/")


def _device_key(device_id):
    try:
        mapping = json.loads(settings.OTA_DEVICE_KEYS_JSON)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("OTA_DEVICE_KEYS_JSON không phải JSON hợp lệ.") from exc
    key_hex = mapping.get(device_id, "") if isinstance(mapping, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", key_hex):
        raise ValidationError(f"Thiếu OTA HMAC key hợp lệ cho {device_id}.")
    return bytes.fromhex(key_hex)


def _signature(key, canonical):
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_device_ready_for_ota(device, firmware, now=None):
    now = now or timezone.now()
    if not DEVICE_ID_PATTERN.fullmatch(device.device_id):
        raise ValidationError("Device ID không hợp lệ.")
    if not firmware.is_approved:
        raise ValidationError("Firmware chưa được phê duyệt triển khai.")
    if firmware.firmware_type != FirmwareArtifact.TYPE_MAIN:
        raise ValidationError("Recovery/bootloader/partition table không được OTA qua ứng dụng.")
    if firmware.chip_target.lower() != "esp32":
        raise ValidationError("Firmware target không tương thích thiết bị ESP32.")
    if ChargingSession.objects.filter(
        device=device, status__in=ACTIVE_CHARGING_STATUSES
    ).exists():
        raise ValidationError("Thiết bị đang có phiên sạc active.")
    if OTAJob.objects.filter(device=device).exclude(
        status__in=OTAJob.TERMINAL_STATUSES
    ).exists():
        raise ValidationError("Thiết bị đã có OTAJob chưa kết thúc.")

    latest = DeviceTelemetry.objects.filter(device=device).order_by("-received_at").first()
    if latest is None:
        raise ValidationError("Chưa có telemetry để xác định thiết bị đang rảnh.")
    stale_before = now - timedelta(seconds=settings.DEVICE_STALE_SECONDS)
    if latest.received_at < stale_before or not device.last_seen_at or device.last_seen_at < stale_before:
        raise ValidationError("Thiết bị OFFLINE/telemetry quá cũ.")
    if latest.charging or latest.charging_status not in {"IDLE", "DONE"}:
        raise ValidationError("Trạng thái thiết bị đang sạc hoặc không xác định.")

    _https_base_url()
    _device_key(device.device_id)
    if not settings.MQTT_USERNAME or not settings.MQTT_PASSWORD:
        raise ValidationError("MQTT OTA yêu cầu username/password; broker công cộng chưa xác thực bị chặn.")


def create_and_issue_ota(device, firmware, user):
    if not user or not user.has_perm("myapp.deploy_firmware"):
        raise PermissionDenied("Tài khoản không có quyền deploy firmware.")

    now = timezone.now()
    validate_device_ready_for_ota(device, firmware, now)
    base_url = _https_base_url()
    key = _device_key(device.device_id)
    raw_token = secrets.token_urlsafe(32)
    request_id = f"ota-{secrets.token_hex(16)}"

    with transaction.atomic():
        locked_device = ChargerDevice.objects.select_for_update().get(pk=device.pk)
        validate_device_ready_for_ota(locked_device, firmware, now)
        locked_device.ota_sequence += 1
        locked_device.save(update_fields=["ota_sequence"])
        job = OTAJob.objects.create(
            request_id=request_id,
            device=locked_device,
            firmware=firmware,
            sequence=locked_device.ota_sequence,
            download_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            download_expires_at=now
            + timedelta(seconds=settings.OTA_DOWNLOAD_TTL_SECONDS),
            created_by=user,
        )

    manifest_url = f"{base_url}/firmware/manifest/{job.request_id}/{raw_token}/"
    canonical = (
        f"SC-CMD-1\n{device.device_id}\nSTART_OTA\n{job.request_id}\n"
        f"{firmware.version}\n{manifest_url}\n{job.sequence}"
    )
    command = {
        "device": device.device_id,
        "command": "START_OTA",
        "request_id": job.request_id,
        "version": firmware.version,
        "manifest_url": manifest_url,
        "seq": str(job.sequence),
        "signature": _signature(key, canonical),
    }
    try:
        mqtt_publish.single(
            OTA_COMMAND_TOPIC,
            json.dumps(command, separators=(",", ":")),
            qos=1,
            hostname=settings.MQTT_BROKER,
            port=settings.MQTT_PORT,
            auth={"username": settings.MQTT_USERNAME, "password": settings.MQTT_PASSWORD},
            tls={},
        )
    except Exception as exc:
        OTAJob.objects.filter(pk=job.pk).update(
            status=OTAJob.STATUS_FAILED,
            error_message=f"MQTT publish failed: {str(exc)[:400]}",
            last_status_at=timezone.now(),
        )
        job.refresh_from_db()
        return job, None

    job.command_sent_at = timezone.now()
    job.save(update_fields=["command_sent_at", "updated_at"])
    return job, command


def authorize_job_token(request_id, raw_token):
    try:
        job = OTAJob.objects.select_related("device", "firmware").get(
            request_id=request_id
        )
    except OTAJob.DoesNotExist as exc:
        raise PermissionDenied("Invalid OTA capability.") from exc
    supplied = hashlib.sha256(raw_token.encode()).hexdigest()
    if not hmac.compare_digest(supplied, job.download_token_hash):
        raise PermissionDenied("Invalid OTA capability.")
    if timezone.now() >= job.download_expires_at or job.is_terminal:
        raise PermissionDenied("OTA capability expired.")
    if not job.command_sent_at or not job.firmware.is_approved:
        raise PermissionDenied("OTA job is not downloadable.")
    return job


def build_manifest(job, raw_token):
    base_url = _https_base_url()
    key = _device_key(job.device.device_id)
    download_url = f"{base_url}/firmware/download/{job.request_id}/{raw_token}/"
    canonical = (
        f"SC-MANIFEST-1\n{job.device.device_id}\n{job.request_id}\n"
        f"{job.firmware.version}\n{download_url}\n{job.firmware.file_size}\n"
        f"{job.firmware.sha256}\nesp32\n{job.sequence}"
    )
    return {
        "device": job.device.device_id,
        "request_id": job.request_id,
        "version": job.firmware.version,
        "url": download_url,
        "size": job.firmware.file_size,
        "sha256": job.firmware.sha256,
        "chip": "esp32",
        "seq": str(job.sequence),
        "signature": _signature(key, canonical),
    }


ALLOWED_TRANSITIONS = {
    OTAJob.STATUS_PENDING: {
        OTAJob.STATUS_ACCEPTED, OTAJob.STATUS_REJECTED_BUSY,
        OTAJob.STATUS_REBOOTING, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_ACCEPTED: {
        OTAJob.STATUS_REBOOTING, OTAJob.STATUS_DOWNLOADING, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_REBOOTING: {
        OTAJob.STATUS_DOWNLOADING, OTAJob.STATUS_RECOVERING,
        OTAJob.STATUS_SUCCESS, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_RECOVERING: {
        OTAJob.STATUS_DOWNLOADING, OTAJob.STATUS_VERIFYING,
        OTAJob.STATUS_INSTALLING, OTAJob.STATUS_REBOOTING, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_DOWNLOADING: {
        OTAJob.STATUS_DOWNLOADING, OTAJob.STATUS_VERIFYING, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_VERIFYING: {
        OTAJob.STATUS_INSTALLING, OTAJob.STATUS_REBOOTING, OTAJob.STATUS_FAILED,
    },
    OTAJob.STATUS_INSTALLING: {OTAJob.STATUS_REBOOTING, OTAJob.STATUS_FAILED},
}


def process_ota_status(data):
    required = {"device", "request_id", "status", "progress", "version"}
    if not isinstance(data, dict) or not required.issubset(data):
        raise ValidationError("OTA status thiếu trường bắt buộc.")
    device_id = data["device"]
    request_id = data["request_id"]
    status = data["status"]
    version = data["version"]
    progress = data["progress"]
    if not DEVICE_ID_PATTERN.fullmatch(str(device_id)):
        raise ValidationError("Device ID không hợp lệ.")
    if status not in dict(OTAJob.STATUS_CHOICES):
        raise ValidationError("OTA status không hợp lệ.")
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise ValidationError("OTA progress không hợp lệ.")

    with transaction.atomic():
        try:
            job = (
                OTAJob.objects.select_for_update()
                .select_related("device", "firmware")
                .get(request_id=request_id, device__device_id=device_id)
            )
        except OTAJob.DoesNotExist as exc:
            raise ValidationError("request_id không thuộc device này.") from exc
        if version != job.firmware.version:
            raise ValidationError("Version báo về không khớp OTAJob.")
        if job.is_terminal:
            if job.status == status and job.progress == progress:
                return job, False
            raise ValidationError("OTAJob đã kết thúc; bỏ qua status đến muộn.")
        if status == job.status:
            if status == OTAJob.STATUS_DOWNLOADING and progress < job.progress:
                raise ValidationError("OTA progress đi lùi.")
            if progress == job.progress:
                return job, False
        # SUCCESS có thể là message đầu tiên backend nhận được sau reconnect;
        # firmware chỉ phát nó sau boot + health checks và version vẫn phải khớp.
        elif status != OTAJob.STATUS_SUCCESS and status not in ALLOWED_TRANSITIONS.get(job.status, set()):
            raise ValidationError(f"OTA status sai thứ tự: {job.status} -> {status}.")
        if status == OTAJob.STATUS_SUCCESS and version != job.firmware.version:
            raise ValidationError("Không xác nhận SUCCESS khi version chưa đúng.")

        now = timezone.now()
        job.status = status
        job.progress = progress
        job.reported_version = version
        job.last_status_at = now
        job.error_message = str(data.get("error", ""))[:500]
        job.save(
            update_fields=[
                "status", "progress", "reported_version", "last_status_at",
                "error_message", "updated_at",
            ]
        )
        ChargerDevice.objects.filter(pk=job.device_id).update(
            last_seen_at=now,
            **({"firmware_version": version} if status == OTAJob.STATUS_SUCCESS else {}),
        )
        return job, True


def expire_stale_ota_jobs(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.OTA_STATUS_TIMEOUT_SECONDS)
    return OTAJob.objects.filter(
        status__in=[
            OTAJob.STATUS_PENDING, OTAJob.STATUS_ACCEPTED, OTAJob.STATUS_DOWNLOADING,
            OTAJob.STATUS_VERIFYING, OTAJob.STATUS_INSTALLING,
            OTAJob.STATUS_REBOOTING, OTAJob.STATUS_RECOVERING,
        ]
    ).filter(
        Q(last_status_at__lt=cutoff)
        | Q(last_status_at__isnull=True, command_sent_at__lt=cutoff)
    ).update(
        status=OTAJob.STATUS_FAILED,
        error_message="Timeout: thiết bị không báo trạng thái OTA.",
        last_status_at=now,
    )
