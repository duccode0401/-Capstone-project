from unittest.mock import Mock

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from mqtt_subscriber import PUBLIC_TEST_EVENTS, _dispatch_public_test
from .models import FirmwareArtifact, OTAJob, OTAProgressEvent
from .mqtt_config import mqtt_connection, public_test_topics


PUBLIC = {
    "MQTT_PUBLIC_TEST_MODE": True,
    "MQTT_PUBLIC_TEST_DEVICE": "SC_TEST_0123456789abcdef",
    "MQTT_PUBLIC_TEST_LOGICAL_DEVICE": "ESP32_001",
    "MQTT_BROKER": "broker.emqx.io",
    "MQTT_PORT": 8883,
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "MQTT_CA_FILE": "",
}


@override_settings(**PUBLIC)
class PublicArchitectureTests(TestCase):
    def test_broker_and_per_device_topics_are_configurable(self):
        config = mqtt_connection(subscribe_only=True)
        self.assertEqual((config["hostname"], config["port"]), ("broker.emqx.io", 8883))
        self.assertEqual(public_test_topics(), (
            "iot/charge_station/test/SC_TEST_0123456789abcdef/telemetry",
            "iot/charge_station/test/SC_TEST_0123456789abcdef/logs",
        ))

    def test_public_messages_cannot_report_charging_or_done(self):
        telemetry, _ = public_test_topics()
        with self.assertRaises(ValidationError):
            _dispatch_public_test(telemetry, {
                "device": "SC_TEST_0123456789abcdef", "charging": True,
                "status": "CHARGING",
            })

    def test_current_firmware_events_are_allowlisted(self):
        emitted = {
            "BOOT", "CHARGE_COMPLETED", "CHARGING_STARTED", "CHARGING_STOPPED", "HEALTHY",
            "HEAP_LOW", "MQTT_CONNECTED", "MQTT_DISCONNECTED", "MQTT_ERROR",
            "PAYMENT_ACCEPTED", "PAYMENT_REJECTED", "REBOOT_REASON", "RELAY_OFF", "RELAY_ON",
            "SENSOR_ERROR", "WATCHDOG_RESET", "WIFI_CONNECTED", "WIFI_DISCONNECTED",
        }
        self.assertLessEqual(emitted, PUBLIC_TEST_EVENTS.keys())

    @override_settings(MQTT_BROKER="broker.hivemq.com", MQTT_PORT=8883)
    def test_public_mode_requires_emqx_tls_endpoint(self):
        with self.assertRaisesMessage(ValidationError, "broker.emqx.io:8883"):
            mqtt_connection(subscribe_only=True)

    def test_ota_web_and_admin_are_removed_but_legacy_models_remain(self):
        with self.assertRaises(NoReverseMatch):
            reverse("myapp:ota_dashboard")
        self.assertNotIn(FirmwareArtifact, admin.site._registry)
        self.assertNotIn(OTAJob, admin.site._registry)
        self.assertEqual(OTAProgressEvent._meta.db_table, "myapp_otaprogressevent")


class SecureModeTests(SimpleTestCase):
    @override_settings(MQTT_PUBLIC_TEST_MODE=False, MQTT_BROKER="broker.emqx.io", MQTT_PORT=8883,
                       MQTT_USERNAME="backend", MQTT_PASSWORD="secret", MQTT_CA_FILE="")
    def test_public_broker_is_rejected_for_operational_control(self):
        with self.assertRaisesMessage(ValidationError, "private MQTT TLS broker"):
            mqtt_connection()
