from __future__ import annotations

import argparse
import base64
import re
import secrets

_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 32-byte AES-256 audio encryption key."
    )
    parser.add_argument(
        "--version",
        default="v1",
        help="Key version used in AUDIO_ENCRYPTION_KEY_<VERSION> (default: v1).",
    )
    args = parser.parse_args()
    version = args.version.strip().lower()
    if not _VERSION_RE.fullmatch(version):
        parser.error("version may contain only letters, numbers, dot, underscore, or hyphen")

    env_name = f"AUDIO_ENCRYPTION_KEY_{version.upper()}"
    encoded = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    print(f"{env_name}={encoded}")
    print("Store this value only in the backend secret environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
