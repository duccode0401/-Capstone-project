"""Reference serializer for the proposed, firmware-gated charging protocol."""

import hashlib
import hmac
import re

from django.core.exceptions import ValidationError


def signed_start_charging(*, key, device, port, session, amount_vnd, expires_at, nonce):
    """Produce the proposed v1 wire object. Firmware 1.0 must never receive it."""
    if (
        not isinstance(key, bytes) or len(key) != 32
        or not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", device)
        or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
        or type(port) is not int or port != 1
        or any(type(value) is not int or not 0 < value < 2**63
               for value in (session, amount_vnd, expires_at))
        or amount_vnd > 2_147_483_647
    ):
        raise ValidationError("Invalid charging command fields.")
    command = "START_CHARGING"
    canonical = f"SC-CHARGE-1\n{device}\n{command}\n{port}\n{session}\n{amount_vnd}\n{expires_at}\n{nonce}"
    return {
        "device": device, "command": command, "port": port, "session": session,
        "amount_vnd": amount_vnd, "expires_at": expires_at, "nonce": nonce,
        "signature": hmac.new(key, canonical.encode("ascii"), hashlib.sha256).hexdigest(),
    }
