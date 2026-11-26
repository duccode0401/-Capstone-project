"""Opt-in live EMQX test: simulated firmware -> Django ingestion -> Application ACK."""
import json
import os
import ssl
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "smartcharge.settings")

import django

django.setup()

import paho.mqtt.client as mqtt
from django.conf import settings

import mqtt_subscriber as subscriber
from myapp.models import ChargerDevice, DeviceLog, DeviceTelemetry, MqttAttempt
from myapp.mqtt_config import ack_topic, mqtt_connection


def main():
    alias = settings.MQTT_PUBLIC_TEST_DEVICE
    logical = settings.MQTT_PUBLIC_TEST_LOGICAL_DEVICE
    config = mqtt_connection(subscribe_only=True)
    route = f"iot/charge_station/test/{alias}"
    subscribed = threading.Event()
    received = threading.Event()
    acknowledgements = {}

    backend = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="")
    backend.smartcharge_ack_mids = {}
    backend.on_connect = subscriber.on_connect

    def backend_subscribed(client, userdata, mid, reason_codes, properties):
        subscriber.on_subscribe(client, userdata, mid, reason_codes, properties)
        if reason_codes and not any(reason.is_failure for reason in reason_codes):
            subscribed.set()

    backend.on_subscribe = backend_subscribed
    backend.on_message = subscriber.on_message
    backend.on_publish = subscriber.on_publish
    backend.tls_set_context(config["tls"])

    simulator = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="")
    simulator.tls_set_context(ssl.create_default_context())

    def capture_ack(client, userdata, message):
        payload = json.loads(message.payload.decode("utf-8"))
        acknowledgements[payload["message_id"]] = payload
        if len(acknowledgements) == 3:
            received.set()

    simulator.on_message = capture_ack
    try:
        backend.connect(config["hostname"], config["port"], keepalive=30)
        backend.loop_start()
        if not subscribed.wait(10):
            raise TimeoutError("Django subscriber did not subscribe")

        simulator.connect(config["hostname"], config["port"], keepalive=30)
        simulator.loop_start()
        result, _ = simulator.subscribe(ack_topic(alias), qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("Simulated device could not subscribe to ACK")
        time.sleep(1)

        prefix = os.urandom(4).hex()
        common = {
            "protocol_version": 2, "device": alias, "logical_device": logical,
            "uptime_ms": 10000,
        }
        messages = (
            ("telemetry", {
                **common, "message_id": f"{prefix}-00000001", "message_type": "telemetry",
                "port": 1, "voltage_v": 0.0, "current_a": 0.0, "power_w": 0.0,
                "energy_total_wh": 0.0, "energy_delta_wh": 0.0,
                "charging": False, "status": "IDLE", "interval_s": 10.0,
            }),
            ("logs", {
                **common, "message_id": f"{prefix}-00000002", "message_type": "logs",
                "level": "INFO", "event": "BOOT", "message": "Application started",
            }),
            ("heartbeat", {
                **common, "message_id": f"{prefix}-00000003", "message_type": "heartbeat",
                "firmware_version": "2.0.0", "free_heap": 60000, "min_free_heap": 55000,
                "rssi": -45, "mqtt_connected": True, "relay_on": False,
                "sensor_status": "DISABLED", "display_status": "DISABLED",
                "dropped_messages": 0, "reset_reason": 1, "mode": "HEADLESS",
            }),
        )
        for kind, payload in messages:
            info = simulator.publish(f"{route}/{kind}", json.dumps(payload), qos=1, retain=False)
            info.wait_for_publish(10)
            if not info.is_published():
                raise TimeoutError(f"Broker PUBACK missing for {kind}")

        if not received.wait(15):
            raise TimeoutError(f"Only received {len(acknowledgements)}/3 Application ACKs")
        expected = {payload["message_id"] for _, payload in messages}
        if set(acknowledgements) != expected or any(
                ack["status"] != "SUCCESS" or ack["device"] != alias
                for ack in acknowledgements.values()):
            raise AssertionError(f"Unexpected ACK payloads: {acknowledgements}")
        if (MqttAttempt.objects.filter(device_id=alias, message_id__in=expected, status="SUCCESS").count() != 3
                or not DeviceTelemetry.objects.filter(device__device_id=alias).exists()
                or not DeviceLog.objects.filter(device__device_id=alias, event="BOOT").exists()
                or not ChargerDevice.objects.filter(device_id=alias, last_heartbeat_at__isnull=False).exists()):
            raise AssertionError("Django did not commit all simulated firmware messages")
        print(f"PASS alias={alias} telemetry/logs/heartbeat ACKed over TLS")
    finally:
        simulator.disconnect()
        simulator.loop_stop()
        backend.disconnect()
        backend.loop_stop()


if __name__ == "__main__":
    main()
