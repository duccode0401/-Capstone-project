from datetime import timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import ChargerDevice, ChargingSession, DeviceLog, DeviceTelemetry, MqttAttempt


@override_settings(MQTT_PUBLIC_TEST_MODE=False, DEVICE_STALE_SECONDS=30)
class DeviceManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("monitor", password="test-pass")
        now = timezone.now()
        cls.devices = []
        for index in range(21):
            device = ChargerDevice.objects.create(
                device_id=f"ESP32_{index:03d}", name=f"Trạm {index:02d}",
                last_seen_at=now if index < 10 else now - timedelta(minutes=5),
            )
            DeviceTelemetry.objects.create(
                device=device, port=1, voltage_v=220, current_a=index,
                power_w=index * 220, energy_total_wh=index, energy_delta_wh=0,
                charging=index == 0, charging_status="CHARGING" if index == 0 else "IDLE",
            )
            cls.devices.append(device)
        cls.a, cls.b = cls.devices[:2]
        for device, marker in ((cls.a, "ONLY-A"), (cls.b, "ONLY-B")):
            DeviceLog.objects.create(device=device, level="INFO", event=marker, message=marker)
            MqttAttempt.objects.create(
                received_at=now, device_id=device.device_id, message_id=f"12345678-{device.pk:08d}",
                message_type="logs", topic="test", payload={"event": marker}, status="SUCCESS",
                ack_status="BROKER_CONFIRMED", duration_ms=4,
            )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_list_has_stats_search_state_and_pagination_for_20_plus_devices(self):
        response = self.client.get(reverse("myapp:devices_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"], {"total": 21, "online": 10, "offline": 11, "charging": 1})
        self.assertEqual(len(response.context["page"]), 20)
        self.assertTrue(response.context["page"].has_next())
        searched = self.client.get(reverse("myapp:devices_dashboard"), {"q": "Trạm 20"})
        self.assertEqual(len(searched.context["page"]), 1)
        offline = self.client.get(reverse("myapp:devices_dashboard"), {"state": "offline"})
        self.assertEqual(offline.context["page"].paginator.count, 11)

    def test_device_detail_queries_only_selected_device(self):
        mqtt = self.client.get(reverse("myapp:device_detail", args=[self.a.pk]), {"tab": "mqtt"})
        self.assertContains(mqtt, "ONLY-A")
        self.assertNotContains(mqtt, "ONLY-B")
        events = self.client.get(reverse("myapp:device_detail", args=[self.a.pk]), {"tab": "events"})
        self.assertContains(events, "ONLY-A")
        self.assertNotContains(events, "ONLY-B")
        self.assertContains(mqtt, "BROKER_CONFIRMED")
        self.assertContains(mqtt, "không phải bằng chứng ESP32")

    def test_filters_and_auto_refresh_keep_query_string(self):
        response = self.client.get(reverse("myapp:device_detail", args=[self.a.pk]), {
            "tab": "mqtt", "type": "logs", "outcome": "SUCCESS", "ack": "BROKER_CONFIRMED",
        })
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertIn("type=logs", response.context["query"])
        self.assertContains(response, 'id="auto-refresh"')

    def test_access_requires_authentication_and_permission(self):
        self.client.logout()
        response = self.client.get(reverse("myapp:device_detail", args=[self.a.pk]))
        self.assertEqual(response.status_code, 302)
        staff = get_user_model().objects.create_user("staff", password="test-pass", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("myapp:device_detail", args=[self.a.pk])).status_code, 403)

    def test_general_log_page_redirects_to_device_selection(self):
        response = self.client.get(reverse("myapp:device_logs_dashboard"))
        self.assertRedirects(response, reverse("myapp:devices_dashboard"))

    def test_admin_branding_and_visible_models(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "Quản trị SmartCharge")
        self.assertContains(response, "Giám sát thiết bị")
        self.assertNotContains(response, "Device logs")
        self.assertNotContains(response, "Device telemetrys")
        self.assertIn(ChargerDevice, admin.site._registry)
        self.assertIn(ChargingSession, admin.site._registry)
        self.assertNotIn(DeviceLog, admin.site._registry)
        self.assertNotIn(DeviceTelemetry, admin.site._registry)
