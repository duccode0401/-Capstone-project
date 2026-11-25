from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import hmac
import json
import struct
import tempfile
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mqtt_subscriber import (
    MQTT_TOPIC_LOGS,
    MQTT_TOPIC_OTA_STATUS,
    MQTT_TOPIC_TELEMETRY,
    mark_session_done,
    on_connect,
    on_message,
)

from .iot_services import process_device_log, process_telemetry
from .models import (
    ChargerDevice,
    ChargerPort,
    ChargingSession,
    DeviceLog,
    DeviceTelemetry,
    FirmwareArtifact,
    OTAJob,
)
from .ota_services import _probe_https_release, create_and_issue_ota, process_ota_status
from .views import create_session_after_payment


MQTT_TEST_SETTINGS = {
    "MQTT_BROKER": "mqtt.example.com",
    "MQTT_USERNAME": "backend",
    "MQTT_PASSWORD": "test-password",
}


@override_settings(**MQTT_TEST_SETTINGS)
class PaymentIdempotencyTests(TestCase):
    def setUp(self):
        self.device = ChargerDevice.objects.create(device_id="ESP32_001")
        ChargerPort.objects.create(device=self.device, port_no=1)

    @staticmethod
    def sepay_response(transaction_id="tx-100"):
        response = Mock()
        response.json.return_value = {"transactions": [{"id": transaction_id}]}
        return response

    @patch("myapp.views.mqtt_publish.single")
    @patch("myapp.views.requests.get")
    def test_successful_payment_creates_one_session(self, request_get, mqtt_single):
        request_get.return_value = self.sepay_response()

        response = self.client.get(reverse("myapp:check_payment"), {"amount": 20000})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChargingSession.objects.count(), 1)
        session = ChargingSession.objects.get()
        self.assertEqual(session.sepay_transaction_id, "tx-100")
        self.assertEqual(session.status, "assigned")
        mqtt_single.assert_called_once()

    @patch("myapp.views.mqtt_publish.single")
    @patch("myapp.views.requests.get")
    def test_repeated_poll_returns_same_session_without_duplicate_start(
        self, request_get, mqtt_single
    ):
        request_get.return_value = self.sepay_response()

        first = self.client.get(reverse("myapp:check_payment"), {"amount": 20000})
        second = self.client.get(reverse("myapp:check_payment"), {"amount": 20000})

        self.assertEqual(first.json()["session"]["id"], second.json()["session"]["id"])
        self.assertEqual(ChargingSession.objects.count(), 1)
        mqtt_single.assert_called_once()

    def test_two_handlers_for_same_transaction_do_not_duplicate_session(self):
        first = create_session_after_payment(20000, "tx-concurrent")
        second = create_session_after_payment(20000, "tx-concurrent")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            ChargingSession.objects.filter(
                sepay_transaction_id="tx-concurrent"
            ).count(),
            1,
        )

    def test_database_unique_constraint_is_final_concurrency_guard(self):
        create_session_after_payment(20000, "tx-unique")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChargingSession.objects.create(
                amount_vnd=20000,
                status="paid",
                sepay_transaction_id="tx-unique",
            )

    @patch("myapp.views.mqtt_publish.single")
    @patch("myapp.views.requests.get")
    def test_each_transaction_in_one_sepay_response_is_processed_once(
        self, request_get, mqtt_single
    ):
        response = Mock()
        response.json.return_value = {
            "transactions": [{"id": "tx-a"}, {"id": "tx-b"}]
        }
        request_get.return_value = response

        self.client.get(reverse("myapp:check_payment"), {"amount": 20000})
        self.client.get(reverse("myapp:check_payment"), {"amount": 20000})

        self.assertEqual(ChargingSession.objects.count(), 2)
        self.assertSetEqual(
            set(
                ChargingSession.objects.values_list(
                    "sepay_transaction_id", flat=True
                )
            ),
            {"tx-a", "tx-b"},
        )
        mqtt_single.assert_called_once()

    @patch("myapp.views.mqtt_publish.single")
    @patch("myapp.views.requests.get")
    def test_no_port_leaves_session_waiting_and_does_not_publish(
        self, request_get, mqtt_single
    ):
        ChargerPort.objects.all().delete()
        request_get.return_value = self.sepay_response("tx-waiting")

        response = self.client.get(reverse("myapp:check_payment"), {"amount": 30000})

        self.assertEqual(response.json()["session"]["status"], "waiting")
        mqtt_single.assert_not_called()

    def test_legacy_session_without_transaction_id_is_preserved(self):
        legacy = ChargingSession.objects.create(amount_vnd=10000, status="done")

        create_session_after_payment(20000, "tx-new")

        self.assertTrue(ChargingSession.objects.filter(pk=legacy.pk).exists())
        legacy.refresh_from_db()
        self.assertIsNone(legacy.sepay_transaction_id)


