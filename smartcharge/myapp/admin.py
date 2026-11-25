from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.http import HttpResponseRedirect

from .models import (
    ChargerDevice,
    ChargerPort,
    ChargingSession,
    DeviceLog,
    DeviceTelemetry,
    FirmwareArtifact,
    OTAJob,
)
from .ota_services import prepare_ota, publish_ota, validate_device_ready_for_ota


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

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return (*self.readonly_fields, "version", "firmware_type", "chip_target")
        return self.readonly_fields

    def get_fieldsets(self, request, obj=None):
        if obj:
            return (
                ("Firmware", {"fields": ("id", "version", "firmware_type", "chip_target")}),
                *self.fieldsets[1:],
            )
        return self.fieldsets

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
        help_text="Web kiểm tra manifest và binary qua HTTPS trước khi gửi lệnh.",
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
        if not self.instance._state.adding:
            self.fields["confirm_ota"].required = False
            self.fields["confirm_ota"].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("device") and cleaned.get("firmware"):
            validate_device_ready_for_ota(cleaned["device"], cleaned["firmware"])
        return cleaned


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

    def has_delete_permission(self, request, obj=None):
        return False

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            self.message_user(request, " ".join(exc.messages), level=messages.ERROR)
        except (IntegrityError, OperationalError):
            self.message_user(request, "Could not save the OTA job. Check active jobs and try again.", level=messages.ERROR)
        return HttpResponseRedirect(request.path)

    def save_model(self, request, obj, form, change):
        job, command = prepare_ota(obj.device, obj.firmware, request.user)
        obj.__dict__.update(job.__dict__)

        def issue_after_commit():
            if publish_ota(job, command):
                self.message_user(
                    request,
                    f"Đã gửi OTA {job.request_id}. Đang chờ trạng thái từ ESP32.",
                    level=messages.INFO,
                )
            else:
                self.message_user(request, job.error_message or "OTA job is no longer active.", level=messages.ERROR)

        transaction.on_commit(issue_after_commit)


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
