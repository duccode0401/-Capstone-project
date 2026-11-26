from django.conf import settings


class LanHostMiddleware:
    def __init__(self, get_response):
        from .lan import refresh_allowed_hosts

        self.get_response = get_response
        self.refresh_allowed_hosts = refresh_allowed_hosts
        if settings.DJANGO_LAN_MODE:
            self.refresh_allowed_hosts()

    def __call__(self, request):
        if settings.DJANGO_LAN_MODE:
            self.refresh_allowed_hosts()
        return self.get_response(request)