class MqttDoneTests(TestCase):
    def setUp(self):
        self.device = ChargerDevice.objects.create(device_id="ESP32_001")
        self.port = ChargerPort.objects.create(device=self.device, port_no=1)

    def active_session(self, transaction_id="tx-active", status="assigned"):
        return ChargingSession.objects.create(
            device=self.device,
            port_no=1,
            amount_vnd=20000,
            status=status,
            sepay_transaction_id=transaction_id,
        )

    def test_done_updates_assigned_session(self):
        session = self.active_session()

        result = mark_session_done("ESP32_001", 1)

        session.refresh_from_db()
        self.assertEqual(result.pk, session.pk)
        self.assertEqual(session.status, "done")

    def test_done_updates_charging_session(self):
        session = self.active_session(status="charging")
        mark_session_done("ESP32_001", 1)
        session.refresh_from_db()
        self.assertEqual(session.status, "done")

    def test_duplicate_done_is_safe_and_creates_nothing(self):
        session = self.active_session()
        mark_session_done("ESP32_001", 1)

        result = mark_session_done("ESP32_001", 1)

        self.assertIsNone(result)
        self.assertEqual(ChargingSession.objects.count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, "done")

    def test_invalid_device_or_port_does_not_update_history(self):
        session = self.active_session()

        self.assertIsNone(mark_session_done("ESP32_OTHER", 1))
        self.assertIsNone(mark_session_done("ESP32_001", 2))

        session.refresh_from_db()
        self.assertEqual(session.status, "assigned")

    def test_done_frees_port_for_next_session(self):
        first = self.active_session()
        mark_session_done("ESP32_001", 1)

        second = create_session_after_payment(30000, "tx-next")

        first.refresh_from_db()
        self.assertEqual(first.status, "done")
        self.assertEqual(second.status, "assigned")
        self.assertEqual(second.device_id, self.device.id)
        self.assertEqual(second.port_no, 1)


class ConcurrentPaymentTests(TransactionTestCase):
    reset_sequences = True

    def test_two_simultaneous_handlers_create_one_session(self):
        barrier = Barrier(2)

        def handle_payment():
            close_old_connections()
            try:
                barrier.wait()
                return create_session_after_payment(20000, "tx-simultaneous").pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            session_ids = list(executor.map(lambda _: handle_payment(), range(2)))

        self.assertEqual(session_ids[0], session_ids[1])
        self.assertEqual(
            ChargingSession.objects.filter(
                sepay_transaction_id="tx-simultaneous"
            ).count(),
            1,
        )


