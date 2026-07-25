"""Generate backend secrets and an RSA key for encrypted visit payloads."""

from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


if __name__ == "__main__":
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    print(f"VISIT_HASH_SALT={secrets.token_urlsafe(48)}")
    print(f"SUPPORTERS_ADMIN_KEY={secrets.token_urlsafe(48)}")
    print(f"VISIT_PRIVATE_KEY_B64={base64.b64encode(private_pem).decode('ascii')}")
