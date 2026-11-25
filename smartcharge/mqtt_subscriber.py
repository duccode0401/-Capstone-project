import json
import logging
import os
import threading
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "smartcharge.settings")

import django

django.setup()

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, transaction
from django.utils import timezone

from myapp.iot_services import process_device_log, process_telemetry
from myapp.models import ChargerDevice, ChargingSession
from myapp.mqtt_config import mqtt_connection, public_test_topics
from myapp.sepay import dispatch_paid_orders
from myapp.ota_services import expire_stale_ota_jobs, process_ota_status


MQTT_TOPIC_STATUS = "iot/charge_station/v1/status"
MQTT_TOPIC_TELEMETRY = "iot/charge_station/v1/telemetry"
MQTT_TOPIC_LOGS = "iot/charge_station/v1/logs"
MQTT_TOPIC_OTA_STATUS = "iot/charge_station/v1/ota/status"
SUBSCRIPTIONS = [
    (MQTT_TOPIC_STATUS, 1),
    (MQTT_TOPIC_TELEMETRY, 0),
    (MQTT_TOPIC_LOGS, 0),
    (MQTT_TOPIC_OTA_STATUS, 1),
]
PUBLIC_TEST_EVENTS = {
    "BOOT": ("INFO", "Relay OFF; application starting"),
    "BOOTSTRAP_FAILED": ("ERROR", "Preloaded application invalid"),
    "HEALTHY": ("INFO", "Application startup health checks passed"),
    "HEAP_LOW": ("WARN", "Internal RAM pressure"),
    "MQTT_CONNECTED": ("INFO", "MQTT session connected"),
    "MQTT_DISCONNECTED": ("WARN", "MQTT session disconnected"),
    "REBOOT_REASON": ("INFO", "Device restarted"),
    "RECOVERING": ("WARN", "Recovery running; relay locked OFF"),
    "RELAY_OFF": ("INFO", "Relay OFF"),
    "SENSOR_ERROR": ("ERROR", "ADC measurement failed; relay OFF"),
    "WATCHDOG_RESET": ("WARN", "Watchdog reset"),
    "WIFI_CONNECTED": ("INFO", "Wi-Fi obtained IP address"),
    "WIFI_DISCONNECTED": ("WARN", "Wi-Fi disconnected; reconnecting"),
}
PUBLIC_MEASUREMENTS = (
    "voltage_v", "current_a", "power_w", "energy_total_wh", "energy_delta_wh", "interval_s",
    "heap_free_bytes", "rssi_dbm",
)
ACTIVE_STATUSES = ("assigned", "charging")
MAX_PAYLOAD_BYTES = 4096

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smartcharge.mqtt_subscriber")


