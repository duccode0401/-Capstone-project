import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def firmware_upload_path(instance, filename):
    return f"firmware/{instance.id}/{uuid.uuid4().hex}.bin"


class ChargerDevice(models.Model):
    """
    Mỗi con ESP32 (trạm sạc)
    """
    device_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    firmware_version = models.CharField(max_length=31, blank=True)
    ota_sequence = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return self.device_id


class ChargingSession(models.Model):
    """
    Mỗi lần người dùng nạp tiền/sạc là 1 session
    """
    STATUS_CHOICES = [
        ("paid", "Đã thanh toán"),
        ("waiting", "Đang chờ cổng trống"),
        ("assigned", "Đã gán cổng"),
        ("charging", "Đang sạc"),
        ("done", "Hoàn tất"),
    ]

    device = models.ForeignKey(
        ChargerDevice, null=True, blank=True, on_delete=models.SET_NULL
    )
    port_no = models.PositiveSmallIntegerField(null=True, blank=True)

    amount_vnd = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)

    # Các session cũ có giá trị NULL. Session tạo từ SePay luôn có transaction_id.
    sepay_transaction_id = models.CharField(
        max_length=100, null=True, blank=True, unique=True
    )
    mqtt_start_sent_at = models.DateTimeField(null=True, blank=True)
    mqtt_start_delivery_unknown = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def device_id_str(self):
        return self.device.device_id if self.device else "N/A"

    def __str__(self):
        return f"#{self.id} {self.device_id_str} P{self.port_no} {self.amount_vnd} {self.status}"


class ChargerPort(models.Model):
    """
    Mỗi cổng sạc trên một ESP32.
    """
    device = models.ForeignKey(ChargerDevice, on_delete=models.CASCADE)
    port_no = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("device", "port_no")

    def __str__(self):
        return f"{self.device.device_id} - Port {self.port_no}"


class PaymentOrder(models.Model):
    code = models.CharField(max_length=20, unique=True)
    port = models.ForeignKey(ChargerPort, on_delete=models.PROTECT)
    amount_vnd = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    session = models.OneToOneField(
        ChargingSession, null=True, blank=True, on_delete=models.PROTECT,
    )


class FirmwareArtifact(models.Model):
    TYPE_MAIN = "main"
    TYPE_RECOVERY = "recovery"
    TYPE_CHOICES = [
        (TYPE_MAIN, "SmartCharge main"),
        (TYPE_RECOVERY, "Recovery (chỉ quản lý, không OTA)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=31)
    file = models.FileField(upload_to=firmware_upload_path)
    file_size = models.PositiveBigIntegerField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    chip_target = models.CharField(max_length=20, default="esp32")
    firmware_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, default=TYPE_MAIN
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_firmware",
    )
    description = models.TextField(blank=True)
    is_approved = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("version", "firmware_type"),
                name="unique_firmware_version_type",
            )
        ]
        ordering = ("-uploaded_at",)

    def clean(self):
        from .firmware_validation import inspect_esp_idf_image

        super().clean()
        if self.file:
            metadata = inspect_esp_idf_image(self.file, self.firmware_type)
            if self.chip_target.lower() != metadata["chip_target"]:
                raise ValidationError({"chip_target": "Chip target không khớp image."})
            if self.version != metadata["version"]:
                raise ValidationError(
                    {"version": f"Version trong image là {metadata['version']}."}
                )

    def save(self, *args, **kwargs):
        if self.file:
            position = self.file.tell() if hasattr(self.file, "tell") else 0
            self.file.seek(0)
            digest = hashlib.sha256()
            size = 0
            for chunk in self.file.chunks():
                digest.update(chunk)
                size += len(chunk)
            self.file.seek(position)
            self.file_size = size
            self.sha256 = digest.hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.version} ({self.get_firmware_type_display()})"


class OTAJob(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_ACCEPTED = "ACCEPTED"
    STATUS_REJECTED_BUSY = "REJECTED_BUSY"
    STATUS_DOWNLOADING = "DOWNLOADING"
    STATUS_VERIFYING = "VERIFYING"
    STATUS_INSTALLING = "INSTALLING"
    STATUS_REBOOTING = "REBOOTING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_RECOVERING = "RECOVERING"
    STATUS_CHOICES = [(value, value) for value in (
        STATUS_PENDING,
        STATUS_ACCEPTED,
        STATUS_REJECTED_BUSY,
        STATUS_DOWNLOADING,
        STATUS_VERIFYING,
        STATUS_INSTALLING,
        STATUS_REBOOTING,
        STATUS_SUCCESS,
        STATUS_FAILED,
        STATUS_RECOVERING,
    )]
    TERMINAL_STATUSES = {STATUS_REJECTED_BUSY, STATUS_SUCCESS, STATUS_FAILED}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.CharField(max_length=63, unique=True, editable=False)
    device = models.ForeignKey(
        ChargerDevice, on_delete=models.PROTECT, related_name="ota_jobs"
    )
    firmware = models.ForeignKey(
        FirmwareArtifact, on_delete=models.PROTECT, related_name="ota_jobs"
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    progress = models.PositiveSmallIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True)
    sequence = models.PositiveBigIntegerField(editable=False)
    download_token_hash = models.CharField(max_length=64, editable=False)
    download_expires_at = models.DateTimeField(editable=False)
    command_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_status_at = models.DateTimeField(null=True, blank=True, editable=False)
    reported_version = models.CharField(max_length=31, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ota_jobs",
    )

    class Meta:
        ordering = ("-created_at",)
        permissions = [("deploy_firmware", "Can deploy approved firmware")]
        constraints = [
            models.UniqueConstraint(
                fields=("device",),
                condition=~Q(status__in=("REJECTED_BUSY", "SUCCESS", "FAILED")),
                name="one_active_ota_job_per_device",
            )
        ]

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    def __str__(self):
        return f"{self.request_id}: {self.device} → {self.firmware.version}"


class DeviceTelemetry(models.Model):
    device = models.ForeignKey(
        ChargerDevice, on_delete=models.CASCADE, related_name="telemetry"
    )
    port = models.PositiveSmallIntegerField()
    voltage_v = models.FloatField()
    current_a = models.FloatField()
    power_w = models.FloatField()
    energy_total_wh = models.FloatField()
    energy_delta_wh = models.FloatField()
    charging = models.BooleanField()
    charging_status = models.CharField(max_length=24)
    heap_free_bytes = models.PositiveIntegerField(null=True, blank=True)
    rssi_dbm = models.SmallIntegerField(null=True, blank=True)
    firmware_version = models.CharField(max_length=31, blank=True)
    timestamp = models.DateTimeField(default=timezone.now)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-received_at",)
        indexes = [
            models.Index(fields=("device", "port", "-received_at")),
            models.Index(fields=("-received_at",)),
        ]

    def __str__(self):
        return f"{self.device} P{self.port} {self.charging_status}"


class DeviceLog(models.Model):
    LEVEL_CHOICES = [
        ("INFO", "INFO"),
        ("WARNING", "WARNING"),
        ("ERROR", "ERROR"),
    ]

    device = models.ForeignKey(
        ChargerDevice, on_delete=models.CASCADE, related_name="logs"
    )
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    event = models.CharField(max_length=64)
    message = models.CharField(max_length=500)
    uptime_ms = models.PositiveBigIntegerField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-received_at",)
        indexes = [
            models.Index(fields=("device", "level", "-received_at")),
            models.Index(fields=("event", "-received_at")),
        ]

    def __str__(self):
        return f"{self.device} {self.level} {self.event}"
