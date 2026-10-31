#!/usr/bin/env python3
"""Reject credential-shaped literals before package contents are published.

The source distribution includes tests, so fixtures must carry the explicit DUMMY marker.
The same check runs in local verification and release workflows.

Usage: python scripts/check_secrets.py [paths…]   (default: every git-tracked text file)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SHAPES = (r"AIza[A-Za-z0-9_\-]{20,}",                       # Google traffic/standard keys
          r"AQ\.[A-Za-z0-9_\-]{20,}",                        # Google auth keys (2026)
          r"sk-[A-Za-z0-9]{20,}",                              # OpenAI-style
          r"gh[pousr]_[A-Za-z0-9]{20,}",                       # GitHub tokens
          r"AKIA[0-9A-Z]{16}",                                 # AWS access key ids
          r"xox[baprs]-[A-Za-z0-9-]{10,}")                     # Slack tokens
PATTERN = re.compile("|".join(SHAPES))
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".ruff_cache"}
MAX_BYTES = 2_000_000


def scan_text(text: str, name: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in PATTERN.findall(line):
            if "DUMMY" in m or "dummy" in m:
                continue
            hits.append(f"{name}:{lineno}: looks like a live credential "
                        f"({m[:8]}…{m[-4:]}, {len(m)} chars) — use a DUMMY-marked fake")
    return hits


def tracked_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return [p for p in Path().rglob("*") if p.is_file()]
    return [Path(line) for line in out.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    hits: list[str] = []
    for path in tracked_files(argv):
        if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        try:
            hits += scan_text(path.read_text(encoding="utf-8"), str(path))
        except (UnicodeDecodeError, OSError):
            continue
    for hit in hits:
        print(hit, file=sys.stderr)
    if hits:
        print(f"\n{len(hits)} credential-shaped literal(s) without a DUMMY marker",
              file=sys.stderr)
        return 1
    print("secrets guard: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
