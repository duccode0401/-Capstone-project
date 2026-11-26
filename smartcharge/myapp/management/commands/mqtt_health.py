from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from myapp.mqtt_config import mqtt_connection
from myapp.mqtt_health import roundtrip


class Command(BaseCommand):
    help = "Verify private MQTT TLS, authentication, QoS 1 loopback, and optional keepalive."

    def add_arguments(self, parser):
        parser.add_argument("--ping", action="store_true")
        parser.add_argument("--check-rejections", action="store_true")

    def handle(self, *args, **options):
        if settings.MQTT_PUBLIC_TEST_MODE:
            raise CommandError("MQTT health requires the private broker configuration.")
        try:
            config = mqtt_connection()
            roundtrip(config, verify_ping=options["ping"])
            self.stdout.write(self.style.SUCCESS(
                f"{config['hostname']}:{config['port']} TLS, CONNACK, SUBACK and QoS 1 round trip OK."
            ))
            if options["ping"]:
                self.stdout.write(self.style.SUCCESS("PINGREQ/PINGRESP OK."))
            if options["check_rejections"]:
                wrong = {**config["auth"], "password": uuid4().hex}
                roundtrip({**config, "auth": wrong}, expect_rejection=True)
                roundtrip({**config, "auth": None}, expect_rejection=True)
                self.stdout.write(self.style.SUCCESS("Wrong password and anonymous MQTT rejected."))
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
