import math
import re
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import ChargerDevice, ChargingSession, DeviceLog, DeviceTelemetry
from .device_identity import DEVICE_ID_PATTERN, version_parts


ALLOWED_TELEMETRY_STATUSES = {"IDLE", "CHARGING", "DONE"}
EVENT_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")
SECRET_PATTERN = re.compile(
    r"(password|passwd|secret|token|authorization|bearer|api[_-]?key|hmac|sepay|account[_ -]?number|bank[_ -]?account|payment[_ -]?code)", re.I
)


def _number(data, key, minimum, maximum):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key} phải là số.")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValidationError(f"{key} vượt phạm vi hợp lệ.")
    return value


def _optional_integer(data, key, minimum, maximum):
    value = data.get(key)
    if value is not None and (type(value) is not int or not minimum <= value <= maximum):
        raise ValidationError(f"Invalid {key}.")
    return value


def _device(device_id):
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValidationError("Device ID không hợp lệ.")
    device, _ = ChargerDevice.objects.get_or_create(device_id=device_id)
    return device


def _prune(model, device, limit):
    if limit <= 0:
        return
    boundary = (
        model.objects.filter(device=device)
        .order_by("-received_at")
        .values_list("received_at", flat=True)[limit : limit + 1]
        .first()
    )
    if boundary:
        model.objects.filter(device=device, received_at__lte=boundary).delete()


def process_telemetry(data):
    if not isinstance(data, dict):
        raise ValidationError("Telemetry phải là JSON object.")
    device = _device(data.get("device"))
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 32:
        raise ValidationError("Telemetry port không hợp lệ.")
    charging = data.get("charging")
    if not isinstance(charging, bool):
        raise ValidationError("Telemetry charging phải là boolean.")
    status = data.get("status")
    if status not in ALLOWED_TELEMETRY_STATUSES:
        raise ValidationError("Telemetry status không hợp lệ.")
    if charging != (status == "CHARGING"):
        raise ValidationError("Telemetry charging và status không nhất quán.")
    interval = _number(data, "interval_s", 1, 60)
    if not 5 <= interval <= 20:
        raise ValidationError("Telemetry interval lệch quá xa chu kỳ 10 giây.")

    sample_time = timezone.now()
    if data.get("timestamp"):
        sample_time = parse_datetime(str(data["timestamp"]))
        if sample_time is None or timezone.is_naive(sample_time):
            raise ValidationError("Telemetry timestamp phải là ISO-8601 UTC.")
        sample_time = sample_time.astimezone(datetime_timezone.utc)

    firmware_version = data.get("firmware_version", "")
    if (not isinstance(firmware_version, str) or len(firmware_version) > 31
            or (firmware_version and not version_parts(firmware_version))):
        raise ValidationError("Invalid reported firmware version.")

    with transaction.atomic():
        telemetry = DeviceTelemetry.objects.create(
            device=device,
            port=port,
            voltage_v=_number(data, "voltage_v", 0, 500),
            current_a=_number(data, "current_a", 0, 100),
            power_w=_number(data, "power_w", 0, 50000),
            energy_total_wh=_number(data, "energy_total_wh", 0, 1_000_000_000),
            energy_delta_wh=_number(data, "energy_delta_wh", -1000, 1_000_000),
            charging=charging,
            charging_status=status,
            timestamp=sample_time,
            heap_free_bytes=_optional_integer(data, "heap_free_bytes", 0, 16 * 1024 * 1024),
            rssi_dbm=_optional_integer(data, "rssi_dbm", -127, 0),
            firmware_version=firmware_version,
        )
        ChargerDevice.objects.filter(pk=device.pk).update(last_seen_at=telemetry.received_at)
        if charging and not settings.MQTT_PUBLIC_TEST_MODE:
            ChargingSession.objects.filter(
                device=device, port_no=port, status="assigned", mqtt_start_sent_at__isnull=False,
            ).update(status="charging", updated_at=telemetry.received_at)
    _prune(
        DeviceTelemetry,
        device,
        settings.TELEMETRY_RETENTION_PER_DEVICE,
    )
    return telemetry


def process_device_log(data):
    if not isinstance(data, dict):
        raise ValidationError("Device log phải là JSON object.")
    device = _device(data.get("device"))
    level = data.get("level")
    if level == "WARN":
        level = "WARNING"
    if level not in {"INFO", "WARNING", "ERROR"}:
        raise ValidationError("Log level không hợp lệ.")
    event = data.get("event")
    if not isinstance(event, str) or not EVENT_PATTERN.fullmatch(event):
        raise ValidationError("Log event không hợp lệ.")
    message = data.get("message")
    if not isinstance(message, str) or not 1 <= len(message) <= 500:
        raise ValidationError("Log message không hợp lệ.")
    if SECRET_PATTERN.search(message):
        message = "[REDACTED: possible credential]"
    uptime = data.get("uptime_ms")
    if uptime is not None and (
        isinstance(uptime, bool) or not isinstance(uptime, int) or not 0 <= uptime <= 2**63 - 1
    ):
        raise ValidationError("uptime_ms không hợp lệ.")

    with transaction.atomic():
        log = DeviceLog.objects.create(
            device=device,
            level=level,
            event=event,
            message=message,
            uptime_ms=uptime,
        )
        ChargerDevice.objects.filter(pk=device.pk).update(last_seen_at=log.received_at)
    _prune(DeviceLog, device, settings.DEVICE_LOG_RETENTION_PER_DEVICE)
    return log
