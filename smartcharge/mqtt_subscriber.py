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
from myapp.ota_services import expire_stale_ota_jobs, process_ota_status


MQTT_BROKER = settings.MQTT_BROKER
MQTT_PORT = settings.MQTT_PORT
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


def on_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code != 0:
        logger.error("MQTT connection failed: reason=%s", reason_code)
        return
    result, message_id = client.subscribe(SUBSCRIPTIONS)
    logger.info(
        "MQTT connected to %s:%s; subscribed=%s result=%s mid=%s",
        MQTT_BROKER, MQTT_PORT, [topic for topic, _ in SUBSCRIPTIONS], result, message_id,
    )


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    logger.warning("MQTT disconnected: reason=%s; client will reconnect", reason_code)


def _dispatch(topic, data):
    if topic == MQTT_TOPIC_STATUS:
        if data.get("status") != "DONE":
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
    if len(msg.payload) > MAX_PAYLOAD_BYTES:
        logger.error("MQTT payload too large topic=%s bytes=%s", msg.topic, len(msg.payload))
        return
    try:
        payload = msg.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.error("MQTT payload is not UTF-8 topic=%s: %s", msg.topic, exc)
        return
    logger.info("MQTT received topic=%s bytes=%s", msg.topic, len(msg.payload))
    try:
        data = json.loads(payload)
        result = _dispatch(msg.topic, data)
        logger.info("MQTT processed topic=%s result=%s", msg.topic, getattr(result, "pk", result))
    except json.JSONDecodeError as exc:
        logger.error("Invalid MQTT JSON topic=%s: %s", msg.topic, exc)
    except ValidationError as exc:
        logger.warning("Rejected MQTT message topic=%s: %s", msg.topic, "; ".join(exc.messages))
    except DatabaseError:
        logger.exception("Database error processing MQTT topic=%s", msg.topic)
    except Exception:
        logger.exception("Unexpected error processing MQTT topic=%s", msg.topic)


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
        time.sleep(30)


def start_mqtt_subscriber():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smartcharge-backend")
    if settings.MQTT_USERNAME and settings.MQTT_PASSWORD:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
    threading.Thread(target=_timeout_worker, name="ota-timeouts", daemon=True).start()
    logger.info(
        "Starting one standalone MQTT subscriber %s:%s (never run inside Django workers)",
        MQTT_BROKER, MQTT_PORT,
    )
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    start_mqtt_subscriber()
