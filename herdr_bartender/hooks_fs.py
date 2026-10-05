"""Filesystem primitives for the hook installer: atomic hook replace, private state files.

Every writer cleans up its temporary file on any failure (gap
install-tmp-and-sha-file-hygiene) and state files are created 0600 (Plan §8).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Tuple

from .boundedio import HOOK_SCRIPT_MAX_BYTES, SMALL_STATE_MAX_BYTES, read_regular_file
from . import jsonsafe
from .log import log_warning

BASH_CHECK_TIMEOUT_SECONDS = 10.0
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")


class HookWriteError(OSError):
    """A hook patch was refused (validation failure or concurrent modification)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bash_syntax_ok(path: Path) -> Tuple[bool, str]:
    """Run ``bash -n`` on ``path``; returns (ok, stderr text)."""
    res = subprocess.run(
        ["bash", "-n", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=BASH_CHECK_TIMEOUT_SECONDS,
    )
    return res.returncode == 0, res.stderr.decode("utf-8", errors="replace").strip()


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def atomic_replace_hook(hook: Path, new_content: bytes, mode: int, expected: bytes) -> None:
    """Write ``hook.tmp.<pid>``, chmod, ``bash -n``, re-check on-disk bytes, then os.replace.

    Raises HookWriteError when validation fails or the hook changed since it was
    read as ``expected``; any exception leaves the hook untouched and no tmp file.
    """
    tmp_path = hook.with_name(f"{hook.name}.tmp.{os.getpid()}")
    try:
        _write_exclusive(tmp_path, new_content)
        os.chmod(tmp_path, mode)
        ok, err = bash_syntax_ok(tmp_path)
        if not ok:
            raise HookWriteError(f"Syntax validation failed for {hook.name}: {err}")
        if read_regular_file(hook, HOOK_SCRIPT_MAX_BYTES, follow_symlinks=True) != expected:   # R69: bounded
            raise HookWriteError(f"Aborting patch for {hook.name}: file modified on disk during patch preparation")
        os.replace(tmp_path, hook)
    finally:
        _unlink_quietly(tmp_path)


def write_private_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``data`` (tmp in the same dir, fsync, os.replace)."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        _unlink_quietly(tmp_path)


def touch_private(path: Path) -> None:
    """Create ``path`` 0600 if missing (existing files keep their content and mode)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)


def unlink_flags(state_dir: Path, names) -> None:
    for name in names:
        try:
            (state_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log_warning(f"could not remove {name}: {e}")


def load_sha_allowlist(path: Path) -> Dict[str, str]:
    """Known-clean hook SHAs; a missing, corrupt or invalid file yields {} (fail closed)."""
    try:
        raw = jsonsafe.loads(read_regular_file(path, SMALL_STATE_MAX_BYTES))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log_warning(f"unreadable SHA allowlist {path.name}: {e}; treating every hook as unknown")
        return {}
    if not isinstance(raw, dict):
        log_warning(f"SHA allowlist {path.name} is not an object; treating every hook as unknown")
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and _SHA_RE.match(v)}


def save_sha_allowlist(path: Path, mapping: Dict[str, str]) -> None:
    data = json.dumps(dict(sorted(mapping.items())), indent=2).encode("utf-8") + b"\n"
    write_private_atomic(path, data, 0o600)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log_warning(f"could not remove temporary file {path}: {e}")
