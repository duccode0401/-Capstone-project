"""MQTT v2 ingestion and application acknowledgements."""
import hashlib
import json
import logging
import math
import re
from datetime import timedelta, timezone as utc_timezone
from time import monotonic

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

from .iot_services import EVENT_PATTERN, _optional_integer
from .models import ChargerDevice, MqttAttempt, MqttReceipt, OTAJob
from .mqtt_config import ack_topic
from .device_identity import DEVICE_ID_PATTERN
from .ota_services import version_parts

logger = logging.getLogger("smartcharge.mqtt_subscriber")
MESSAGE_ID = re.compile(r"(?:[a-f0-9]{16}-[a-f0-9]{8}|[a-fA-F0-9]{8}-[a-fA-F0-9]{8})")
WIRE_TYPES = {
    "telemetry": {"telemetry"}, "logs": {"log", "logs"}, "heartbeat": {"heartbeat"},
    "status": {"status"}, "ota": {"ota_status"},
}
HEALTH_INTEGERS = {
    "uptime_ms": (0, 2**63 - 1),
    "free_heap": (0, 16 * 1024 * 1024),
    "min_free_heap": (0, 16 * 1024 * 1024),
    "rssi": (-127, 0),
    "dropped_messages": (0, 2**63 - 1),
    "reset_reason": (0, 255),
}
HEALTH_FLAGS = ("mqtt_connected", "relay_on")
HEALTH_ENUMS = {
    "sensor_status": {"DISABLED", "OK", "ERROR", "ENABLED", "READY", "FAILED", "UNKNOWN", "UNAVAILABLE", "FAULT"},
    "display_status": {"DISABLED", "OK", "ERROR", "ENABLED", "READY", "FAILED", "UNKNOWN", "UNAVAILABLE", "FAULT"},
    "mode": {"HEADLESS", "NORMAL", "RECOVERY"},
}
NUMERIC_FIELDS = {
    *HEALTH_INTEGERS, "progress", "protocol_version", "port", "voltage_v", "current_a", "power_w",
    "energy_total_wh", "energy_delta_wh", "interval_s", "heap_free_bytes", "rssi_dbm",
}


class Rejected(ValidationError):
    pass


def _matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _firmware(value):
    return isinstance(value, str) and len(value) <= 31 and version_parts(value)


def sanitized_payload(data):
    # Free-form text, nested values, credentials and payment fields are excluded.
    result = {}
    for key in NUMERIC_FIELDS:
        value = data.get(key)
        if ((type(value) is int and -(2**63) <= value < 2**63)
                or (type(value) is float and math.isfinite(value))):
            result[key] = value
    for key in (*HEALTH_FLAGS, "charging"):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key, allowed in {
        **HEALTH_ENUMS, "status": {"IDLE", "CHARGING", "DONE", *(key for key, _ in OTAJob.STATUS_CHOICES)},
        "level": {"INFO", "WARN", "WARNING", "ERROR"},
        "message_type": set().union(*WIRE_TYPES.values()),
    }.items():
        if isinstance(data.get(key), str) and data[key] in allowed:
            result[key] = data[key]
    if _firmware(data.get("firmware_version")):
        result["firmware_version"] = data["firmware_version"]
    if _matches(EVENT_PATTERN, data.get("event")):
        result["event"] = data["event"]
    if _firmware(data.get("version")):
        result["version"] = data["version"]
    if "message" in data:
        result["message"] = "[REDACTED]"
    return result


def process_heartbeat(device, data, received_at):
    health = {}
    for key, (minimum, maximum) in HEALTH_INTEGERS.items():
        value = _optional_integer(data, key, minimum, maximum)
        if value is None:
            raise Rejected("INVALID_PAYLOAD")
        health[key] = value
    for key in HEALTH_FLAGS:
        if type(data.get(key)) is not bool:
            raise Rejected("INVALID_PAYLOAD")
        health[key] = data[key]
    for key, allowed in HEALTH_ENUMS.items():
        if not isinstance(data.get(key), str) or data[key] not in allowed:
            raise Rejected("INVALID_PAYLOAD")
        health[key] = data[key]
    if not _firmware(data.get("firmware_version")) or health["min_free_heap"] > health["free_heap"]:
        raise Rejected("INVALID_PAYLOAD")
    health["firmware_version"] = data["firmware_version"]
    ChargerDevice.objects.filter(pk=device.pk).update(
        mqtt_health=health, last_heartbeat_at=received_at, last_seen_at=received_at,
    )


