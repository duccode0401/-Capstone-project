from django import forms
from django.contrib import admin, messages

from .models import (
    ChargerDevice,
    ChargerPort,
    ChargingSession,
    DeviceLog,
    DeviceTelemetry,
    FirmwareArtifact,
    OTAJob,
)
from .ota_services import create_and_issue_ota


@admin.register(ChargerDevice)
class ChargerDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "device_id", "name", "firmware_version", "last_seen_at", "ota_sequence"
    )
    search_fields = ("device_id", "name")
    readonly_fields = ("last_seen_at", "firmware_version", "ota_sequence")


@admin.register(ChargerPort)
class ChargerPortAdmin(admin.ModelAdmin):
    list_display = ("device", "port_no", "is_active")
    list_filter = ("device",)


@admin.register(ChargingSession)
class ChargingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "device_id_str", "port_no", "amount_vnd", "status", "created_at")
    list_filter = ("status", "device", "port_no")
    ordering = ("-created_at",)


@admin.register(FirmwareArtifact)
class FirmwareArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "version", "firmware_type", "chip_target", "file_size", "short_sha",
        "is_approved", "uploaded_by", "uploaded_at",
    )
    list_filter = ("firmware_type", "chip_target", "is_approved")
    search_fields = ("version", "sha256", "description")
    readonly_fields = ("id", "file_size", "sha256", "uploaded_at", "uploaded_by")
    fieldsets = (
        ("Firmware", {"fields": ("id", "version", "file", "firmware_type", "chip_target")}),
        ("Kiểm tra", {"fields": ("file_size", "sha256", "is_approved")}),
        ("Thông tin", {"fields": ("description", "uploaded_by", "uploaded_at")}),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="SHA-256")
    def short_sha(self, obj):
        return f"{obj.sha256[:12]}…"


class OTAJobAdminForm(forms.ModelForm):
    confirm_ota = forms.BooleanField(
        required=True,
        label="Tôi xác nhận thiết bị đang rảnh và muốn phát lệnh OTA",
        help_text="Lệnh được ký và publish ngay sau khi lưu.",
    )

    class Meta:
        model = OTAJob
        fields = ("device", "firmware")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["firmware"].queryset = FirmwareArtifact.objects.filter(
            is_approved=True,
            firmware_type=FirmwareArtifact.TYPE_MAIN,
            chip_target__iexact="esp32",
        )
        if self.instance.pk:
            self.fields["confirm_ota"].required = False
            self.fields["confirm_ota"].widget = forms.HiddenInput()


@admin.register(OTAJob)
class OTAJobAdmin(admin.ModelAdmin):
    form = OTAJobAdminForm
    list_display = (
        "request_id", "device", "firmware", "status", "progress",
        "command_sent_at", "last_status_at", "created_at",
    )
    list_filter = ("status", "device", "firmware")
    search_fields = ("request_id", "device__device_id", "firmware__version")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return tuple(field.name for field in self.model._meta.fields)
        return ()

    def has_add_permission(self, request):
        return super().has_add_permission(request) and request.user.has_perm(
            "myapp.deploy_firmware"
        )

    def has_change_permission(self, request, obj=None):
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return request.user.has_perm("myapp.view_otajob")
        return False

    def save_model(self, request, obj, form, change):
        if change:
            return
        job, command = create_and_issue_ota(obj.device, obj.firmware, request.user)
        obj.__dict__.update(job.__dict__)
        if command is None:
            self.message_user(
                request,
                f"OTAJob đã lưu nhưng publish thất bại: {job.error_message}",
                level=messages.ERROR,
            )
        else:
            self.message_user(
                request,
                f"Đã phát lệnh OTA {job.request_id}; đang chờ ESP32 ACK.",
                level=messages.SUCCESS,
            )


@admin.register(DeviceTelemetry)
class DeviceTelemetryAdmin(admin.ModelAdmin):
    list_display = (
        "device", "port", "voltage_v", "current_a", "power_w",
        "energy_total_wh", "energy_delta_wh", "charging_status", "received_at",
    )
    list_filter = ("device", "port", "charging", "charging_status")
    date_hierarchy = "received_at"
    readonly_fields = tuple(field.name for field in DeviceTelemetry._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(DeviceLog)
class DeviceLogAdmin(admin.ModelAdmin):
    list_display = ("device", "level", "event", "message", "uptime_ms", "received_at")
    list_filter = ("device", "level", "event")
    search_fields = ("device__device_id", "event", "message")
    date_hierarchy = "received_at"
    readonly_fields = tuple(field.name for field in DeviceLog._meta.fields)

    def has_add_permission(self, request):
        return False
