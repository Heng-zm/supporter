from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXPECTED_ROUTES = {
    "/api/audio/metadata",
    "/api/audio/file",
}


def _runtime_check() -> tuple[bool, list[str]]:
    from app.main import app

    registered = {
        getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", "")
    }
    missing = sorted(EXPECTED_ROUTES - registered)
    return not missing, missing


def _patch_source_check() -> tuple[bool, list[str]]:
    """Validate a standalone patch before it is merged into the host backend."""
    main_path = PROJECT_ROOT / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")
    required = [
        "include_audio_router(app, api_prefix=runtime_settings.api_prefix)",
        "await start_audio_extension(app)",
        "await close_audio_extension(app)",
    ]
    missing = [value for value in required if value not in source]
    return not missing, missing


def main() -> int:
    mode = "runtime"
    try:
        passed, missing = _runtime_check()
    except ModuleNotFoundError as exc:
        # The downloadable backend package is a patch and intentionally does not
        # duplicate every host-backend module. After merging, runtime mode runs.
        if exc.name not in {"app.config", "app.routers", "app.services"}:
            raise
        mode = "standalone-patch"
        passed, missing = _patch_source_check()

    if not passed:
        print(f"Audio route check FAILED ({mode}).")
        for item in missing:
            print(f"Missing: {item}")
        return 1

    print(f"Audio route check passed ({mode}).")
    for route in sorted(EXPECTED_ROUTES):
        print(f"Expected route: {route}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
