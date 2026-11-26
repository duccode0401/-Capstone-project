from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError
from django.db.models import Prefetch
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .models import ChargerDevice, DeviceTelemetry, FirmwareArtifact, OTAJob, OTAProgressEvent
from .ota_services import create_and_issue_ota, validate_device_ready_for_ota


class OTASelection(forms.Form):
    device = forms.ModelChoiceField(
        label="Thiết bị", queryset=ChargerDevice.objects.order_by("device_id"),
    )
    firmware = forms.ModelChoiceField(
        label="Bản phát hành", queryset=FirmwareArtifact.objects.filter(
            firmware_type=FirmwareArtifact.TYPE_MAIN,
        ),
    )


@staff_member_required
@permission_required("myapp.view_otajob", raise_exception=True)
@require_http_methods(["GET", "POST"])
def ota_dashboard(request):
    can_deploy = request.user.has_perms(("myapp.add_otajob", "myapp.deploy_firmware"))
    public = settings.MQTT_PUBLIC_TEST_MODE
    if request.method == "POST" and (public or not can_deploy):
        raise PermissionDenied

    selection = request.POST if request.method == "POST" else request.GET
    if not selection.get("device"):
        selection = selection.copy()
        device = ChargerDevice.objects.filter(device_id=(
            settings.MQTT_PUBLIC_TEST_DEVICE if public else settings.MQTT_DEVICE_ID
        )).first()
        if device:
            selection["device"] = str(device.pk)
    form = OTASelection(selection or None)
    ready = False
    compatibility = ""
    device = firmware = None
    if form.is_bound:
        form.is_valid()
        device = form.cleaned_data.get("device")
        firmware = form.cleaned_data.get("firmware")
    if device and firmware and not public:
        try:
            validate_device_ready_for_ota(device, firmware)
            ready = True
            if request.method == "POST":
                create_and_issue_ota(device, firmware, request.user)
                messages.info(request, "Đã tạo yêu cầu OTA. Theo dõi trạng thái bên dưới.")
                query = urlencode({"device": device.pk, "firmware": firmware.pk})
                return redirect(f"{reverse('myapp:ota_dashboard')}?{query}")
        except ValidationError as exc:
            ready = False
            compatibility = " ".join(exc.messages)
        except (IntegrityError, OperationalError):
            ready = False
            compatibility = "Không thể lưu yêu cầu OTA. Kiểm tra job đang chạy và thử lại."

    running_version = device.running_version if device else ""
    if device and not running_version:
        reading = DeviceTelemetry.objects.filter(device=device).first()
        running_version = reading.firmware_version if reading else ""

    history = OTAJob.objects.select_related("device", "firmware").prefetch_related(
        Prefetch("progress_events", queryset=OTAProgressEvent.objects.order_by("-pk")[:30],
                 to_attr="recent_events"),
    )
    if device:
        history = history.filter(device=device)
    page = Paginator(history[:500], 15).get_page(request.GET.get("page"))
    return render(request, "myapp/ota_dashboard.html", {
        "form": form, "device": device, "firmware": firmware, "running_version": running_version,
        "public": public, "can_deploy": can_deploy, "ready": ready,
        "compatibility": compatibility, "page": page,
    })
