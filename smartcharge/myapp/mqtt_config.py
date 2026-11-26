import re
import ssl
from time import monotonic

import paho.mqtt.client as mqtt

from django.conf import settings
from django.core.exceptions import ValidationError


from .device_identity import DEVICE_ID_PATTERN


PUBLIC_TEST_HOST = "broker.emqx.io"
PUBLIC_TEST_PORT = 8883


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
    if settings.MQTT_PUBLIC_TEST_MODE:
        if not subscribe_only:
            raise ValidationError("MQTT publishing is disabled in public test mode.")
        public_test_topics()
        return {
            "hostname": PUBLIC_TEST_HOST,
            "port": PUBLIC_TEST_PORT,
            "auth": None,
            "tls": ssl.create_default_context(),
        }

    host = settings.MQTT_BROKER
    normalized_host = host.lower().rstrip(".")
    public_hosts = ("broker.hivemq.com", PUBLIC_TEST_HOST)
    if (
        not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or any(normalized_host == public or normalized_host.endswith("." + public) for public in public_hosts)
        or not 1 <= settings.MQTT_PORT <= 65535
        or not settings.MQTT_USERNAME.strip()
        or not settings.MQTT_PASSWORD
    ):
        raise ValidationError("Configure a private MQTT TLS broker and backend credentials.")
    try:
        tls = ssl.create_default_context(cafile=settings.MQTT_CA_FILE or None)
    except (OSError, ValueError) as exc:
        raise ValidationError("MQTT_CA_FILE cannot be loaded. Check the Root CA path and PEM format.") from exc
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
