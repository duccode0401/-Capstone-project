from django.contrib import admin

from .models import ChargerDevice, ChargerPort, ChargingSession, DeviceLog, DeviceTelemetry


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


@admin.register(DeviceTelemetry)
class DeviceTelemetryAdmin(admin.ModelAdmin):
    list_display = ("device", "port", "voltage_v", "current_a", "power_w", "energy_total_wh", "charging_status", "received_at")
    list_filter = ("device", "port", "charging", "charging_status")
    readonly_fields = tuple(field.name for field in DeviceTelemetry._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(DeviceLog)
class DeviceLogAdmin(admin.ModelAdmin):
    list_display = ("device", "level", "event", "message", "uptime_ms", "received_at")
    list_filter = ("device", "level", "event")
    search_fields = ("device__device_id", "event", "message")
    readonly_fields = tuple(field.name for field in DeviceLog._meta.fields)

    def has_add_permission(self, request):
        return False
