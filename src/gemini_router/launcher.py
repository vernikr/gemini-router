"""Optional PyPI update check for the installed ``gemini-router`` CLI.

Long-running services use the package version installed by their deployment process;
the ``proxy`` command therefore bypasses this launcher check. Interactive CLI commands
use the newest published package when PyPI is reachable, and keep the installed version
when it is not.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

_ENV = "GEMINI_ROUTER_AUTO_UPDATE"
_PYPI_JSON = "https://pypi.org/pypi/gemini-router/json"
_DISABLED = {"0", "false", "no", "off"}
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _version_key(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(value)
    return tuple(map(int, match.groups())) if match else None


def _latest_version(timeout: float = 1.25) -> str | None:
    request = urllib.request.Request(
        _PYPI_JSON,
        headers={"Accept": "application/json", "User-Agent": "gemini-router-cli"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(256_000))
        version = data.get("info", {}).get("version")
        return version if isinstance(version, str) and _version_key(version) else None
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return None


def _uvx_command() -> list[str] | None:
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx]
    uv = shutil.which("uv")
    return [uv, "tool", "run"] if uv else None


def _source_checkout() -> bool:
    root = Path(__file__).resolve().parents[2]
    return (root / ".git").exists()


def _run_current(args: list[str]) -> int:
    from .cli import main as cli_main

    return cli_main(args)


def main(argv: list[str] | None = None) -> int:
    """Run the newest published CLI when available; use ``--no-update`` to opt out."""
    args = list(sys.argv[1:] if argv is None else argv)
    no_update = "--no-update" in args
    args = [arg for arg in args if arg != "--no-update"]
    env_disabled = os.environ.get(_ENV, "").strip().lower() in _DISABLED
    # The proxy is a managed service entry point; its deploy tool owns upgrades.
    # Help/version are local introspection and must not wait for a registry request.
    local_only = any(arg in {"--help", "-h", "--version"} for arg in args)
    local_only = local_only or (args and args[0] == "proxy")
    if no_update or env_disabled or local_only or _source_checkout():
        return _run_current(args)

    current = _version_key(__version__)
    latest = _latest_version()
    candidate = _version_key(latest or "")
    if not current or not candidate or candidate <= current:
        return _run_current(args)

    uvx = _uvx_command()
    if not uvx:
        print("gemini-router: uv is unavailable; continuing with the installed version",
              file=sys.stderr)
        return _run_current(args)

    env = os.environ.copy()
    env[_ENV] = "0"  # prevent a child launch from checking the registry again
    pin = f"gemini-router=={latest}"
    base = [*uvx, "--from", pin, "gemini-router"]
    try:
        check = subprocess.run(
            [*base, "--version"], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        check = None
    if check is None or check.returncode != 0:
        print(f"gemini-router: update to {latest} failed; using {__version__}",
              file=sys.stderr)
        return _run_current(args)

    print(f"gemini-router: using updated {latest} (disable with --no-update or {_ENV}=0)",
          file=sys.stderr)
    try:
        return subprocess.run([*base, *args], env=env, check=False).returncode
    except OSError as exc:
        print(f"gemini-router: could not launch updated version ({exc}); using {__version__}",
              file=sys.stderr)
        return _run_current(args)


if __name__ == "__main__":
    raise SystemExit(main())
