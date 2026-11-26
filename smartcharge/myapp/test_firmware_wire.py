import json
import socket
import struct
from unittest.mock import patch
from datetime import datetime, timezone as utc_timezone

import paho.mqtt.client as mqtt
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from mqtt_subscriber import on_connect, on_message, on_publish, on_subscribe

from .models import ChargerDevice, DeviceLog, MqttAttempt, MqttReceipt


ALIAS = "SC_TEST_0123456789abcdef"
ROUTE = "iot/charge_station/test/" + ALIAS


def read_packet(stream):
    header = stream.read(1)[0]
    length = 0
    for shift in range(0, 28, 7):
        value = stream.read(1)[0]
        length |= (value & 127) << shift
        if value < 128:
            return header, stream.read(length)
    raise AssertionError("Malformed MQTT remaining length")


def packet(header, payload):
    length = len(payload)
    remainder = bytearray()
    while length >= 128:
        remainder.append((length & 127) | 128)
        length >>= 7
    remainder.append(length)
    return bytes([header]) + remainder + payload


@override_settings(
    MQTT_PUBLIC_TEST_MODE=True, MQTT_PUBLIC_TEST_DEVICE=ALIAS,
    MQTT_PUBLIC_TEST_LOGICAL_DEVICE="ESP32_001",
)
class FirmwareWireTests(TransactionTestCase):
    def test_firmware_json_round_trip_through_paho_and_broker_puback(self):
        # Loopback broker peer exercises MQTT 3.1.1 framing and Paho callbacks.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(3)
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.smartcharge_ack_mids = {}
            client.on_connect = on_connect
            client.on_subscribe = on_subscribe
            client.on_message = on_message
            client.on_publish = on_publish
            client.connect("127.0.0.1", listener.getsockname()[1])
            self.addCleanup(client.disconnect)
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                with connection.makefile("rb") as stream:
                    self.assertEqual(read_packet(stream)[0], 0x10)
                    connection.sendall(b"\x20\x02\x00\x00")
                    client.loop_read()
                    client.loop_write()
                    header, subscription = read_packet(stream)
                    self.assertEqual(header, 0x82)
                    self.assertIn((ROUTE + "/logs").encode(), subscription)
                    connection.sendall(packet(0x90, subscription[:2] + b"\x01\x01\x01"))
                    client.loop_read()
                    for packet_id, case in enumerate(("SUCCESS", "DUPLICATE", "FAILED"), 1):
                        with self.subTest(case=case):
                            message_id = "0123456789abcdef-0000000" + ("2" if case == "FAILED" else "1")
                            data = {
                                "protocol_version": 2, "message_id": message_id, "message_type": "log",
                                "device": ALIAS, "uptime_ms": 10000, "level": "INFO",
                                "event": "INVALID" if case == "FAILED" else "BOOT",
                            }
                            topic = (ROUTE + "/logs").encode()
                            connection.sendall(packet(
                                0x32, struct.pack("!H", len(topic)) + topic
                                + struct.pack("!H", packet_id) + json.dumps(data).encode(),
                            ))
                            client.loop_read()
                            client.loop_write()
                            replies = dict(read_packet(stream) for _ in range(2))
                            self.assertEqual(replies[0x40], struct.pack("!H", packet_id))
                            body = replies[0x32]
                            topic_size = struct.unpack_from("!H", body)[0]
                            self.assertEqual(body[2:2 + topic_size].decode(), ROUTE + "/ack")
                            ack_mid = body[2 + topic_size:4 + topic_size]
                            ack_bytes = body[4 + topic_size:]
                            self.assertLess(len(ack_bytes), 384)
                            self.assertNotIn(b"\\", ack_bytes)
                            ack = json.loads(ack_bytes)
                            self.assertEqual(ack, {
                                "protocol_version": 2, "message_id": message_id, "message_type": "log",
                                "device": ALIAS, "status": "FAILED" if case == "FAILED" else "SUCCESS",
                                "retryable": False, "error_code": "INVALID_PAYLOAD" if case == "FAILED" else None,
                                "server_timestamp": ack["server_timestamp"],
                            })
                            self.assertTrue(ack["server_timestamp"].endswith("Z"))
                            row = MqttAttempt.objects.get(status=case)
                            self.assertEqual(row.ack_status, "QUEUED")
                            self.assertIsNotNone(row.ack_published_at)
                            self.assertIsNone(row.ack_confirmed_at)
                            connection.sendall(packet(0x40, ack_mid))
                            client.loop_read()
                            row.refresh_from_db()
                            self.assertEqual(row.ack_status, "BROKER_CONFIRMED")
                            self.assertIsNotNone(row.ack_confirmed_at)
        self.assertEqual(DeviceLog.objects.count(), 1)
        self.assertEqual(MqttReceipt.objects.count(), 1)


@override_settings(
    MQTT_PUBLIC_TEST_MODE=True, MQTT_PUBLIC_TEST_DEVICE=ALIAS,
    MQTT_PUBLIC_TEST_LOGICAL_DEVICE="ESP32_001",
)
class MonitoringLocalizationTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser(username="operator"))
        self.instant = datetime(2026, 11, 26, 5, 20, 10, tzinfo=utc_timezone.utc)

    @patch("myapp.views.timezone.now", return_value=datetime(2026, 11, 26, 5, 20, 10, tzinfo=utc_timezone.utc))
    def test_routing_identity_stays_unique_and_vietnam_time_is_displayed(self, now):
        ChargerDevice.objects.create(device_id="ESP32_001")
        ChargerDevice.objects.create(
            device_id=ALIAS, last_seen_at=self.instant,
            last_heartbeat_at=self.instant, mqtt_health={"mode": "HEADLESS", "firmware_version": "1.2.3"},
        )
        for device in (ALIAS, "ESP32_001"):
            MqttAttempt.objects.create(
                device_id=device, received_at=self.instant, message_id="0123456789abcdef-00000001",
                message_type="logs", topic=ROUTE + "/logs", status="SUCCESS", duration_ms=3,
                payload={"event": "WIFI_CONNECTED"},
            )
        dashboard = self.client.get(reverse("myapp:devices_dashboard"))
        self.assertContains(dashboard, "26/11/2026 12:20:10")
        self.assertContains(dashboard, "Ngoại vi chưa kết nối")
        api = self.client.get(reverse("myapp:device_status_api")).json()
        self.assertEqual({item["routing_id"] for item in api["devices"]}, {ALIAS, "ESP32_001"})
        self.assertEqual({item["device_id"] for item in api["devices"]}, {"ESP32_001"})
        alias_row = next(item for item in api["devices"] if item["routing_id"] == ALIAS)
        self.assertEqual(alias_row["last_heartbeat"], "2026-11-26T12:20:10+07:00")
        logs = reverse("myapp:device_logs_dashboard")
        response = self.client.get(logs, {
            "device": ALIAS, "category": "WIFI", "since": "2026-11-26T12:20:10",
            "until": "2026-11-26T12:20:10",
        })
        self.assertEqual(len(response.context["page"]), 1)
        self.assertContains(response, "26/11/2026 12:20:10")
        self.assertContains(response, "WIFI_CONNECTED")
        self.assertEqual(dict(ChargerDevice.objects.values_list("device_id", "last_heartbeat_at"))[ALIAS], self.instant)

