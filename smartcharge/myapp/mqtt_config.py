import re
import ssl
from time import monotonic

import paho.mqtt.client as mqtt

from django.conf import settings
from django.core.exceptions import ValidationError


def mqtt_connection():
    host = settings.MQTT_BROKER
    if (
        not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or "broker.hivemq.com" in host.lower()
        or not 1 <= settings.MQTT_PORT <= 65535
        or not settings.MQTT_USERNAME.strip()
        or not settings.MQTT_PASSWORD
    ):
        raise ValidationError("Configure a private MQTT TLS broker and backend credentials.")
    return {
        "hostname": host,
        "port": settings.MQTT_PORT,
        "auth": {"username": settings.MQTT_USERNAME, "password": settings.MQTT_PASSWORD},
        "tls": ssl.create_default_context(),
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
