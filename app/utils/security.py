from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets


def secure_equals(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_id() -> str:
    return secrets.token_urlsafe(18)


def mask_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "Unknown"

    if address.version == 4:
        parts = value.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"

    network = ipaddress.ip_network(f"{address}/32", strict=False)
    return f"{network.network_address.compressed}/32"
