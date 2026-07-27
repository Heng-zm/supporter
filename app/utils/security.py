from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Iterable


def secure_equals(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_id() -> str:
    return secrets.token_urlsafe(18)


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip().strip('"').strip("[]")
    if not value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def ip_is_trusted(
    value: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    address = parse_ip(value)
    if address is None:
        return False
    return any(address.version == network.version and address in network for network in networks)


def mask_ip(value: str) -> str:
    address = parse_ip(value)
    if address is None:
        return "Unknown"

    if address.version == 4:
        parts = address.compressed.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"

    network = ipaddress.ip_network(f"{address}/32", strict=False)
    return f"{network.network_address.compressed}/32"
