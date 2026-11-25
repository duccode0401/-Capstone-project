import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from time import monotonic

import requests
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .mqtt_config import mqtt_connection, publish_once
from .firmware_validation import MAIN_SLOT_SIZE, version_parts
from .models import ChargerDevice, ChargingSession, DeviceTelemetry, FirmwareArtifact, OTAJob


OTA_COMMAND_TOPIC = "iot/charge_station/v1/ota/command"
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,31}$")
ACTIVE_CHARGING_STATUSES = ("assigned", "charging")


def _https_base_url():
    base = settings.FIRMWARE_HTTPS_BASE_URL
    try:
        parsed = urlsplit(base)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", parsed.netloc)
            and (parsed.port is None or parsed.port > 0)
            and not parsed.path
            and ".." not in base[8:]
            and not re.search(r"[^A-Za-z0-9.:/_-]", base)
            and len(base) + len("/firmware/download/") + 36 + 1 + 43 + 1 < 384
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("FIRMWARE_HTTPS_BASE_URL must match a valid ESP32 HTTPS trust base.")
    return base.rstrip("/")


def _device_key(device_id):
    try:
        mapping = json.loads(settings.OTA_DEVICE_KEYS_JSON)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("OTA_DEVICE_KEYS_JSON không phải JSON hợp lệ.") from exc
    key_hex = mapping.get(device_id, "") if isinstance(mapping, dict) else ""
    if not isinstance(key_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", key_hex):
        raise ValidationError(f"Thiếu OTA HMAC key hợp lệ cho {device_id}.")
    return bytes.fromhex(key_hex)


def _signature(key, canonical):
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_device_ready_for_ota(device, firmware, now=None, *, issuing_job=None):
    if settings.MQTT_PUBLIC_TEST_MODE:
        raise ValidationError("OTA is disabled in public test mode.")
    now = now or timezone.now()
    if not DEVICE_ID_PATTERN.fullmatch(device.device_id):
        raise ValidationError("Device ID không hợp lệ.")
    target_version = version_parts(firmware.version)
    current_version = version_parts(device.firmware_version)
    if not target_version or (current_version and target_version <= current_version):
        raise ValidationError("Firmware version must be X.Y.Z and newer than the known device version.")
    if issuing_job is None and device.ota_sequence >= 2**32 - 1:
        raise ValidationError("ESP32 OTA sequence exhausted.")
    if not firmware.is_approved:
        raise ValidationError("Firmware chưa được phê duyệt triển khai.")
    if firmware.firmware_type != FirmwareArtifact.TYPE_MAIN:
        raise ValidationError("Recovery/bootloader/partition table không được OTA qua ứng dụng.")
    if firmware.chip_target.lower() != "esp32":
        raise ValidationError("Firmware target không tương thích thiết bị ESP32.")
    if not 288 < firmware.file_size < MAIN_SLOT_SIZE or not re.fullmatch(r"[0-9a-f]{64}", firmware.sha256):
        raise ValidationError("Invalid firmware size or SHA-256.")
    if not firmware.file.storage.exists(firmware.file.name):
        raise ValidationError("The private firmware file is missing.")
    if ChargingSession.objects.filter(
        device=device, status__in=ACTIVE_CHARGING_STATUSES
    ).exists():
        raise ValidationError("Thiết bị đang có phiên sạc active.")
    if OTAJob.objects.filter(device=device).exclude(
        status__in=OTAJob.TERMINAL_STATUSES
    ).exclude(pk=issuing_job).exists():
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
    mqtt_connection()
    if settings.OTA_DOWNLOAD_TTL_SECONDS <= 0:
        raise ValidationError("OTA download TTL must be positive.")


def prepare_ota(device, firmware, user):
    if not user or not user.has_perm("myapp.deploy_firmware"):
        raise PermissionDenied("Tài khoản không có quyền deploy firmware.")

    if settings.MQTT_PUBLIC_TEST_MODE:
        raise ValidationError("OTA is disabled in public test mode.")

    now = timezone.now()
    base_url = _https_base_url()
    key = _device_key(device.device_id)
    raw_token = secrets.token_urlsafe(32)
    request_id = f"ota-{secrets.token_hex(16)}"

    with transaction.atomic():
        locked_device = ChargerDevice.objects.select_for_update().get(pk=device.pk)
        firmware = FirmwareArtifact.objects.get(pk=firmware.pk)
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
    return job, command


def _probe_https_release(job, command):
    manifest_url = command["manifest_url"]
    token = manifest_url.rstrip("/").rsplit("/", 1)[1]
    expected_manifest = build_manifest(job, token)
    deadline = monotonic() + 30
    headers = {"User-Agent": "ESP32 HTTP Client/1.0", "Accept-Encoding": "identity"}
    for url, content_type, size in (
        (manifest_url, "application/json", None),
        (expected_manifest["url"], "application/octet-stream", job.firmware.file_size),
    ):
        with requests.get(
            url, headers=headers, timeout=(5, 10), stream=True, allow_redirects=False,
        ) as response:
            length = int(response.headers.get("Content-Length", "0"))
            if (
                response.status_code != 200
                or response.headers.get("Content-Type", "").split(";", 1)[0] != content_type
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or response.headers.get("Transfer-Encoding")
                or not 0 < length <= (size or 2048)
                or (size is not None and length != size)
            ):
                raise ValueError("HTTPS response is incompatible with ESP32 OTA.")
            manifest = bytearray()
            digest = hashlib.sha256()
            received = 0
            for chunk in response.iter_content(65536):
                received += len(chunk)
                if received > length or monotonic() > deadline:
                    raise ValueError("HTTPS OTA probe exceeded its size or time limit.")
                if size is None:
                    manifest.extend(chunk)
                else:
                    digest.update(chunk)
            if received != length:
                raise ValueError("HTTPS OTA body length mismatch.")
            if size is None:
                if json.loads(manifest) != expected_manifest:
                    raise ValueError("HTTPS OTA manifest mismatch.")
            elif digest.hexdigest() != job.firmware.sha256:
                raise ValueError("HTTPS OTA image SHA-256 mismatch.")


def publish_ota(job, command):
    try:
        if settings.MQTT_PUBLIC_TEST_MODE:
            raise ValidationError("OTA is disabled in public test mode.")
        _probe_https_release(job, command)
        job.refresh_from_db()
        if job.is_terminal:
            return False
        validate_device_ready_for_ota(job.device, job.firmware, issuing_job=job.pk)
    except (requests.RequestException, ValueError, ValidationError) as exc:
        message = (
            " ".join(exc.messages) if isinstance(exc, ValidationError)
            else "HTTPS verification failed. Check the tunnel, TLS, headers and release."
        )
        OTAJob.objects.filter(pk=job.pk, status=OTAJob.STATUS_PENDING).update(
            status=OTAJob.STATUS_FAILED,
            error_message=message,
            last_status_at=timezone.now(),
        )
        job.refresh_from_db()
        return False
    try:
        publish_once(
            OTA_COMMAND_TOPIC,
            json.dumps(command, separators=(",", ":")),
            qos=1,
            retain=False,
            **mqtt_connection(),
        )
    except Exception:
        OTAJob.objects.filter(pk=job.pk, status=OTAJob.STATUS_PENDING).update(
            error_message="MQTT publish could not be confirmed. Check broker and device logs.",
        )
        job.refresh_from_db()
        return False

    job.command_sent_at = timezone.now()
    job.save(update_fields=["command_sent_at", "updated_at"])
    return True


def create_and_issue_ota(device, firmware, user):
    job, command = prepare_ota(device, firmware, user)
    transaction.on_commit(lambda: publish_ota(job, command))
    return job, command


def authorize_job_token(request_id, raw_token):
    if settings.MQTT_PUBLIC_TEST_MODE:
        raise PermissionDenied("OTA is disabled in public test mode.")
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
    if not job.firmware.is_approved or job.firmware.firmware_type != FirmwareArtifact.TYPE_MAIN:
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


# Recovery emits progress on a best-effort connection. Allow forward jumps
# during an attempt and a reset when Recovery starts another attempt.
STATUS_ORDER = {
    OTAJob.STATUS_PENDING: 0,
    OTAJob.STATUS_ACCEPTED: 1,
    OTAJob.STATUS_RECOVERING: 2,
    OTAJob.STATUS_DOWNLOADING: 3,
    OTAJob.STATUS_VERIFYING: 4,
    OTAJob.STATUS_INSTALLING: 5,
    OTAJob.STATUS_REBOOTING: 6,
    OTAJob.STATUS_SUCCESS: 7,
}


def process_ota_status(data):
    if settings.MQTT_PUBLIC_TEST_MODE:
        raise ValidationError("Unsigned OTA status requires authenticated broker ACLs.")
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
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", request_id):
        raise ValidationError("Invalid OTA request_id.")
    if not isinstance(status, str) or status == OTAJob.STATUS_PENDING or status not in dict(OTAJob.STATUS_CHOICES):
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
        if status == OTAJob.STATUS_SUCCESS and progress != 100:
            raise ValidationError("SUCCESS must report 100 percent progress.")
        if status == OTAJob.STATUS_REJECTED_BUSY and job.status != OTAJob.STATUS_PENDING:
            raise ValidationError("REJECTED_BUSY is only valid before acceptance.")
        if status == job.status and progress == job.progress:
            return job, False
        if status == OTAJob.STATUS_DOWNLOADING and job.status == status and progress < job.progress:
            raise ValidationError("OTA download progress moved backwards.")
        restarting = status == OTAJob.STATUS_RECOVERING or (
            job.status == OTAJob.STATUS_REBOOTING
            and status in {OTAJob.STATUS_DOWNLOADING, OTAJob.STATUS_VERIFYING, OTAJob.STATUS_INSTALLING}
        )
        if not restarting and STATUS_ORDER.get(status, 8) < STATUS_ORDER[job.status]:
            raise ValidationError(f"Unexpected OTA transition: {job.status} -> {status}.")

        now = timezone.now()
        # FAILED after entering Recovery can mean the download will be retried.
        # The firmware uses the same FAILED payload after exhausting boot attempts.
        # Keep the capability and active-job lock until timeout or confirmation.
        recovery_failure = status == OTAJob.STATUS_FAILED and (
            job.status in {
                OTAJob.STATUS_RECOVERING, OTAJob.STATUS_DOWNLOADING,
                OTAJob.STATUS_VERIFYING, OTAJob.STATUS_INSTALLING,
            }
            or job.status == OTAJob.STATUS_REBOOTING
        )
        job.status = OTAJob.STATUS_RECOVERING if recovery_failure else status
        job.progress = progress
        job.reported_version = version
        job.last_status_at = now
        job.error_message = (
            "FAILED: Recovery reported an error and may retry." if recovery_failure
            else str(data.get("error", ""))[:500]
        )
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
        | Q(last_status_at__isnull=True, command_sent_at__isnull=True, created_at__lt=cutoff)
        | Q(download_expires_at__lte=now)
    ).update(
        status=OTAJob.STATUS_FAILED,
        error_message="Timeout: thiết bị không báo trạng thái OTA.",
        last_status_at=now,
    )
