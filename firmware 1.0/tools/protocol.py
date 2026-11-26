"""Host-side reference for signed charge commands (test and backend integration)."""
import hashlib
import hmac


def order_text(order):
    return "\n".join(("SC-ORDER-1", order["device"], "START_CHARGE", str(order["port"]),
                      order["order_id"], str(order["amount_vnd"]), str(order["expires_at"]),
                      str(order["seq"])))


def sign(text, key_hex):
    return hmac.new(bytes.fromhex(key_hex), text.encode(), hashlib.sha256).hexdigest()
