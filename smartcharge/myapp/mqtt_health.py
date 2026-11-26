"""TLS MQTT health probes on an isolated topic."""
from time import monotonic
from uuid import uuid4

import paho.mqtt.client as mqtt


def roundtrip(config, *, topic_prefix="smartcharge/health", verify_ping=False, expect_rejection=False, credential_check=False):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    client.connect_timeout = 5
    if config["auth"]:
        client.username_pw_set(**config["auth"])
    client.tls_set_context(config["tls"])

    nonce = uuid4().hex
    topic = f"{topic_prefix.rstrip('/')}/{nonce}"
    expected = nonce.encode()
    state = {
        "connack": None, "suback": None, "puback": None, "received": False,
        "pingreq": False, "pingresp": False,
    }

    def on_connect(client, userdata, flags, reason, properties):
        state["connack"] = reason

    def on_subscribe(client, userdata, mid, reasons, properties):
        state["suback"] = reasons

    def on_publish(client, userdata, mid, reason, properties):
        state["puback"] = reason

    def on_message(client, userdata, message):
        state["received"] = message.topic == topic and message.payload == expected

    def on_log(client, userdata, level, message):
        for name in ("pingreq", "pingresp"):
            if name.upper() in message:
                state[name] = True

    def wait_for(key, seconds=6):
        deadline = monotonic() + seconds
        while not state[key] and monotonic() < deadline:
            result = client.loop(timeout=0.25)
            if result != mqtt.MQTT_ERR_SUCCESS and not state[key]:
                raise ConnectionError(f"MQTT {key}: {mqtt.error_string(result)}")
        if not state[key]:
            raise TimeoutError(f"MQTT {key}: no response.")
        return state[key]

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_publish = on_publish
    client.on_message = on_message
    client.on_log = on_log
    try:
        client.connect(config["hostname"], config["port"], keepalive=3)
        connected = wait_for("connack")
        if expect_rejection:
            if not connected.is_failure or connected.value not in (134, 135):
                raise ConnectionError(f"Expected an authentication rejection, got {connected}.")
            return
        if connected.is_failure:
            raise ConnectionError(f"MQTT CONNACK: {connected}")
        if credential_check:
            return
        result, _ = client.subscribe(topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS or any(reason.is_failure for reason in wait_for("suback")):
            raise ConnectionError("MQTT health subscription was denied.")
        publication = client.publish(topic, expected, qos=1, retain=False)
        if publication.rc != mqtt.MQTT_ERR_SUCCESS or wait_for("puback").is_failure:
            raise ConnectionError("MQTT health publication was denied.")
        wait_for("received")
        if verify_ping:
            wait_for("pingresp")
            if not state["pingreq"]:
                raise ConnectionError("MQTT PINGREQ was not observed.")
    finally:
        client.disconnect()
