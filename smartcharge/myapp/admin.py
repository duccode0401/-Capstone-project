from django.contrib import admin

from .models import ChargerDevice, ChargerPort, ChargingSession

admin.site.site_header = "Quản trị SmartCharge"
admin.site.site_title = "SmartCharge Administration"
admin.site.index_title = "Quản trị hệ thống SmartCharge"
admin.site.site_url = "/dashboard/devices/"


@admin.register(ChargerDevice)
class ChargerDeviceAdmin(admin.ModelAdmin):
    list_display = ("logical_id", "device_id", "name", "running_version", "last_seen_at")
    search_fields = ("device_id", "name")
    readonly_fields = ("last_seen_at", "firmware_version", "last_heartbeat_at", "mqtt_health")


@admin.register(ChargerPort)
class ChargerPortAdmin(admin.ModelAdmin):
    list_display = ("device", "port_no", "is_active")
    list_filter = ("device",)


@admin.register(ChargingSession)
class ChargingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "device_id_str", "port_no", "amount_vnd", "status", "created_at")
    list_filter = ("status", "device", "port_no")
    ordering = ("-created_at",)