def _ack_state(attempt_id, state):
    if attempt_id is None:
        return
    try:
        timestamps = {}
        if state == "QUEUED":
            timestamps["ack_published_at"] = timezone.now()
        elif state in {"BROKER_CONFIRMED", "BROKER_REJECTED"}:
            timestamps["ack_confirmed_at"] = timezone.now()
        MqttAttempt.objects.filter(pk=attempt_id).update(ack_status=state, **timestamps)
    except DatabaseError:
        logger.error("[DB] ACK status update failed")


def publish_ack(client, attempt_id, device, message_id, message_type, error, retryable):
    status = "FAILED" if error else "SUCCESS"
    payload = {
        "protocol_version": 2, "device": device, "message_id": message_id,
        "message_type": message_type, "status": status, "error_code": error,
        "retryable": retryable,
        "server_timestamp": timezone.now().astimezone(utc_timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        if not client.is_connected():
            _ack_state(attempt_id, "PUBLISH_FAILED")
            logger.warning("[TX] ACK publish failed: disconnected")
            return
        info = client.publish(ack_topic(device), json.dumps(payload), qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            _ack_state(attempt_id, "PUBLISH_FAILED")
            logger.warning("[TX] ACK publish failed rc=%s", info.rc)
            return
        # This runs on the Paho loop thread, before it can dispatch on_publish.
        client.smartcharge_ack_mids[info.mid] = attempt_id
        _ack_state(attempt_id, "QUEUED")
        logger.info("[TX] ACK %s", status)
        if error:
            logger.info("[TX] RETRYABLE=%s", str(retryable).lower())
    except Exception:
        _ack_state(attempt_id, "PUBLISH_FAILED")
        logger.warning("[TX] ACK publish failed")


def on_publish(client, userdata, mid, reason_code, properties):
    if mid in client.smartcharge_ack_mids:
        attempt = client.smartcharge_ack_mids.pop(mid)
        failed = reason_code.is_failure
        _ack_state(attempt, "BROKER_REJECTED" if failed else "BROKER_CONFIRMED")
        logger.info("[MQTT] ACK %s mid=%s", "REJECTED" if failed else "PUBACK", mid)


def ingest(client, topic, kind, data, dispatch, *, invalid_json=False):
    received_at = timezone.now()
    started = monotonic()
    device = data.get("device")
    message_id = data.get("message_id")
    valid_device = _matches(DEVICE_ID_PATTERN, device)
    valid_id = _matches(MESSAGE_ID, message_id)
    v2 = "protocol_version" in data or "message_id" in data
    public = settings.MQTT_PUBLIC_TEST_MODE
    safe_route = valid_device and (not public or device == settings.MQTT_PUBLIC_TEST_DEVICE)
    versioned_kind = kind in WIRE_TYPES
    wire_type = data.get("message_type")
    valid_type = isinstance(wire_type, str) and wire_type in WIRE_TYPES.get(kind, ())
    ack_type = wire_type if valid_type else {"logs": "log", "ota": "ota_status"}.get(kind, kind)
    can_ack = v2 and versioned_kind and safe_route and valid_id and data.get("message_type") != "ack"
    audit = {
        "received_at": received_at, "device_id": device if valid_device else "",
        "message_id": message_id if valid_id else "", "message_type": kind,
        "topic": topic, "payload": sanitized_payload(data),
    }
    logger.info("[RX] DEVICE=%s TYPE=%s ID=%s PAYLOAD=%s",
                audit["device_id"], kind.upper(), audit["message_id"], audit["payload"])

    def record(status, error=""):
        return MqttAttempt.objects.create(
            **audit, status=status, error_code=error, duration_ms=int((monotonic() - started) * 1000),
        ).pk

    def reply(attempt_id, error=None, retryable=False):
        if can_ack:
            transaction.on_commit(lambda: publish_ack(
                client, attempt_id, device, message_id, ack_type, error, retryable,
            ))

    error = None
    retryable = False
    try:
        if invalid_json:
            raise Rejected("INVALID_PAYLOAD")
        if not valid_device:
            raise Rejected("INVALID_DEVICE")
        if not safe_route:
            raise Rejected("UNAUTHORIZED_DEVICE")
        if v2 and versioned_kind:
            if type(data.get("protocol_version")) is not int or data["protocol_version"] != 2:
                raise Rejected("UNSUPPORTED_PROTOCOL")
            if not valid_id:
                raise Rejected("INVALID_MESSAGE_ID")
            if not valid_type:
                raise Rejected("INVALID_MESSAGE_TYPE")
            if _optional_integer(data, "uptime_ms", 0, 2**63 - 1) is None:
                raise Rejected("INVALID_PAYLOAD")
            if kind == "status" and (type(data.get("port")) is not int or not 1 <= data["port"] <= 32):
                raise Rejected("INVALID_PAYLOAD")
        elif kind == "heartbeat":
            raise Rejected("UNSUPPORTED_PROTOCOL")

        with transaction.atomic():
            status = "SUCCESS"
            if v2 and versioned_kind:
                devices = ChargerDevice.objects.select_for_update()
                if public or device == settings.MQTT_DEVICE_ID:
                    charger, _ = devices.get_or_create(device_id=device)
                else:
                    charger = devices.filter(device_id=device).first()
                    if charger is None:
                        raise Rejected("UNAUTHORIZED_DEVICE")
                digest = hashlib.sha256(json.dumps(
                    data, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode()).hexdigest()
                receipt, created = MqttReceipt.objects.get_or_create(
                    device=charger, message_id=message_id,
                    defaults={"digest": digest, "last_seen_at": received_at},
                )
                if receipt.digest != digest:
                    raise Rejected("MESSAGE_ID_CONFLICT")
                if created:
                    if kind == "heartbeat":
                        process_heartbeat(charger, data, received_at)
                    else:
                        dispatch(topic, data)
                else:
                    status = "DUPLICATE"
                    MqttReceipt.objects.filter(pk=receipt.pk).update(last_seen_at=received_at)
                counter = "mqtt_success_count" if created else "mqtt_duplicate_count"
                ChargerDevice.objects.filter(pk=charger.pk).update(
                    **{counter: F(counter) + 1}, mqtt_last_message_id=message_id,
                )
            else:
                dispatch(topic, data)
                ChargerDevice.objects.filter(device_id=device).update(
                    mqtt_success_count=F("mqtt_success_count") + 1,
                )
            attempt_id = record(status)
            transaction.on_commit(lambda: logger.info("[DB] COMMIT SUCCESS %s", status))
            reply(attempt_id)
        return
    except Rejected as exc:
        error = exc.message
    except (ValidationError, ValueError, TypeError, OverflowError):
        error = "INVALID_PAYLOAD"
    except DatabaseError:
        error, retryable = "DATABASE_ERROR", True
    except Exception:
        error, retryable = "INTERNAL_ERROR", True

    logger.warning("[DB] FAILED: %s", error)
    attempt_id = None
    try:
        with transaction.atomic():
            attempt_id = record("FAILED", error)
            if safe_route:
                updates = {"mqtt_failed_count": F("mqtt_failed_count") + 1, "mqtt_last_error": error}
                if valid_id:
                    updates["mqtt_last_message_id"] = message_id
                ChargerDevice.objects.filter(device_id=device).update(**updates)
    except DatabaseError:
        attempt_id = None
        logger.error("[DB] Failed to persist MQTT failure")
    reply(attempt_id, error, retryable)


def cleanup():
    """Keep receipt keys for at least 30 days after the last successful delivery."""
    now = timezone.now()
    for model, field, days in ((MqttAttempt, "received_at", 7), (MqttReceipt, "last_seen_at", 30)):
        expired = model.objects.filter(**{f"{field}__lt": now - timedelta(days=days)})
        ids = list(expired.order_by(field).values_list("pk", flat=True)[:5000])
        if ids:
            expired.filter(pk__in=ids).delete()
