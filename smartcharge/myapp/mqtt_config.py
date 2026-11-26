import re
import ssl
from time import monotonic

import paho.mqtt.client as mqtt

from django.conf import settings
from django.core.exceptions import ValidationError


from .device_identity import DEVICE_ID_PATTERN


PUBLIC_BROKER_HOSTS = ("broker.emqx.io", "broker.hivemq.com")


def public_test_topics():
    device = settings.MQTT_PUBLIC_TEST_DEVICE
    if not re.fullmatch(r"SC_TEST_[a-f0-9]{16}", device):
        raise ValidationError("MQTT_PUBLIC_TEST_DEVICE must be a provisioning alias.")
    if not DEVICE_ID_PATTERN.fullmatch(settings.MQTT_PUBLIC_TEST_LOGICAL_DEVICE):
        raise ValidationError("MQTT_PUBLIC_TEST_LOGICAL_DEVICE must be a valid logical Device ID.")
    base = f"iot/charge_station/test/{device}"
    return f"{base}/telemetry", f"{base}/logs"


def public_test_heartbeat_topic():
    telemetry, _ = public_test_topics()
    return telemetry.rsplit("/", 1)[0] + "/heartbeat"


def ack_topic(device):
    if not isinstance(device, str) or not DEVICE_ID_PATTERN.fullmatch(device):
        raise ValidationError("Invalid MQTT ACK device.")
    if settings.MQTT_PUBLIC_TEST_MODE:
        if device != settings.MQTT_PUBLIC_TEST_DEVICE:
            raise ValidationError("Unexpected public MQTT ACK device.")
        return public_test_heartbeat_topic().rsplit("/", 1)[0] + "/ack"
    return f"iot/charge_station/v1/ack/{device}"



def mqtt_connection(*, subscribe_only=False):
    host = settings.MQTT_BROKER.strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not 1 <= settings.MQTT_PORT <= 65535:
        raise ValidationError("Configure a valid MQTT_BROKER and MQTT_PORT.")
    try:
        tls = ssl.create_default_context(cafile=settings.MQTT_CA_FILE or None)
    except (OSError, ValueError) as exc:
        raise ValidationError("MQTT_CA_FILE cannot be loaded. Check the CA path and PEM format.") from exc

    if settings.MQTT_PUBLIC_TEST_MODE:
        public_test_topics()
        return {
            "hostname": host,
            "port": settings.MQTT_PORT,
            "auth": ({"username": settings.MQTT_USERNAME, "password": settings.MQTT_PASSWORD}
                     if settings.MQTT_USERNAME else None),
            "tls": tls,
        }

    normalized_host = host.lower().rstrip(".")
    if (
        any(normalized_host == public or normalized_host.endswith("." + public) for public in PUBLIC_BROKER_HOSTS)
        or not settings.MQTT_USERNAME.strip()
        or not settings.MQTT_PASSWORD
    ):
        raise ValidationError("Configure a private MQTT TLS broker and backend credentials.")
    return {
        "hostname": host,
        "port": settings.MQTT_PORT,
        "auth": {"username": settings.MQTT_USERNAME, "password": settings.MQTT_PASSWORD},
        "tls": tls,
    }


def publish_once(topic, payload, *, qos, retain, hostname, port, auth, tls):
    """Publish once and bound the wait for CONNACK and PUBACK."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
    client.connect_timeout = 5
    if auth:
        client.username_pw_set(**auth)
    client.tls_set_context(tls)
    connection = None

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal connection
        connection = not reason_code.is_failure

    def advance():
        if monotonic() >= deadline:
            raise TimeoutError("MQTT acknowledgement timed out.")
        if client.loop(timeout=0.5) != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT connection failed.")

    client.on_connect = on_connect
    try:
        if client.connect(hostname, port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT connection failed.")
        deadline = monotonic() + 10
        while connection is None:
            advance()
        if not connection:
            raise ConnectionError("MQTT broker rejected the connection.")
        message = client.publish(topic, payload, qos=qos, retain=retain)
        while not message.is_published():
            advance()
    finally:
        client.disconnect()