class MigrationPreservesSessionsTests(TransactionTestCase):
    migrate_from = ("myapp", "0004_alter_chargingsession_status")
    migrate_to = ("myapp", "0007_otajob_one_active_ota_job_per_device")

    def test_existing_session_survives_migration(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        OldSession = old_apps.get_model("myapp", "ChargingSession")
        old_session = OldSession.objects.create(amount_vnd=15000, status="done")

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        NewSession = new_apps.get_model("myapp", "ChargingSession")
        migrated = NewSession.objects.get(pk=old_session.pk)

        self.assertEqual(migrated.amount_vnd, 15000)
        self.assertEqual(migrated.status, "done")
        self.assertIsNone(migrated.sepay_transaction_id)
        self.assertIsNone(migrated.mqtt_start_sent_at)


def esp_idf_image(version="1.1.0", project="smartcharge", chip_id=0, size=512):
    image = bytearray(size)
    image[0] = 0xE9
    image[1] = 1
    struct.pack_into("<H", image, 12, chip_id)
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48 : 48 + len(version)] = version.encode("ascii")
    image[80 : 80 + len(project)] = project.encode("ascii")
    return bytes(image)


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class FirmwareUploadTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="firmware-admin", password="test", email="admin@example.com"
        )
        self.client.force_login(self.admin)

    def artifact(self, content=None, **overrides):
        values = {
            "version": "1.1.0",
            "file": SimpleUploadedFile(
                "smartcharge.bin", content or esp_idf_image(),
                content_type="application/octet-stream",
            ),
            "firmware_type": FirmwareArtifact.TYPE_MAIN,
            "chip_target": "esp32",
        }
        values.update(overrides)
        return FirmwareArtifact(**values)

    def test_valid_esp_idf_image_records_size_and_sha256(self):
        artifact = self.artifact()
        artifact.full_clean()
        artifact.save()
        self.assertEqual(artifact.file_size, 512)
        self.assertEqual(len(artifact.sha256), 64)

    def test_invalid_magic_and_wrong_chip_are_rejected(self):
        invalid = self.artifact(content=b"x" * 512)
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        wrong_chip = self.artifact(content=esp_idf_image(chip_id=5))
        with self.assertRaises(ValidationError):
            wrong_chip.full_clean()

    def test_embedded_version_and_project_are_checked(self):
        for image in (
            esp_idf_image(version="1.0.0"),
            esp_idf_image(project="recovery"),
            esp_idf_image(version="65536.0.0"),
        ):
            with self.subTest(image=image[48:112]), self.assertRaises(ValidationError):
                self.artifact(content=image).full_clean()

    def test_main_firmware_partition_limit_is_enforced(self):
        oversized = self.artifact(content=esp_idf_image(size=0x2F0000))
        with self.assertRaises(ValidationError):
            oversized.full_clean()

    def test_admin_upload_accepts_valid_and_rejects_arbitrary_bin(self):
        url = reverse("admin:myapp_firmwareartifact_add")
        valid = self.client.post(url, {
            "version": "1.2.0", "firmware_type": "main", "chip_target": "esp32",
            "description": "release", "is_approved": "on",
            "file": SimpleUploadedFile("release.bin", esp_idf_image(version="1.2.0")),
        })
        self.assertEqual(valid.status_code, 302)
        self.assertTrue(FirmwareArtifact.objects.filter(version="1.2.0").exists())
        invalid = self.client.post(url, {
            "version": "1.3.0", "firmware_type": "main", "chip_target": "esp32",
            "description": "bad", "file": SimpleUploadedFile("bad.bin", b"X" * 512),
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(FirmwareArtifact.objects.filter(version="1.3.0").exists())


OTA_TEST_SETTINGS = {
    "FIRMWARE_HTTPS_BASE_URL": "https://ota.example.com",
    "OTA_DEVICE_KEYS_JSON": json.dumps({"ESP32_001": "11" * 32}),
    **MQTT_TEST_SETTINGS,
    "DEVICE_STALE_SECONDS": 30,
}


@override_settings(MEDIA_ROOT=tempfile.gettempdir(), **OTA_TEST_SETTINGS)
class OTAWorkflowTests(TestCase):
    def setUp(self):
        self.probe = self.enterContext(patch('myapp.ota_services._probe_https_release'))
        self.user = get_user_model().objects.create_user(
            username="ota-admin", password="test", is_staff=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="deploy_firmware")
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.device = ChargerDevice.objects.create(
            device_id="ESP32_001", last_seen_at=timezone.now()
        )
        self.telemetry = DeviceTelemetry.objects.create(
            device=self.device, port=1, voltage_v=229, current_a=0,
            power_w=0, energy_total_wh=25, energy_delta_wh=0,
            charging=False, charging_status="IDLE",
        )
        self.firmware = FirmwareArtifact(
            version="1.1.0",
            file=SimpleUploadedFile("app.bin", esp_idf_image()),
            firmware_type="main", chip_target="esp32", is_approved=True,
        )
        self.firmware.full_clean()
        self.firmware.save()

    def issue_ota(self):
        with self.captureOnCommitCallbacks(execute=True):
            return create_and_issue_ota(self.device, self.firmware, self.user)

    @patch("myapp.ota_services.publish_once")
    def test_create_job_publishes_signed_command_once(self, publish):
        job, command = self.issue_ota()
        self.assertEqual(job.status, "PENDING")
        self.assertEqual(command["device"], "ESP32_001")
        self.assertEqual(command["seq"], "1")
        canonical = ("SC-CMD-1\nESP32_001\nSTART_OTA\n"
                     f"{job.request_id}\n1.1.0\n{command['manifest_url']}\n1")
        self.assertEqual(command["signature"], hmac.new(bytes.fromhex("11" * 32), canonical.encode(), "sha256").hexdigest())
        self.assertEqual(publish.call_args.args[0], "iot/charge_station/v1/ota/command")
        self.assertEqual(json.loads(publish.call_args.args[1]), command)
        self.assertFalse(publish.call_args.kwargs["retain"])
        self.assertTrue(publish.call_args.kwargs["tls"].check_hostname)
        publish.assert_called_once()
        self.assertEqual(OTAJob.objects.count(), 1)
        with self.assertRaises(ValidationError):
            self.issue_ota()
        publish.assert_called_once()

    @patch("myapp.ota_services.publish_once")
    def test_reject_ota_while_charging(self, publish):
        ChargingSession.objects.create(
            device=self.device, port_no=1, amount_vnd=10000, status="charging"
        )
        with self.assertRaises(ValidationError):
            self.issue_ota()
        publish.assert_not_called()

    @patch("myapp.ota_services.publish_once")
    def test_reject_ota_when_offline_or_unknown(self, publish):
        self.device.last_seen_at = timezone.now() - timedelta(minutes=5)
        self.device.save(update_fields=["last_seen_at"])
        with self.assertRaises(ValidationError):
            self.issue_ota()
        publish.assert_not_called()

    @patch("myapp.ota_services.publish_once")
    def test_ota_status_matches_request_and_duplicate_is_safe(self, publish):
        job, _ = self.issue_ota()
        payload = {
            "device": "ESP32_001", "request_id": job.request_id,
            "status": "ACCEPTED", "progress": 0, "version": "1.1.0",
        }
        updated, changed = process_ota_status(payload)
        self.assertTrue(changed)
        self.assertEqual(updated.status, "ACCEPTED")
        _, changed = process_ota_status(payload)
        self.assertFalse(changed)
        payload["request_id"] = "ota-wrong"
        with self.assertRaises(ValidationError):
            process_ota_status(payload)

    @patch("myapp.ota_services.publish_once")
    def test_manifest_and_range_download_require_job_token(self, publish):
        job, command = self.issue_ota()
        token = command["manifest_url"].rstrip("/").split("/")[-1]
        manifest = self.client.get(
            reverse("myapp:firmware_manifest", args=[job.request_id, token])
        )
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["sha256"], self.firmware.sha256)
        self.assertEqual(int(manifest["Content-Length"]), len(manifest.content))
        data = manifest.json()
        canonical = (f"SC-MANIFEST-1\nESP32_001\n{job.request_id}\n1.1.0\n"
                     f"{data['url']}\n{data['size']}\n{data['sha256']}\nesp32\n1")
        self.assertEqual(data["signature"], hmac.new(bytes.fromhex("11" * 32), canonical.encode(), "sha256").hexdigest())
        binary = self.client.get(reverse("myapp:firmware_download", args=[job.request_id, token]))
        self.assertEqual(binary.status_code, 200)
        self.assertEqual(binary["Content-Type"], "application/octet-stream")
        self.assertEqual(int(binary["Content-Length"]), data["size"])
        self.assertEqual(hashlib.sha256(b"".join(binary.streaming_content)).hexdigest(), data["sha256"])
        partial = self.client.get(
            reverse("myapp:firmware_download", args=[job.request_id, token]),
            HTTP_RANGE="bytes=0-31",
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial["Content-Length"], "32")
        self.assertEqual(b"".join(partial.streaming_content), esp_idf_image()[:32])
        denied = self.client.get(
            reverse("myapp:firmware_manifest", args=[job.request_id, "wrong-token"])
        )
        self.assertEqual(denied.status_code, 403)

    @patch("myapp.ota_services.publish_once")
    def test_success_requires_reported_firmware_version(self, publish):
        job, _ = self.issue_ota()
        with self.assertRaises(ValidationError):
            process_ota_status({
                "device": "ESP32_001", "request_id": job.request_id,
                "status": "SUCCESS", "progress": 100, "version": "9.9.9",
            })


    @patch("myapp.ota_services.publish_once")
    def test_admin_add_publishes_only_after_commit(self, publish):
        self.user.user_permissions.add(Permission.objects.get(codename="add_otajob"))
        self.client.force_login(self.user)
        url = reverse("admin:myapp_otajob_add")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {
                "device": self.device.pk, "firmware": self.firmware.pk, "confirm_ota": "on",
            })
            publish.assert_not_called()
        self.assertEqual(response.status_code, 302)
        publish.assert_called_once()
        self.assertIsNotNone(OTAJob.objects.get().command_sent_at)

    @patch("myapp.ota_services.publish_once")
    def test_invalid_proxy_response_prevents_publish(self, publish):
        self.probe.side_effect = ValueError("bad response")
        job, _ = self.issue_ota()
        self.assertEqual(job.status, "FAILED")
        publish.assert_not_called()

    @patch("myapp.ota_services.requests.get")
    @patch("myapp.ota_services.publish_once")
    def test_https_probe_rejects_html_and_chunking(self, publish, get):
        job, command = self.issue_ota()
        manifest = self.client.get(urlsplit(command["manifest_url"]).path)
        binary_bytes = esp_idf_image()

        def reply(content_type, raw, **headers):
            response = Mock(status_code=200, headers={
                "Content-Type": content_type, "Content-Length": str(len(raw)), **headers,
            })
            response.iter_content.return_value = iter([raw])
            return response

        def probe_with(binary):
            get.side_effect = [
                nullcontext(reply("application/json", manifest.content)),
                nullcontext(binary),
            ]
            _probe_https_release(job, command)

        probe_with(reply("application/octet-stream", binary_bytes))
        for binary in (
            reply("text/html", b"<html>ngrok</html>"),
            reply("application/octet-stream", binary_bytes, **{"Transfer-Encoding": "chunked"}),
            reply("application/octet-stream", b"x" * len(binary_bytes)),
        ):
            with self.assertRaises(ValueError):
                probe_with(binary)

    @override_settings(MQTT_BROKER="broker.hivemq.com")
    @patch("myapp.ota_services.publish_once")
    def test_public_broker_prevents_publish(self, publish):
        with self.assertRaises(ValidationError):
            self.issue_ota()
        publish.assert_not_called()

    @patch("myapp.ota_services.publish_once")
    def test_recovery_retry_and_boot_confirmation(self, publish):
        job, _ = self.issue_ota()
        payload = {"device": self.device.device_id, "request_id": job.request_id, "version": "1.1.0"}

        def report(status, progress):
            return process_ota_status(dict(payload, status=status, progress=progress))[0]

        for status, progress in (("ACCEPTED", 0), ("REBOOTING", 0), ("RECOVERING", 0), ("DOWNLOADING", 70)):
            self.assertEqual(report(status, progress).status, status)
        self.assertIn("FAILED", report("FAILED", 0).error_message)
        for status, progress in (("RECOVERING", 0), ("DOWNLOADING", 0), ("VERIFYING", 100), ("INSTALLING", 100), ("REBOOTING", 100)):
            self.assertEqual(report(status, progress).status, status)
        self.device.refresh_from_db()
        self.assertEqual(self.device.firmware_version, "")
        self.assertEqual(report("SUCCESS", 100).status, "SUCCESS")
        self.device.refresh_from_db()
        self.assertEqual(self.device.firmware_version, "1.1.0")


