"""Server-side reference for Django: per-device HMAC, no shared embedded secret.

Use separate per-device secrets from a secrets manager. Never log signing inputs
containing secrets or expose the config/key to the browser.
"""
import hashlib
import hmac
import re
import struct
from urllib.parse import urlsplit


def trusted_url(url, base):
    try:
        p = urlsplit(base)
        valid_authority = p.hostname and re.fullmatch(r"[A-Za-z0-9.-]+", p.hostname) and p.port != 0
    except ValueError:
        return False
    return (base.startswith("https://") and valid_authority and base.endswith("/")
            and url.startswith(base) and len(url) < 384
            and re.fullmatch(r"[A-Za-z0-9._~:/-]+", url) is not None and ".." not in url[8:])


def order_text(order):
    """Canonical START_CHARGE message for the backend signer."""
    return "\n".join(("SC-ORDER-1", order["device"], "START_CHARGE", str(order["port"]),
                     order["order_id"], str(order["amount_vnd"]), str(order["expires_at"]),
                     str(order["seq"])))


def command_text(command):
    return "\n".join(("SC-CMD-1", command["device"], "START_OTA", command["request_id"],
                     command["version"], command["manifest_url"], str(int(command["seq"]))))


def manifest_text(manifest):
    return "\n".join(("SC-MANIFEST-1", manifest["device"], manifest["request_id"],
                     manifest["version"], manifest["url"], str(manifest["size"]),
                     manifest["sha256"], "esp32", str(int(manifest["seq"]))))


def sign(text, key_hex):
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise ValueError("A unique 32-byte device key is required")
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def image_version(image):
    if len(image) <= 288 or image[0] != 0xE9 or struct.unpack_from("<H", image, 12)[0] != 0:
        raise ValueError("Expected an ESP32 application image")
    if struct.unpack_from("<I", image, 32)[0] != 0xABCD5432:
        raise ValueError("Missing ESP-IDF app descriptor")
    version = image[48:80].split(b"\0", 1)[0].decode("ascii")
    project = image[80:112].split(b"\0", 1)[0].decode("ascii")
    if project != "smartcharge" or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Image must be SmartCharge with an X.Y.Z version")
    return version


def release(config, image, request_id, seq, manifest_url, image_url):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", request_id):
        raise ValueError("Invalid request ID")
    if not 0 < seq <= 0xFFFFFFFF or not 288 < len(image) < 0x2F0000:
        raise ValueError("Invalid sequence or image size")
    if not all(trusted_url(u, config["trust_base"]) for u in (manifest_url, image_url)):
        raise ValueError("URLs must be under the provisioned HTTPS base; redirects/query strings disabled")
    version = image_version(image)
    command = dict(device=config["device"], command="START_OTA", request_id=request_id,
                   version=version, manifest_url=manifest_url, seq=str(seq))
    manifest = dict(device=config["device"], request_id=request_id, version=version,
                    url=image_url, size=len(image), sha256=hashlib.sha256(image).hexdigest(),
                    chip="esp32", seq=str(seq))
    command["signature"] = sign(command_text(command), config["key_hex"])
    manifest["signature"] = sign(manifest_text(manifest), config["key_hex"])
    return command, manifest
