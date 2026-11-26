from datetime import timedelta
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mqtt_subscriber import MQTT_TOPIC_STATUS, on_connect, on_message

from .charging_protocol import signed_start_charging
from .iot_services import process_telemetry
from .models import ChargerDevice, ChargerPort, ChargingSession, DeviceTelemetry, PaymentOrder
from .sepay import dispatch_paid_orders


def telemetry(device="ESP32_001", *, charging=False):
    return {
        "device": device, "port": 1, "voltage_v": 0.12, "current_a": 0, "power_w": 0,
        "energy_total_wh": 0, "energy_delta_wh": 0, "interval_s": 10, "charging": charging,
        "status": "CHARGING" if charging else "IDLE",
    }


@override_settings(
    MQTT_PUBLIC_TEST_MODE=False, MQTT_BROKER="mqtt.example.test", MQTT_USERNAME="backend",
    MQTT_PASSWORD="test-password", SEPAY_WEBHOOK_API_KEY="test-webhook",
    SEPAY_ACCOUNT_NUMBER="12345", SEPAY_BANK="TCB",
    ALLOWED_HOSTS=["testserver", "firmware.example.test"],
)
class SePayWebhookTests(TestCase):
    def setUp(self):
        other = ChargerDevice.objects.create(device_id="ESP32_000")
        ChargerPort.objects.create(device=other, port_no=1)
        self.device = ChargerDevice.objects.create(device_id="ESP32_001")
        self.port = ChargerPort.objects.create(device=self.device, port_no=1)
        self.order = PaymentOrder.objects.create(
            code="SC0123456789ABCDEF", port=self.port, amount_vnd=10000,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.webhook = reverse("myapp:sepay_webhook")
        self.payload = {"id": 123, "code": self.order.code, "transferType": "in",
                        "accountNumber": "12345", "transferAmount": 10000}

    def post_webhook(self, **changes):
        authorization = changes.pop("authorization", "Apikey test-webhook")
        return self.client.post(self.webhook, data=dict(self.payload, **changes),
                                content_type="application/json", HTTP_AUTHORIZATION=authorization,
                                HTTP_HOST="firmware.example.test", HTTP_X_FORWARDED_PROTO="https")

    def test_verified_webhook_assigns_selected_port_and_sends_once(self):
        self.assertEqual(self.post_webhook().json(), {"success": True})
        self.assertEqual(self.post_webhook().json(), {"success": True})
        self.assertEqual(ChargingSession.objects.count(), 1)
        with patch("myapp.views.publish_once") as publish:
            dispatch_paid_orders()
            dispatch_paid_orders()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args, ("iot/charge_station/v1/orders", "ESP32_001 1 10000"))
        self.assertFalse(publish.call_args.kwargs["retain"])
        session = ChargingSession.objects.get()
        self.assertEqual((session.device, session.port_no, session.status), (self.device, 1, "assigned"))

        process_telemetry(telemetry(charging=True))
        session.refresh_from_db()
        self.assertEqual(session.status, "charging")
        message = SimpleNamespace(topic=MQTT_TOPIC_STATUS, retain=False,
                                  payload=b'{"device":"ESP32_001","port":1,"status":"DONE"}')
        on_message(None, None, message)
        on_message(None, None, message)
        session.refresh_from_db()
        self.assertEqual(session.status, "done")

    def test_rejects_forged_and_mismatched_webhooks(self):
        self.assertEqual(self.post_webhook(authorization="wrong").status_code, 401)
        self.assertEqual(self.post_webhook(transferAmount=9999).status_code, 400)
        self.assertEqual(self.post_webhook(accountNumber="99999").status_code, 400)
        self.assertEqual(self.post_webhook(transferType="out").status_code, 400)
        self.assertFalse(ChargingSession.objects.exists())
        self.assertEqual(self.post_webhook().status_code, 200)
        self.assertEqual(self.post_webhook(id=124).status_code, 409)
        other = PaymentOrder.objects.create(
            code="SCFEDCBA9876543210", port=self.port, amount_vnd=10000,
            expires_at=self.order.expires_at,
        )
        self.assertEqual(self.post_webhook(code=other.code).status_code, 409)
        self.assertEqual(ChargingSession.objects.count(), 1)

    def test_uncertain_publish_is_not_retried(self):
        self.assertEqual(self.post_webhook().status_code, 200)
        with patch("myapp.views.publish_once", side_effect=TimeoutError) as publish:
            dispatch_paid_orders()
            dispatch_paid_orders()
        publish.assert_called_once()
        self.assertTrue(ChargingSession.objects.get().mqtt_start_delivery_unknown)

    def test_checkout_qr_contains_order_code_and_selected_port(self):
        response = self.client.post(reverse("myapp:payment_order"), {"amount": 20000, "port": self.port.pk})
        self.assertEqual(response.status_code, 302)
        order = PaymentOrder.objects.get(amount_vnd=20000)
        response = self.client.get(response.url)
        self.assertContains(response, f"des={order.code}")
        self.assertEqual(order.port_id, self.port.pk)

    @override_settings(MQTT_PUBLIC_TEST_MODE=True)
    def test_public_mode_cannot_create_orders_or_apply_webhooks(self):
        self.assertEqual(self.post_webhook().status_code, 404)
        self.assertEqual(self.client.get(reverse("myapp:payment_order")).status_code, 404)
        self.assertFalse(ChargingSession.objects.exists())


@override_settings(MQTT_PUBLIC_TEST_MODE=True, MQTT_PUBLIC_TEST_DEVICE="")
class PublicAliasRequiredTests(TestCase):
    def test_blank_alias_refuses_public_subscription(self):
        from django.core.exceptions import ValidationError

        mqtt_client = Mock()
        with self.assertRaises(ValidationError):
            on_connect(mqtt_client, None, None, 0, None)
        mqtt_client.subscribe.assert_not_called()
