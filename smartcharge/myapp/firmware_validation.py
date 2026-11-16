import struct

from django.core.exceptions import ValidationError


ESP_IMAGE_MAGIC = 0xE9
ESP_APP_DESC_MAGIC = 0xABCD5432
ESP32_CHIP_ID = 0
ESP_IMAGE_HEADER_SIZE = 24
ESP_SEGMENT_HEADER_SIZE = 8
ESP_APP_DESC_OFFSET = ESP_IMAGE_HEADER_SIZE + ESP_SEGMENT_HEADER_SIZE
MAIN_SLOT_SIZE = 0x2F0000
RECOVERY_SLOT_SIZE = 0x100000
MIN_IMAGE_SIZE = ESP_APP_DESC_OFFSET + 256


def _cstring(data):
    return data.split(b"\0", 1)[0].decode("ascii", errors="strict")


def inspect_esp_idf_image(uploaded_file, firmware_type):
    if not uploaded_file.name.lower().endswith(".bin"):
        raise ValidationError("Chỉ chấp nhận file có phần mở rộng .bin.")

    size = uploaded_file.size
    limit = MAIN_SLOT_SIZE if firmware_type == "main" else RECOVERY_SLOT_SIZE
    if size < MIN_IMAGE_SIZE:
        raise ValidationError("File quá nhỏ để là ESP-IDF application image.")
    # Firmware hiện tại yêu cầu size < partition size.
    if size >= limit:
        raise ValidationError(
            f"Firmware phải nhỏ hơn 0x{limit:X} bytes cho loại đã chọn."
        )

    position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    uploaded_file.seek(0)
    header = uploaded_file.read(ESP_APP_DESC_OFFSET + 256)
    uploaded_file.seek(position)
    if len(header) < MIN_IMAGE_SIZE:
        raise ValidationError("ESP-IDF image header không đầy đủ.")
    if header[0] != ESP_IMAGE_MAGIC:
        raise ValidationError("Sai ESP image magic; đây không phải app image hợp lệ.")
    if not 1 <= header[1] <= 16:
        raise ValidationError("Số segment trong ESP image không hợp lệ.")

    chip_id = struct.unpack_from("<H", header, 12)[0]
    if chip_id != ESP32_CHIP_ID:
        raise ValidationError("Firmware không dành cho chip ESP32.")
    descriptor_magic = struct.unpack_from("<I", header, ESP_APP_DESC_OFFSET)[0]
    if descriptor_magic != ESP_APP_DESC_MAGIC:
        raise ValidationError("Không tìm thấy ESP-IDF app descriptor hợp lệ.")

    try:
        version = _cstring(header[ESP_APP_DESC_OFFSET + 16 : ESP_APP_DESC_OFFSET + 48])
        project_name = _cstring(
            header[ESP_APP_DESC_OFFSET + 48 : ESP_APP_DESC_OFFSET + 80]
        )
    except UnicodeDecodeError as exc:
        raise ValidationError("App descriptor chứa chuỗi không hợp lệ.") from exc

    expected_project = "smartcharge" if firmware_type == "main" else "recovery"
    if project_name != expected_project:
        raise ValidationError(
            f"Project trong image là '{project_name}', cần '{expected_project}'."
        )
    if not version or len(version) > 31:
        raise ValidationError("Version trong app descriptor không hợp lệ.")

    return {
        "chip_target": "esp32",
        "version": version,
        "project_name": project_name,
        "size": size,
    }
