import logging
import re

from django.conf import settings
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.http import Http404, HttpResponseBadRequest, HttpResponseNotFound, HttpResponseServerError


FIRMWARE_PATH = re.compile(r"/firmware/(manifest|download)/[A-Za-z0-9_.-]{1,63}/[A-Za-z0-9_-]{43}/")


class FirmwareTunnelMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            host = request.get_host().lower().split(":", 1)[0].rstrip(".")
        except DisallowedHost:
            return HttpResponseBadRequest()
        if host == settings.OTA_PUBLIC_HOST or request.META.get("HTTP_X_FORWARDED_PROTO"):
            if request.method != "GET" or not FIRMWARE_PATH.fullmatch(request.path_info):
                return HttpResponseNotFound()
        return self.get_response(request)

    def process_exception(self, request, exception):
        if FIRMWARE_PATH.fullmatch(request.path_info):
            if isinstance(exception, PermissionDenied):
                return None
            if isinstance(exception, Http404):
                return HttpResponseNotFound()
            logging.getLogger(__name__).error("Firmware endpoint failed (%s)", type(exception).__name__)
            return HttpResponseServerError()


class HideFirmwareURL(logging.Filter):
    def filter(self, record):
        request = getattr(record, "request", None)
        return not (
            getattr(request, "path_info", "").startswith("/firmware/")
            or "/firmware/" in record.getMessage()
        )
