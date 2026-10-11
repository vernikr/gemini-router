#!/usr/bin/env python3
"""CI entry point: `python scripts/check_secrets.py` (any interpreter).

The rule lives in gemini_router.secrets_guard (tested by the suite); this wrapper loads that
file directly so a fresh checkout without an install can run the check before anything
else imports the package and its dependencies."""
import importlib.util
import sys
from pathlib import Path

_guard_path = Path(__file__).resolve().parent.parent / "src" / "gemini_router" / "secrets_guard.py"
_spec = importlib.util.spec_from_file_location("secrets_guard", _guard_path)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

if __name__ == "__main__":
    raise SystemExit(guard.main(sys.argv[1:]))
