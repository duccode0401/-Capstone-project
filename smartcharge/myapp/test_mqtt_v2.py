import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt
from django.contrib.auth import get_user_model
from django.db import OperationalError, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mqtt_subscriber import (
    MQTT_TOPIC_HEARTBEAT, MQTT_TOPIC_LOGS, MQTT_TOPIC_STATUS, MQTT_TOPIC_TELEMETRY,
    on_connect, on_message, on_publish,
)

from .models import (
    ChargerDevice, ChargingSession, DeviceLog, DeviceTelemetry, MqttAttempt, MqttReceipt,
)
from .mqtt_config import mqtt_connection, public_test_heartbeat_topic, public_test_topics
from .mqtt_ingestion import cleanup
from .test_web_integration import telemetry
from .views import _device_snapshot


@override_settings(MQTT_PUBLIC_TEST_MODE=False, MQTT_DEVICE_ID="ESP32_001", DEVICE_STALE_SECONDS=30)
class MqttV2Tests(TransactionTestCase):
    def setUp(self):
        self.mqtt = Mock(smartcharge_ack_mids={})
        self.mqtt.is_connected.return_value = True
        self.mid = 0
        self.mqtt.publish.side_effect = self.publish
        self.message_id = "a83f921b-00000001"

    def publish(self, *args, **kwargs):
        self.mid += 1
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, mid=self.mid)

    def receive(self, topic, data, *, retain=False):
        on_message(self.mqtt, None, SimpleNamespace(
            topic=topic, payload=json.dumps(data).encode(), retain=retain,
        ))

    def envelope(self, kind, **data):
        return {
            "protocol_version": 2, "device": "ESP32_001", "message_id": self.message_id,
            "message_type": kind, "uptime_ms": 10000, **data,
        }

    def heartbeat(self, **data):
        return {**self.envelope(
            "heartbeat", firmware_version="1.0.0", free_heap=125632, min_free_heap=110000,
            rssi=-45, mqtt_connected=True, relay_on=False, sensor_status="DISABLED",
            display_status="DISABLED", dropped_messages=0, reset_reason=1, mode="HEADLESS",
        ), **data}

    def ack(self, *, topic="iot/charge_station/v1/ack/ESP32_001", error=None, retryable=False):
        call = self.mqtt.publish.call_args
        self.assertEqual(call.args[0], topic)
        self.assertEqual(call.kwargs, {"qos": 1, "retain": False})
        data = json.loads(call.args[1])
        self.assertEqual(set(data), {
            "protocol_version", "device", "message_id", "message_type",
            "status", "error_code", "retryable", "server_timestamp",
        })
        self.assertEqual(data["protocol_version"], 2)
        self.assertEqual(data["device"], topic.split("/")[-2 if topic.endswith("/ack") else -1])
        self.assertEqual(data["message_id"], self.message_id)
        self.assertEqual(data["error_code"], error)
        self.assertEqual(data["status"], "FAILED" if error else "SUCCESS")
        self.assertEqual(data["retryable"], retryable)
        self.assertTrue(data["server_timestamp"].endswith("Z"))
        return data

    def test_heartbeat_health_liveness_and_puback(self):
        with transaction.atomic():
            self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
            self.mqtt.publish.assert_not_called()
            self.assertEqual(MqttAttempt.objects.get().ack_status, "NOT_SENT")
        self.assertEqual(self.ack()["message_type"], "heartbeat")
        self.assertEqual(MqttAttempt.objects.get().ack_status, "QUEUED")
        on_publish(self.mqtt, None, self.mid, SimpleNamespace(is_failure=False), None)
        self.assertEqual(MqttAttempt.objects.get().ack_status, "BROKER_CONFIRMED")
        device = ChargerDevice.objects.get()
        DeviceTelemetry.objects.create(
            device=device, port=1, voltage_v=0, current_a=0, power_w=0,
            energy_total_wh=0, energy_delta_wh=0, charging=False, charging_status="IDLE",
            timestamp=timezone.now(),
        )
        current = _device_snapshot(device, device.last_heartbeat_at)
        self.assertTrue(current["online"])
        self.assertIsNone(current["voltage_v"])
        self.assertEqual((current["minimum_heap"], current["rssi_dbm"]), (110000, -45))
        self.assertEqual(device.mqtt_success_count, 1)
        self.assertFalse(_device_snapshot(device, device.last_heartbeat_at + timedelta(seconds=31))["online"])

    def test_telemetry_duplicate_and_conflict(self):
        original = self.envelope("telemetry", **telemetry())
        self.receive(MQTT_TOPIC_TELEMETRY, original)
        self.ack()
        self.receive(MQTT_TOPIC_TELEMETRY, original)
        self.ack()
        self.receive(MQTT_TOPIC_TELEMETRY, dict(original, voltage_v=2))
        self.ack(error="MESSAGE_ID_CONFLICT")
        self.assertEqual(DeviceTelemetry.objects.count(), 1)
        self.assertEqual(MqttReceipt.objects.count(), 1)
        self.assertEqual(set(MqttAttempt.objects.values_list("status", flat=True)), {
            "SUCCESS", "FAILED", "DUPLICATE",
        })
        d = ChargerDevice.objects.get()
        self.assertEqual((d.mqtt_success_count, d.mqtt_failed_count, d.mqtt_duplicate_count), (1, 1, 1))

    def test_log_payload_is_sanitized_and_legacy_has_no_ack(self):
        payload = self.envelope("logs", level="INFO", event="BOOT",
                                message="HMAC my-key", token="private-token", payment={"code": "private-code"})
        self.receive(MQTT_TOPIC_LOGS, payload)
        self.ack()
        self.assertEqual(DeviceLog.objects.count(), 1)
        self.assertIn("REDACTED", DeviceLog.objects.get().message)
        audit = MqttAttempt.objects.get()
        self.assertEqual(audit.payload["message"], "[REDACTED]")
        self.assertNotIn("token", audit.payload)
        self.assertNotIn("payment", audit.payload)
        self.mqtt.reset_mock()
        self.receive(MQTT_TOPIC_LOGS, {"device": "ESP32_001", "level": "INFO", "event": "BOOT", "message": "Relay OFF"})
        self.mqtt.publish.assert_not_called()
        self.assertEqual(DeviceLog.objects.count(), 2)

    def test_database_rollback_allows_retry(self):
        from mqtt_subscriber import _dispatch

        def fail_after_writing(topic, data):
            _dispatch(topic, data)
            raise OperationalError("simulated rollback")

        payload = self.envelope("telemetry", **telemetry())
        with patch("mqtt_subscriber._dispatch", side_effect=fail_after_writing):
            self.receive(MQTT_TOPIC_TELEMETRY, payload)
        self.ack(error="DATABASE_ERROR", retryable=True)
        self.assertFalse(MqttReceipt.objects.exists())
        self.assertFalse(DeviceTelemetry.objects.exists())
        self.assertFalse(ChargerDevice.objects.exists())
        self.receive(MQTT_TOPIC_TELEMETRY, payload)
        self.ack()
        self.assertEqual(DeviceTelemetry.objects.count(), 1)

    def test_outer_rollback_never_emits_success(self):
        with transaction.atomic():
            self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
            transaction.set_rollback(True)
        self.mqtt.publish.assert_not_called()
        self.assertFalse(MqttAttempt.objects.exists())
        self.assertFalse(MqttReceipt.objects.exists())

    def test_ack_publish_failure_preserves_commit_and_duplicate_can_recover(self):
        self.mqtt.publish.side_effect = [
            SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN, mid=1),
            SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, mid=2),
        ]
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
        self.assertEqual(MqttAttempt.objects.get().ack_status, "PUBLISH_FAILED")
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
        self.ack()
        on_publish(self.mqtt, None, 2, SimpleNamespace(is_failure=True), None)
        self.assertEqual(MqttAttempt.objects.get(status="DUPLICATE").ack_status, "BROKER_REJECTED")
        self.assertEqual(MqttReceipt.objects.count(), 1)

    def test_invalid_schema_device_topic_id_and_protocol(self):
        for overrides, error in [
            ({"relay_on": "false"}, "INVALID_PAYLOAD"),
            ({"message_type": "telemetry"}, "INVALID_MESSAGE_TYPE"),
            ({"device": "unknown"}, "UNAUTHORIZED_DEVICE"),
            ({"protocol_version": 3}, "UNSUPPORTED_PROTOCOL"),
        ]:
            with self.subTest(overrides=overrides):
                self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(**overrides))
                self.ack(topic=f"iot/charge_station/v1/ack/{overrides.get('device', 'ESP32_001')}", error=error)
        self.mqtt.reset_mock()
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(device="bad/+/device"))
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(message_id="bad/id"))
        self.receive("iot/charge_station/v1/ack/ESP32_001", self.heartbeat())
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(message_type="ack"))
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(), retain=True)
        self.mqtt.publish.assert_not_called()
        self.assertFalse(MqttReceipt.objects.exists())
        self.assertEqual(set(MqttAttempt.objects.values_list("error_code", flat=True)), {
            "INVALID_PAYLOAD", "INVALID_MESSAGE_TYPE", "UNAUTHORIZED_DEVICE",
            "UNSUPPORTED_PROTOCOL", "INVALID_DEVICE", "INVALID_MESSAGE_ID",
        })

    def test_duplicate_done_cannot_finish_a_new_charging_session(self):
        charger = ChargerDevice.objects.create(device_id="ESP32_001")
        first = ChargingSession.objects.create(device=charger, port_no=1, amount_vnd=10000, status="charging")
        done = self.envelope("status", status="DONE", port=1)
        self.receive(MQTT_TOPIC_STATUS, done)
        first.refresh_from_db()
        self.assertEqual(first.status, "done")
        second = ChargingSession.objects.create(device=charger, port_no=1, amount_vnd=10000, status="charging")
        self.receive(MQTT_TOPIC_STATUS, done)
        second.refresh_from_db()
        self.assertEqual(second.status, "charging")
        self.ack()

    @override_settings(MQTT_PUBLIC_TEST_MODE=True, MQTT_PUBLIC_TEST_DEVICE="SC_TEST_0123456789abcdef")
    def test_public_alias_ack_and_reconnect(self):
        alias = "SC_TEST_0123456789abcdef"
        topic = public_test_heartbeat_topic()
        self.mqtt.subscribe.return_value = (0, 1)
        for _ in range(2):
            on_connect(self.mqtt, None, None, 0, None)
        self.assertEqual(self.mqtt.subscribe.call_count, 2)
        self.mqtt.subscribe.assert_called_with([(t, 1) for t in (*public_test_topics(), topic)])
        self.assertEqual(mqtt_connection(subscribe_only=True)["port"], 8883)
        self.receive(topic, self.heartbeat())
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat(device=alias))
        self.mqtt.publish.assert_not_called()
        self.receive(topic, self.heartbeat(device=alias, logical_device="ESP32_002"))
        self.ack(topic=topic.rsplit("/", 1)[0] + "/ack", error="INVALID_LOGICAL_DEVICE")
        self.mqtt.reset_mock()
        self.receive(topic, self.heartbeat(device=alias, logical_device="ESP32_001"))
        self.ack(topic=topic.rsplit("/", 1)[0] + "/ack")
        self.assertEqual(ChargerDevice.objects.get().device_id, alias)
        self.assertFalse(ChargingSession.objects.exists())

    def test_retention_keeps_receipts_longer_than_logs(self):
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
        now = timezone.now()
        MqttReceipt.objects.update(last_seen_at=now - timedelta(days=10))
        MqttAttempt.objects.update(received_at=now - timedelta(days=10))
        cleanup()
        self.assertTrue(MqttReceipt.objects.exists())
        self.assertFalse(MqttAttempt.objects.exists())
        MqttReceipt.objects.update(last_seen_at=now - timedelta(days=31))
        cleanup()
        self.assertFalse(MqttReceipt.objects.exists())

    def test_dashboard_and_filtered_logs_require_staff_permission(self):
        self.receive(MQTT_TOPIC_HEARTBEAT, self.heartbeat())
        logs = reverse("myapp:device_logs_dashboard")
        dashboard = reverse("myapp:devices_dashboard")
        self.assertEqual(self.client.get(logs).status_code, 302)
        user = get_user_model().objects.create_superuser(username="monitor")
        self.client.force_login(user)
        response = self.client.get(logs, {"device": "ESP32_001", "type": "heartbeat", "outcome": "SUCCESS"})
        self.assertContains(response, self.message_id)
        self.assertRedirects(
            self.client.get(logs, {"outcome": "DUPLICATE"}),
            reverse("myapp:devices_dashboard"),
        )
        self.assertContains(self.client.get(dashboard), "HEADLESS")
