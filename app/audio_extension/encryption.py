from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import struct
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RAAEGCM1"
FORMAT_VERSION = 1
ALGORITHM = "AES-256-GCM-CHUNKED"
HEADER_LENGTH_STRUCT = struct.Struct(">I")
CHUNK_LENGTH_STRUCT = struct.Struct(">I")
CHUNK_INDEX_STRUCT = struct.Struct(">I")
TAG_BYTES = 16
NONCE_SEED_BYTES = 16
MAX_HEADER_BYTES = 16 * 1024


class AudioEncryptionError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EncryptedAudio:
    data: bytes
    ciphertext_sha256: str
    ciphertext_byte_length: int
    nonce_seed_b64: str
    chunk_count: int


@dataclass(frozen=True, slots=True)
class DecryptionPlan:
    key_version: str
    chunk_size: int
    plaintext_length: int
    plaintext_sha256: str
    nonce_seed: bytes
    chunk_count: int
    authenticated_header: bytes


class _AsyncByteReader:
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source.__aiter__()
        self._buffer = bytearray()
        self._eof = False
        self.sha256 = hashlib.sha256()
        self.total = 0

    async def read_exactly(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("size cannot be negative")
        while len(self._buffer) < size and not self._eof:
            try:
                chunk = await self._source.__anext__()
            except StopAsyncIteration:
                self._eof = True
                break
            if chunk:
                self._buffer.extend(chunk)
        if len(self._buffer) < size:
            raise AudioEncryptionError(
                "Encrypted audio container ended unexpectedly.",
                code="encrypted_audio_truncated",
            )
        value = bytes(self._buffer[:size])
        del self._buffer[:size]
        self.sha256.update(value)
        self.total += len(value)
        return value

    async def ensure_eof(self) -> None:
        if self._buffer:
            raise AudioEncryptionError(
                "Encrypted audio container has trailing data.",
                code="encrypted_audio_trailing_data",
            )
        while not self._eof:
            try:
                chunk = await self._source.__anext__()
            except StopAsyncIteration:
                self._eof = True
                return
            if chunk:
                raise AudioEncryptionError(
                    "Encrypted audio container has trailing data.",
                    code="encrypted_audio_trailing_data",
                )


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _nonce(seed: bytes, index: int) -> bytes:
    if len(seed) != NONCE_SEED_BYTES:
        raise AudioEncryptionError(
            "Encrypted audio nonce seed is invalid.",
            code="encrypted_audio_header_invalid",
        )
    if not 0 <= index <= 0xFFFFFFFF:
        raise AudioEncryptionError(
            "Encrypted audio contains too many chunks.",
            code="encrypted_audio_chunk_count_invalid",
        )
    # Derive an independent 96-bit GCM nonce from a random 128-bit per-file
    # seed and the chunk index. This avoids relying on a short random prefix
    # while preserving deterministic, non-repeating nonces inside the file.
    return hashlib.sha256(
        b"RAAE-GCM-NONCE\x00" + seed + CHUNK_INDEX_STRUCT.pack(index)
    ).digest()[:12]


def _aad(authenticated_header: bytes, index: int, plain_length: int) -> bytes:
    return (
        authenticated_header
        + CHUNK_INDEX_STRUCT.pack(index)
        + CHUNK_LENGTH_STRUCT.pack(plain_length)
    )


def encrypt_audio(
    plaintext: bytes,
    *,
    key: bytes,
    key_version: str,
    chunk_size: int,
    plaintext_sha256: str | None = None,
) -> EncryptedAudio:
    if len(key) != 32:
        raise AudioEncryptionError(
            "AES-256-GCM requires a 32-byte key.",
            code="audio_encryption_key_invalid",
        )
    if not plaintext:
        raise AudioEncryptionError(
            "The plaintext audio is empty.",
            code="audio_plaintext_empty",
        )
    if chunk_size <= 0:
        raise AudioEncryptionError(
            "Encryption chunk size is invalid.",
            code="audio_encryption_chunk_size_invalid",
        )

    actual_plain_hash = hashlib.sha256(plaintext).hexdigest()
    if plaintext_sha256 and plaintext_sha256 != actual_plain_hash:
        raise AudioEncryptionError(
            "Plaintext checksum changed before encryption.",
            code="audio_plaintext_checksum_mismatch",
        )

    nonce_seed = secrets.token_bytes(NONCE_SEED_BYTES)
    chunk_count = math.ceil(len(plaintext) / chunk_size)
    header = {
        "algorithm": ALGORITHM,
        "chunkCount": chunk_count,
        "chunkSize": chunk_size,
        "formatVersion": FORMAT_VERSION,
        "keyVersion": key_version,
        "nonceSeed": base64.b64encode(nonce_seed).decode("ascii"),
        "plaintextLength": len(plaintext),
        "plaintextSha256": actual_plain_hash,
    }
    header_bytes = _canonical_json(header)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise AudioEncryptionError(
            "Encrypted audio header is too large.",
            code="encrypted_audio_header_too_large",
        )
    authenticated_header = MAGIC + HEADER_LENGTH_STRUCT.pack(len(header_bytes)) + header_bytes

    aesgcm = AESGCM(key)
    output = bytearray(authenticated_header)
    for index in range(chunk_count):
        start = index * chunk_size
        chunk = plaintext[start : start + chunk_size]
        output.extend(CHUNK_LENGTH_STRUCT.pack(len(chunk)))
        output.extend(
            aesgcm.encrypt(
                _nonce(nonce_seed, index),
                chunk,
                _aad(authenticated_header, index, len(chunk)),
            )
        )

    data = bytes(output)
    return EncryptedAudio(
        data=data,
        ciphertext_sha256=hashlib.sha256(data).hexdigest(),
        ciphertext_byte_length=len(data),
        nonce_seed_b64=header["nonceSeed"],
        chunk_count=chunk_count,
    )


def _parse_header(raw: bytes, authenticated_header: bytes) -> DecryptionPlan:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioEncryptionError(
            "Encrypted audio header is not valid JSON.",
            code="encrypted_audio_header_invalid",
        ) from exc
    if not isinstance(value, dict):
        raise AudioEncryptionError(
            "Encrypted audio header is invalid.",
            code="encrypted_audio_header_invalid",
        )

    if value.get("formatVersion") != FORMAT_VERSION or value.get("algorithm") != ALGORITHM:
        raise AudioEncryptionError(
            "Encrypted audio format or algorithm is unsupported.",
            code="encrypted_audio_format_unsupported",
        )
    try:
        chunk_size = int(value.get("chunkSize"))
        plaintext_length = int(value.get("plaintextLength"))
        chunk_count = int(value.get("chunkCount"))
    except (TypeError, ValueError) as exc:
        raise AudioEncryptionError(
            "Encrypted audio header numeric fields are invalid.",
            code="encrypted_audio_header_invalid",
        ) from exc
    key_version = str(value.get("keyVersion") or "").strip().lower()
    plaintext_sha256 = str(value.get("plaintextSha256") or "").strip().lower()
    nonce_text = str(value.get("nonceSeed") or "").strip()
    try:
        nonce_seed = base64.b64decode(nonce_text, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise AudioEncryptionError(
            "Encrypted audio nonce seed is invalid.",
            code="encrypted_audio_header_invalid",
        ) from exc

    expected_chunks = math.ceil(plaintext_length / chunk_size) if chunk_size > 0 else 0
    if (
        chunk_size <= 0
        or plaintext_length <= 0
        or chunk_count <= 0
        or chunk_count != expected_chunks
        or len(nonce_seed) != NONCE_SEED_BYTES
        or len(plaintext_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in plaintext_sha256)
        or not key_version
    ):
        raise AudioEncryptionError(
            "Encrypted audio header values are invalid.",
            code="encrypted_audio_header_invalid",
        )

    return DecryptionPlan(
        key_version=key_version,
        chunk_size=chunk_size,
        plaintext_length=plaintext_length,
        plaintext_sha256=plaintext_sha256,
        nonce_seed=nonce_seed,
        chunk_count=chunk_count,
        authenticated_header=authenticated_header,
    )


async def decrypt_audio_stream(
    source: AsyncIterator[bytes],
    *,
    keys: Mapping[str, bytes],
    expected_plaintext_length: int,
    expected_plaintext_sha256: str,
    expected_ciphertext_length: int,
    expected_ciphertext_sha256: str,
    expected_key_version: str,
    max_plaintext_bytes: int,
    byte_range: tuple[int, int] | None = None,
) -> AsyncIterator[bytes]:
    reader = _AsyncByteReader(source)
    magic = await reader.read_exactly(len(MAGIC))
    if magic != MAGIC:
        raise AudioEncryptionError(
            "Stored audio is not a supported encrypted container.",
            code="encrypted_audio_magic_invalid",
        )
    header_length_raw = await reader.read_exactly(HEADER_LENGTH_STRUCT.size)
    header_length = HEADER_LENGTH_STRUCT.unpack(header_length_raw)[0]
    if not 1 <= header_length <= MAX_HEADER_BYTES:
        raise AudioEncryptionError(
            "Encrypted audio header length is invalid.",
            code="encrypted_audio_header_invalid",
        )
    header_bytes = await reader.read_exactly(header_length)
    authenticated_header = magic + header_length_raw + header_bytes
    plan = _parse_header(header_bytes, authenticated_header)

    if plan.plaintext_length > max_plaintext_bytes:
        raise AudioEncryptionError(
            "Decrypted audio would exceed the configured maximum.",
            code="decrypted_audio_too_large",
        )
    if (
        plan.plaintext_length != expected_plaintext_length
        or plan.plaintext_sha256 != expected_plaintext_sha256
        or plan.key_version != expected_key_version
    ):
        raise AudioEncryptionError(
            "Encrypted audio header does not match its manifest.",
            code="encrypted_audio_manifest_mismatch",
        )

    key = keys.get(plan.key_version)
    if key is None:
        raise AudioEncryptionError(
            f'Encryption key version "{plan.key_version}" is unavailable.',
            code="audio_decryption_key_unavailable",
        )
    if len(key) != 32:
        raise AudioEncryptionError(
            "The selected decryption key is invalid.",
            code="audio_decryption_key_invalid",
        )

    range_start, range_end = byte_range or (0, plan.plaintext_length - 1)
    if not 0 <= range_start <= range_end < plan.plaintext_length:
        raise AudioEncryptionError(
            "Requested plaintext range is invalid.",
            code="audio_range_invalid",
        )

    aesgcm = AESGCM(key)
    plaintext_hash = hashlib.sha256()
    plaintext_total = 0
    emitted_total = 0

    for index in range(plan.chunk_count):
        length_raw = await reader.read_exactly(CHUNK_LENGTH_STRUCT.size)
        plain_length = CHUNK_LENGTH_STRUCT.unpack(length_raw)[0]
        expected_length = min(
            plan.chunk_size,
            plan.plaintext_length - index * plan.chunk_size,
        )
        if plain_length != expected_length or plain_length <= 0:
            raise AudioEncryptionError(
                "Encrypted audio chunk length is invalid.",
                code="encrypted_audio_chunk_invalid",
            )
        ciphertext = await reader.read_exactly(plain_length + TAG_BYTES)
        try:
            plaintext = aesgcm.decrypt(
                _nonce(plan.nonce_seed, index),
                ciphertext,
                _aad(plan.authenticated_header, index, plain_length),
            )
        except InvalidTag as exc:
            raise AudioEncryptionError(
                "Encrypted audio authentication failed.",
                code="encrypted_audio_authentication_failed",
            ) from exc

        plaintext_hash.update(plaintext)
        chunk_start = plaintext_total
        chunk_end = plaintext_total + len(plaintext) - 1
        plaintext_total += len(plaintext)
        if plaintext_total > max_plaintext_bytes:
            raise AudioEncryptionError(
                "Decrypted audio exceeded the configured maximum.",
                code="decrypted_audio_too_large",
            )

        overlap_start = max(range_start, chunk_start)
        overlap_end = min(range_end, chunk_end)
        if overlap_start <= overlap_end:
            local_start = overlap_start - chunk_start
            local_end = overlap_end - chunk_start + 1
            output = plaintext[local_start:local_end]
            emitted_total += len(output)
            if output:
                yield output

    await reader.ensure_eof()
    if reader.total != expected_ciphertext_length:
        raise AudioEncryptionError(
            "Encrypted audio byte length does not match its manifest.",
            code="encrypted_audio_size_mismatch",
        )
    if reader.sha256.hexdigest() != expected_ciphertext_sha256:
        raise AudioEncryptionError(
            "Encrypted audio checksum does not match its manifest.",
            code="encrypted_audio_checksum_mismatch",
        )
    if plaintext_total != plan.plaintext_length:
        raise AudioEncryptionError(
            "Decrypted audio byte length is invalid.",
            code="decrypted_audio_size_mismatch",
        )
    if plaintext_hash.hexdigest() != plan.plaintext_sha256:
        raise AudioEncryptionError(
            "Decrypted audio checksum does not match its manifest.",
            code="decrypted_audio_checksum_mismatch",
        )
    expected_emitted = range_end - range_start + 1
    if emitted_total != expected_emitted:
        raise AudioEncryptionError(
            "Decrypted range byte length is invalid.",
            code="decrypted_audio_range_size_mismatch",
        )
