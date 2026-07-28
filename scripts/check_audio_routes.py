from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app  # noqa: E402


EXPECTED_ROUTES = {
    "/api/audio/metadata",
    "/api/audio/file",
}


def main() -> int:
    registered = {
        getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", "")
    }
    missing = sorted(EXPECTED_ROUTES - registered)

    if missing:
        print("Audio route check FAILED.")
        for route in missing:
            print(f"Missing: {route}")
        return 1

    print("Audio route check passed.")
    for route in sorted(EXPECTED_ROUTES):
        print(f"Registered: {route}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
