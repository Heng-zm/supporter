from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings
from app.models import EncryptedVisitEnvelope, VisitPayload


ENCRYPTION_NAME = "rsa-oaep-aes-gcm-v1"
AAD = b"ozo-visit-v1"
logger = logging.getLogger("app.visit_crypto")


class VisitCryptoService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._private_key: rsa.RSAPrivateKey | None = None
        self.load_error: str | None = None

        encoded_key = "".join((settings.visit_private_key_b64 or "").split())
        if not encoded_key:
            return

        try:
            pem = base64.b64decode(encoded_key, validate=True)
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError("VISIT_PRIVATE_KEY_B64 must contain an RSA private key.")
            if key.key_size < 2048:
                raise ValueError("Visit RSA private key must be at least 2048 bits.")
        except (ValueError, TypeError, binascii.Error) as exc:
            self.load_error = (
                str(exc)
                or "VISIT_PRIVATE_KEY_B64 is not a valid base64 PEM private key."
            )
            logger.error(
                "Visit encryption key could not be loaded; encryption is disabled: %s",
                self.load_error,
            )
            return

        self._private_key = key

    @property
    def enabled(self) -> bool:
        return self._private_key is not None

    def ensure_required_encryption_ready(self) -> None:
        if self.settings.require_encrypted_visits and not self.enabled:
            detail = self.load_error or "Visit encryption key is not configured."
            raise RuntimeError(f"Encrypted visits are required but unavailable: {detail}")

    def public_key_b64(self) -> str:
        if self._private_key is None:
            raise RuntimeError("Visit encryption key is not configured.")
        der = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode("ascii")

    @staticmethod
    def _decode_b64(value: str, field_name: str, maximum: int) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"{field_name} is not valid base64.") from exc
        if not decoded or len(decoded) > maximum:
            raise ValueError(f"{field_name} has an invalid size.")
        return decoded

    def decrypt(self, raw_envelope: dict[str, Any]) -> VisitPayload:
        if self._private_key is None:
            raise RuntimeError("Visit encryption key is not configured.")

        envelope = EncryptedVisitEnvelope.model_validate(raw_envelope)
        encrypted_key = self._decode_b64(envelope.encryptedKey, "encryptedKey", 1024)
        iv = self._decode_b64(envelope.iv, "iv", 32)
        ciphertext = self._decode_b64(envelope.ciphertext, "ciphertext", 64 * 1024)

        if len(iv) != 12:
            raise ValueError("iv must contain exactly 12 bytes.")

        try:
            aes_key = self._private_key.decrypt(
                encrypted_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            if len(aes_key) != 32:
                raise ValueError("Invalid AES key length.")
            plaintext = AESGCM(aes_key).decrypt(iv, ciphertext, AAD)
            decoded = json.loads(plaintext.decode("utf-8"))
        except (
            InvalidTag,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("Encrypted visit payload could not be decrypted.") from exc

        if not isinstance(decoded, dict):
            raise ValueError("Decrypted visit payload must be a JSON object.")
        return VisitPayload.model_validate(decoded)
