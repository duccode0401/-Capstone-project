import re

from django.conf import settings

DEVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,31}")


def logical_device_id(routing_id):
    if settings.MQTT_PUBLIC_TEST_MODE and routing_id and routing_id == settings.MQTT_PUBLIC_TEST_DEVICE:
        return settings.MQTT_PUBLIC_TEST_LOGICAL_DEVICE
    return routing_id
