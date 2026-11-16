"""First-party Antigravity hook (agy-notify-hook.sh) installer, uninstaller, and health/config audit.

Unlike claude-event-hook.sh and codex-notify-hook.sh (vendor scripts patched with a
delimited dedup guard and tracked in vendor-hook-sha.json), agy-notify-hook.sh is
shipped first-party in scripts/agy-notify-hook.sh and replaces Bartender's 51-byte
stub directly. It is intentionally excluded from HOOK_NAMES and vendor-hook-sha.json.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from . import hooks_fs, jsonsafe
from .boundedio import HOOK_SCRIPT_MAX_BYTES, read_regular_file, read_regular_file_stat
from .log import log_debug, log_warning
from .paths import get_vendor_hooks_dir, repo_root

AGY_HOOK_NAME = "agy-notify-hook.sh"
AGY_CONFIG_MAX_BYTES = 262144
PRE_TOOL_USE_WARNING = (
    "PreToolUse in hooks.json invokes agy-notify-hook.sh; remove PreToolUse binding "
    "(only PreInvocation, PostInvocation, Stop, SessionEnd are needed)"
)
UNREADABLE_CONFIG_WARNING = "Antigravity hooks.json is unreadable or invalid JSON"


def _say(msg: str) -> None:
    print(msg)
    log_debug(msg)


def agy_source_path() -> Path:
    return repo_root() / "scripts" / AGY_HOOK_NAME


def install_agy_hook(hooks_dir: Optional[Path] = None) -> bool:
    """Deploy scripts/agy-notify-hook.sh atomically to hooks_dir/agy-notify-hook.sh."""
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    if not hooks_dir.is_dir():
        _say(f"[-] Vendor hooks directory not found: {hooks_dir}")
        return False
    target = hooks_dir / AGY_HOOK_NAME
    if os.path.islink(target):
        _say(f"[-] Refusing to overwrite symlink {target}")
        return False
    if os.path.lexists(target) and not target.is_file():
        _say(f"[-] Refusing to overwrite non-regular path {target}")
        return False
    try:
        source_bytes = read_regular_file(agy_source_path(), HOOK_SCRIPT_MAX_BYTES, follow_symlinks=True)
    except OSError as e:
        _say(f"[-] Cannot read {AGY_HOOK_NAME} source: {e}")
        return False

    if target.is_file():
        try:
            current_bytes, st = read_regular_file_stat(target, HOOK_SCRIPT_MAX_BYTES)
        except OSError:
            current_bytes, st = None, None
        if current_bytes == source_bytes and st is not None:
            orig_mode = stat.S_IMODE(st.st_mode)
            if not (orig_mode & 0o100):
                try:
                    os.chmod(target, orig_mode | 0o755)
                except OSError as e:
                    _say(f"[-] Could not mark {AGY_HOOK_NAME} executable: {e}")
                    return False
            _say(f"[=] Antigravity hook already current in {AGY_HOOK_NAME}")
            return True

    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{AGY_HOOK_NAME}.", suffix=".tmp", dir=str(hooks_dir))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(source_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o755)
        ok, err = hooks_fs.bash_syntax_ok(tmp_path)
        if not ok:
            raise hooks_fs.HookWriteError(f"Syntax validation failed for {AGY_HOOK_NAME}: {err}")
        if os.path.islink(target):
            raise hooks_fs.HookWriteError(f"Refusing to overwrite symlink {target}")
        os.replace(tmp_path, target)
        _say(f"[+] Installed Antigravity hook in {AGY_HOOK_NAME}")
        return True
    except hooks_fs.HookWriteError as e:
        _say(f"[-] {e}")
    except (OSError, subprocess.SubprocessError) as e:
        _say(f"[-] Error installing Antigravity hook in {AGY_HOOK_NAME}: {e}")
    except Exception as e:
        _say(f"[-] Error installing Antigravity hook in {AGY_HOOK_NAME}: {e}")
    finally:
        if tmp_path is not None:
            hooks_fs._unlink_quietly(tmp_path)
    return False


def uninstall_agy_hook(hooks_dir: Optional[Path] = None) -> bool:
    """Remove agy-notify-hook.sh when it matches our first-party script; keep custom scripts."""
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    target = hooks_dir / AGY_HOOK_NAME
    if not os.path.lexists(target):
        return True
    if os.path.islink(target):
        log_warning(f"refusing to unlink symlink {target}")
        _say(f"[-] Refusing to remove symlink {AGY_HOOK_NAME}")
        return False
    try:
        current = read_regular_file(target, HOOK_SCRIPT_MAX_BYTES)
    except OSError as e:
        _say(f"[-] Error reading {AGY_HOOK_NAME} during uninstall: {e}")
        return False
    try:
        source_bytes: Optional[bytes] = read_regular_file(
            agy_source_path(), HOOK_SCRIPT_MAX_BYTES, follow_symlinks=True
        )
    except OSError:
        source_bytes = None
    if (source_bytes is not None and current == source_bytes) or b"HERDR_PLUGIN_STATE_DIR" in current:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _say(f"[-] Could not remove {AGY_HOOK_NAME}: {e}")
            return False
        _say(f"[+] Uninstalled Antigravity hook {AGY_HOOK_NAME}")
        return True
    _say(f"[=] Keeping custom {AGY_HOOK_NAME}")
    return True


def verify_agy_hook_intact(hooks_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Pure read-only health check for agy-notify-hook.sh."""
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    if not hooks_dir.is_dir():
        return False, "hooks_dir_missing"
    target = hooks_dir / AGY_HOOK_NAME
    if not os.path.lexists(target):
        return False, "missing"
    if os.path.islink(target):
        return False, "symlink"
    try:
        target_bytes, st = read_regular_file_stat(target, HOOK_SCRIPT_MAX_BYTES)
        source_bytes = read_regular_file(agy_source_path(), HOOK_SCRIPT_MAX_BYTES, follow_symlinks=True)
    except OSError:
        return False, "unreadable"
    if not (stat.S_IMODE(st.st_mode) & 0o100):
        return False, "not_executable"
    if target_bytes != source_bytes:
        return False, "outdated_or_stub"
    return True, "intact"


def default_agy_hooks_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ("." + "gemini") / "config" / "hooks.json"


def _references_agy_hook(value: object) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if AGY_HOOK_NAME in item:
                return True
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def audit_agy_hooks_config(config_path: Optional[Path] = None) -> Tuple[str, ...]:
    """Pure read-only audit of Antigravity hooks.json for PreToolUse bindings to agy-notify-hook.sh."""
    path = config_path or default_agy_hooks_config_path()
    if os.path.islink(path) or not path.is_file():
        return ()
    try:
        raw_bytes = read_regular_file(path, AGY_CONFIG_MAX_BYTES)
        parsed = jsonsafe.loads(raw_bytes)
    except (OSError, ValueError):
        return (UNREADABLE_CONFIG_WARNING,)
    if not isinstance(parsed, dict):
        return (UNREADABLE_CONFIG_WARNING,)

    warnings: List[str] = []
    stack: List[object] = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "PreToolUse" in node and _references_agy_hook(node["PreToolUse"]):
                if PRE_TOOL_USE_WARNING not in warnings:
                    warnings.append(PRE_TOOL_USE_WARNING)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return tuple(warnings)
