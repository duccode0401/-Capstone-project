from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse


@override_settings(DEBUG=False, DJANGO_LAN_MODE=True, ALLOWED_HOSTS=["localhost", "127.0.0.1", "ota.example.test"])
class LanWebTests(TestCase):
    def test_login_dashboard_logout_and_accept_only_current_lan_host(self):
        get_user_model().objects.create_superuser(username="lan-admin", password="test-pass")
        client = Client(enforce_csrf_checks=True)
        host = "192.168.20.15"
        url = f"http://{host}:8000"
        login = reverse("admin:login")
        with (
            patch("myapp.lan._previous", set()),
            patch("myapp.lan._refresh_after", 0),
            patch("myapp.lan.lan_addresses", return_value=[host]) as addresses,
            patch("myapp.lan.monotonic", return_value=20) as clock,
        ):
            self.assertEqual(client.get(login, HTTP_HOST=f"{host}:8000").status_code, 200)
            csrf = client.cookies["csrftoken"].value
            self.assertEqual(client.post(login, {
                "username": "lan-admin", "password": "test-pass", "csrfmiddlewaretoken": csrf,
            }, HTTP_HOST=f"{host}:8000", HTTP_ORIGIN=url).status_code, 302)
            dashboard = reverse("myapp:devices_dashboard")
            self.assertEqual(client.get(dashboard, HTTP_HOST=f"{host}:8000").status_code, 200)
            csrf = client.cookies["csrftoken"].value
            self.assertEqual(client.post(reverse("admin:logout"), {
                "csrfmiddlewaretoken": csrf,
            }, HTTP_HOST=f"{host}:8000", HTTP_ORIGIN=url).status_code, 200)
            self.assertEqual(client.get(dashboard, HTTP_HOST=f"{host}:8000").status_code, 302)

            addresses.return_value = ["10.20.30.40"]
            clock.return_value = 23
            self.assertEqual(client.get(login, HTTP_HOST=f"{host}:8000").status_code, 400)
            self.assertEqual(client.get(login, HTTP_HOST="10.20.30.40:8000").status_code, 200)
            self.assertEqual(client.get(login, HTTP_HOST="outside.example.test").status_code, 400)
            self.assertEqual(client.get(login, HTTP_HOST="localhost:8000").status_code, 200)
