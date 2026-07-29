from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _test_audio_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give source-configured encryption a deterministic test-only key."""
    value = base64.b64encode(b"K" * 32).decode("ascii")
    monkeypatch.setenv("AUDIO_ENCRYPTION_KEY_V1", value)
