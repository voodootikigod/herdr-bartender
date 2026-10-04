"""Sandboxed vendor-hook fixtures for installer / integrity / rollback tests.

All helpers take an explicit directory (the sandbox vendor hooks dir); none of
them ever resolve the real Bartender hooks path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

CLAUDE_HOOK = "claude-event-hook.sh"
CODEX_HOOK = "codex-notify-hook.sh"
BEGIN = b"# BEGIN HERDR-BARTENDER DEDUP GUARD"
END = b"# END HERDR-BARTENDER DEDUP GUARD"

VENDOR_CLAUDE = b'#!/bin/bash\nset -u\n\nHOOK_JSON=$(cat)\necho "claude:$HOOK_JSON"\n'
VENDOR_CODEX = b"#!/usr/bin/env bash\nset -euo pipefail\n# codex notify hook\nprintf 'codex:%s\\n' \"${1:-}\"\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seed_hook(hooks_dir: Path, name: str, content: bytes, mode: int = 0o755) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / name
    path.write_bytes(content)
    os.chmod(path, mode)
    return path


def seed_vendor_hooks(hooks_dir: Path, claude_mode: int = 0o755, codex_mode: int = 0o755) -> dict:
    """Seed both named vendor hooks; returns {name: Path}."""
    return {
        CLAUDE_HOOK: seed_hook(hooks_dir, CLAUDE_HOOK, VENDOR_CLAUDE, claude_mode),
        CODEX_HOOK: seed_hook(hooks_dir, CODEX_HOOK, VENDOR_CODEX, codex_mode),
    }


def write_allowlist(state_dir: Path, mapping: dict) -> Path:
    path = state_dir / "vendor-hook-sha.json"
    path.write_text(json.dumps(mapping))
    return path


def read_allowlist(state_dir: Path) -> dict:
    return json.loads((state_dir / "vendor-hook-sha.json").read_text())


def guard_count(path: Path) -> tuple:
    data = path.read_bytes()
    return data.count(BEGIN), data.count(END)


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o7777


def call_quietly(fn, *args, **kwargs):
    """Call ``fn`` capturing stdout; returns (result, captured_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def tmp_leftovers(hooks_dir: Path) -> list:
    return sorted(p.name for p in hooks_dir.glob("*.tmp*"))