@override_settings(OTA_PUBLIC_HOST="ota.example.com", ALLOWED_HOSTS=["testserver", "ota.example.com"])
class FirmwareTunnelTests(TestCase):
    def test_public_host_blocks_admin_payment_and_dashboard(self):
        for url in ("/admin/", "/api/check-payment/", "/dashboard/devices/"):
            self.assertEqual(self.client.get(url, HTTP_HOST="ota.example.com").status_code, 404)
            self.assertEqual(self.client.get(url, HTTP_X_FORWARDED_PROTO="https").status_code, 404)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/firmware/manifest/ota-test/" + "x" * 43 + "/", HTTP_HOST="ota.example.com").status_code, 403)


class IoTMessageTests(TestCase):
    def telemetry_payload(self):
        return {
            "device": "ESP32_001", "port": 1, "voltage_v": 229.2,
            "current_a": 0.57, "power_w": 117.6, "energy_total_wh": 25.4,
            "energy_delta_wh": 0.327, "charging": True,
            "status": "CHARGING", "interval_s": 10,
        }

    def test_telemetry_is_stored_without_creating_session(self):
        telemetry = process_telemetry(self.telemetry_payload())
        self.assertEqual(telemetry.device.device_id, "ESP32_001")
        self.assertEqual(DeviceTelemetry.objects.count(), 1)
        self.assertEqual(ChargingSession.objects.count(), 0)

    def test_device_log_is_stored_and_secret_text_redacted(self):
        log = process_device_log({
            "device": "ESP32_001", "level": "INFO",
            "event": "CHARGE_COMPLETED", "message": "Relay OFF",
            "uptime_ms": 123456,
        })
        self.assertEqual(log.event, "CHARGE_COMPLETED")
        redacted = process_device_log({
            "device": "ESP32_001", "level": "WARN",
            "event": "CONFIG_ERROR", "message": "password leaked",
            "uptime_ms": 123457,
        })
        self.assertEqual(redacted.level, "WARNING")
        self.assertIn("REDACTED", redacted.message)

    def test_malformed_and_oversized_mqtt_do_not_raise(self):
        malformed = Mock(topic=MQTT_TOPIC_TELEMETRY, payload=b"{bad-json")
        oversized = Mock(topic=MQTT_TOPIC_LOGS, payload=b"x" * 5000)
        on_message(None, None, malformed)
        on_message(None, None, oversized)
        self.assertEqual(DeviceTelemetry.objects.count(), 0)
        self.assertEqual(DeviceLog.objects.count(), 0)

    def test_reconnect_subscribes_to_all_topics(self):
        client = Mock()
        client.subscribe.return_value = (0, 7)
        on_connect(client, None, None, 0, None)
        topics = {topic for topic, _ in client.subscribe.call_args.args[0]}
        self.assertIn(MQTT_TOPIC_TELEMETRY, topics)
        self.assertIn(MQTT_TOPIC_LOGS, topics)
        self.assertIn(MQTT_TOPIC_OTA_STATUS, topics)
