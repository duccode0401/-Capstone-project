"""Interactive smoke test for the Windows SmartCharge MQTT broker."""
import os
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from queue import Queue
from secrets import token_hex
import socket
import ssl
import sys

sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "SmartChargeMQTT" / "test-libs"))
import paho.mqtt.client as mqtt

IP = "192.168.1.104"
PORT = 8883
CA = r"C:\SmartChargeMQTT\pki\root-ca.pem"
BASE = "iot/charge_station/v1/"
DEVICE = "ESP32_001"
BACKEND = "smartcharge_backend"


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def session(host, user=None, password=None, reject=False):
    replies = Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5,
                         reconnect_on_failure=False)
    if user:
        client.username_pw_set(user, password)
    client.tls_set_context(ssl.create_default_context(cafile=CA))
    client.on_connect = lambda c, u, f, code, p: replies.put(code)
    client.connect_timeout = 5
    client.connect(host, PORT)
    client.loop_start()
    try:
        code = replies.get(timeout=8)
        if reject:
            check(code.value in {134, 135}, f"Expected authentication refusal, got {code}")
        else:
            check(not code.is_failure, f"{user} was refused: {code}")
        yield client
    finally:
        client.disconnect()
        client.loop_stop()


def publish(client, topic, payload, accepted):
    replies = Queue()
    client.on_publish = lambda c, u, mid, code, p: replies.put(code)
    client.publish(BASE + topic, payload, qos=1)
    code = replies.get(timeout=8)
    check(code.value in ({0, 16} if accepted else {135}), f"Unexpected PUBACK for {topic}: {code}")


def main():
    for host in (IP, "localhost"):
        with session(host, reject=True):
            print(f"PASS: trusted TLS identity {host}; anonymous rejected")
    for context, name in ((ssl.create_default_context(), "wrong-name.invalid"),
                          (ssl.create_default_context(cafile=CA), "wrong-name.invalid")):
        try:
            with socket.create_connection((IP, PORT), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=name):
                    raise RuntimeError("Unexpected TLS acceptance")
        except ssl.SSLCertVerificationError:
            pass
    print("PASS: certificate identity mismatch rejected")

    if not sys.stdin.isatty():
        raise RuntimeError("Run in a local interactive console.")
    device_password = getpass(f"{DEVICE} password: ")
    backend_password = getpass(f"{BACKEND} password: ")
    check(device_password and backend_password, "Both passwords are required.")
    with session(IP, DEVICE, token_hex(32), reject=True):
        print("PASS: incorrect password rejected")
    with session("localhost", BACKEND, backend_password) as backend, session(IP, DEVICE, device_password) as device:
        subscriptions = Queue()
        messages = Queue()
        backend.on_subscribe = lambda c, u, mid, codes, p: subscriptions.put(codes)
        backend.on_message = lambda c, u, msg: messages.put((msg.topic, msg.payload))
        backend.subscribe(BASE + "telemetry", qos=1)
        check(not subscriptions.get(timeout=8)[0].is_failure, "Backend telemetry subscription refused")
        payload = ('{"device":"MQTT_SMOKE_TEST","nonce":"' + token_hex(16) + '"}').encode()
        publish(device, "telemetry", payload, accepted=True)
        while True:
            topic, received = messages.get(timeout=8)
            if (topic, received) == (BASE + "telemetry", payload):
                break
        print("PASS: device publish -> backend subscribe with verified TLS")
        publish(device, "orders", b'{}', accepted=False)
        publish(device, "ota/command", b'{}', accepted=False)
        publish(backend, "telemetry", b'{}', accepted=False)
        print("PASS: reverse-direction publish attempts rejected")


if __name__ == "__main__":
    main()