def mark_session_done(device_id, port_no):
    """Đánh dấu session active mới nhất trên đúng device/port là hoàn tất."""
    if not device_id or port_no is None:
        logger.warning("DONE missing device or port: device=%r port=%r", device_id, port_no)
        return None
    try:
        port_no = int(port_no)
    except (TypeError, ValueError):
        logger.warning("DONE has invalid port: device=%r port=%r", device_id, port_no)
        return None

    with transaction.atomic():
        active = list(
            ChargingSession.objects.select_for_update()
            .filter(
                device__device_id=device_id,
                port_no=port_no,
                status__in=ACTIVE_STATUSES,
            )
            .order_by("-created_at", "-id")[:2]
        )
        now = timezone.now()
        ChargerDevice.objects.filter(device_id=device_id).update(last_seen_at=now)
        if not active:
            latest = (
                ChargingSession.objects.filter(
                    device__device_id=device_id, port_no=port_no
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if latest and latest.status == "done":
                logger.info(
                    "Duplicate/stale DONE ignored for completed session=%s device=%s port=%s",
                    latest.id, device_id, port_no,
                )
            else:
                logger.warning(
                    "DONE ignored: no active session for device=%s port=%s",
                    device_id, port_no,
                )
            return None
        if len(active) > 1:
            logger.error(
                "Multiple active sessions device=%s port=%s; updating newest=%s only",
                device_id, port_no, active[0].id,
            )
        session = active[0]
        session.status = "done"
        session.save(update_fields=["status", "updated_at"])
        logger.info("Session=%s changed to done device=%s port=%s", session.id, device_id, port_no)
        return session


def _subscriptions():
    if settings.MQTT_PUBLIC_TEST_MODE:
        return [(topic, 0) for topic in public_test_topics()]
    return SUBSCRIPTIONS


def on_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code != 0:
        logger.error("MQTT connection failed: reason=%s", reason_code)
        return
    subscriptions = _subscriptions()
    result, message_id = client.subscribe(subscriptions)
    logger.info(
        "MQTT connected; subscribed=%s result=%s mid=%s",
        [topic for topic, _ in subscriptions], result, message_id,
    )


def on_subscribe(client, userdata, mid, reason_codes, properties):
    denied = [topic for (topic, _), reason in zip(_subscriptions(), reason_codes) if reason.is_failure]
    if denied:
        logger.error("Broker denied subscriptions: %s", ", ".join(denied))


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    logger.warning("MQTT disconnected: reason=%s; client will reconnect", reason_code)


def _dispatch_public_test(topic, data):
    telemetry, logs = public_test_topics()
    device = settings.MQTT_PUBLIC_TEST_DEVICE or settings.MQTT_DEVICE_ID
    if not isinstance(data, dict) or data.get("device") != device:
        raise ValidationError("Unexpected MQTT test device.")
    if topic == telemetry and data.get("charging") is False and data.get("status") == "IDLE":
        sample = {key: data.get(key) for key in PUBLIC_MEASUREMENTS}
        if "firmware_version" in data:
            sample["firmware_version"] = data["firmware_version"]
        return process_telemetry(dict(sample, device=device, port=1, charging=False, status="IDLE"))
    if topic == logs:
        event = data.get("event")
        if isinstance(event, str) and event in PUBLIC_TEST_EVENTS:
            level, message = PUBLIC_TEST_EVENTS[event]
            return process_device_log({
                "device": device, "level": level, "event": event,
                "message": message, "uptime_ms": data.get("uptime_ms"),
            })
    raise ValidationError("Unexpected MQTT test message.")


def _dispatch(topic, data):
    if settings.MQTT_PUBLIC_TEST_MODE:
        return _dispatch_public_test(topic, data)
    if topic == MQTT_TOPIC_STATUS:
        if not isinstance(data, dict) or data.get("status") != "DONE":
            raise ValidationError("Unsupported charging status message.")
        return mark_session_done(data.get("device"), data.get("port"))
    if topic == MQTT_TOPIC_TELEMETRY:
        return process_telemetry(data)
    if topic == MQTT_TOPIC_LOGS:
        return process_device_log(data)
    if topic == MQTT_TOPIC_OTA_STATUS:
        return process_ota_status(data)
    raise ValidationError("Unexpected MQTT topic.")


def on_message(client, userdata, msg):
    if settings.MQTT_PUBLIC_TEST_MODE and msg.topic not in public_test_topics():
        return
    if getattr(msg, "retain", False) is True:
        logger.warning("Ignoring retained MQTT message on %s", msg.topic)
        return
    close_old_connections()
    if len(msg.payload) > MAX_PAYLOAD_BYTES:
        logger.error("MQTT payload too large topic=%s bytes=%s", msg.topic, len(msg.payload))
        return
    try:
        payload = msg.payload.decode("utf-8")
    except UnicodeDecodeError:
        logger.error("MQTT payload is not UTF-8 topic=%s", msg.topic)
        return
    logger.info("MQTT received topic=%s bytes=%s", msg.topic, len(msg.payload))
    try:
        data = json.loads(payload)
        result = _dispatch(msg.topic, data)
        logger.info("MQTT processed topic=%s result=%s", msg.topic, getattr(result, "pk", result))
    except json.JSONDecodeError:
        logger.error("Invalid MQTT JSON topic=%s", msg.topic)
    except ValidationError as exc:
        logger.warning("Rejected MQTT message topic=%s: %s", msg.topic, "; ".join(exc.messages))
    except DatabaseError:
        logger.exception("Database error processing MQTT topic=%s", msg.topic)
    except Exception:
        logger.exception("Unexpected error processing MQTT topic=%s", msg.topic)
    finally:
        close_old_connections()


def _timeout_worker():
    while True:
        try:
            close_old_connections()
            expired = expire_stale_ota_jobs()
            if expired:
                logger.warning("Marked %s OTA job(s) failed by timeout", expired)
        except Exception:
            logger.exception("OTA timeout worker failed")
        finally:
            close_old_connections()
        try:
            dispatch_paid_orders()
        except (DatabaseError, ValidationError):
            logger.warning("Could not dispatch verified SePay orders.")
        finally:
            close_old_connections()
        time.sleep(30)


def start_mqtt_subscriber():
    test_mode = settings.MQTT_PUBLIC_TEST_MODE
    try:
        config = mqtt_connection(subscribe_only=True)
    except ValidationError as exc:
        raise SystemExit(exc.messages[0]) from exc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="" if test_mode else "smartcharge-backend")
    if config["auth"] is not None:
        client.username_pw_set(**config["auth"])
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.tls_set_context(config["tls"])
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(config["hostname"], config["port"], keepalive=60)
    if not test_mode:
        threading.Thread(target=_timeout_worker, name="ota-timeouts", daemon=True).start()
    logger.info(
        "Starting one standalone MQTT subscriber %s:%s (never run inside Django workers)",
        config["hostname"], config["port"],
    )
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    start_mqtt_subscriber()
