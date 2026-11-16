from django.urls import path
from . import views

app_name = "myapp"

urlpatterns = [
    path("", views.payment_view, name="home"),
    path("api/payment-baseline/", views.payment_baseline, name="payment_baseline"),
    path("api/check-payment/", views.check_payment, name="check_payment"),
    path("api/charger/report-port/", views.api_report_port, name="api_report_port"),
    path("dashboard/sessions/", views.session_dashboard, name="session_dashboard"),
    path("dashboard/devices/", views.devices_dashboard, name="devices_dashboard"),
    path("dashboard/logs/", views.device_logs_dashboard, name="device_logs_dashboard"),
    path("api/devices/status/", views.device_status_api, name="device_status_api"),
    path("api/devices/logs/", views.recent_logs_api, name="recent_logs_api"),
    path(
        "firmware/manifest/<str:request_id>/<str:token>/",
        views.firmware_manifest,
        name="firmware_manifest",
    ),
    path(
        "firmware/download/<str:request_id>/<str:token>/",
        views.firmware_download,
        name="firmware_download",
    ),
]

