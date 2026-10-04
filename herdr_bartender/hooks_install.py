"""Explicit, approving hook installer and the uninstaller (Plan §7.3, CLI paths only).

``install_hooks`` is the *approval* path: it records the clean SHA of every hook
it patched or verified. The background reconciler must never call it; it uses
``hooks_integrity.repair_hooks_if_allowlisted`` instead.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from . import hooks_fs
from .hooks_text import GuardTextError, find_guard, insert_guard, replace_guard, strip_guard
from .log import log_debug
from .paths import get_state_dir, get_vendor_hooks_dir

HOOK_NAMES = ("claude-event-hook.sh", "codex-notify-hook.sh")
SHA_FILE_NAME = "vendor-hook-sha.json"
NO_HOOKS = "NO_HOOKS"
HOOK_NEEDS_REVIEW = "HOOK_NEEDS_REVIEW"
HOOK_REVIEW_ALERTED = ".hook_review_alerted"
REVIEW_FLAGS = (HOOK_NEEDS_REVIEW, HOOK_REVIEW_ALERTED)
PRISTINE_SUFFIX = ".pristine"


@dataclass(frozen=True)
class HookOutcome:
    name: str
    ok: bool
    clean_sha: Optional[str] = None


def present_hooks(hooks_dir: Path) -> List[Path]:
    return [hooks_dir / name for name in HOOK_NAMES if (hooks_dir / name).is_file()]


def load_template_bytes() -> bytes:
    """The guard block as bytes; raises OSError when hook_guard.sh is unreadable."""
    from .hooks import get_hook_guard_template  # late import: hooks re-exports this module

    return get_hook_guard_template().encode("utf-8")


def patched_content(content: bytes, template: bytes) -> bytes:
    """Content with exactly one current guard (in-place swap, or insertion at the anchor)."""
    if find_guard(content) is None:
        return insert_guard(content, template)
    return replace_guard(content, template)


def _say(msg: str) -> None:
    print(msg)
    log_debug(msg)


def _ensure_pristine(hook: Path, clean: bytes, mode: int) -> None:
    pristine = hook.with_name(hook.name + PRISTINE_SUFFIX)
    try:
        if pristine.is_file() and pristine.read_bytes() == clean:
            if stat.S_IMODE(pristine.stat().st_mode) != mode:
                os.chmod(pristine, mode)
            return
        hooks_fs.write_private_atomic(pristine, clean, mode)
    except OSError as e:
        _say(f"[-] WARNING: could not write {pristine.name}: {e}")


def _install_one(hook: Path, template: bytes) -> HookOutcome:
    try:
        content = hook.read_bytes()
        orig_mode = stat.S_IMODE(hook.stat().st_mode)
        target_mode = orig_mode | 0o100
        clean = strip_guard(content)
        new_content = patched_content(content, template)
        if new_content != content:
            hooks_fs.atomic_replace_hook(hook, new_content, target_mode, expected=content)
            _say(f"[+] Installed dedup guard in {hook.name}")
        else:
            if orig_mode != target_mode:
                os.chmod(hook, target_mode)
            _say(f"[=] Dedup guard already current in {hook.name}")
        _ensure_pristine(hook, clean, target_mode)
        return HookOutcome(hook.name, True, hooks_fs.sha256_bytes(clean))
    except GuardTextError as e:
        _say(f"[-] Aborting patch for {hook.name}: {e}; file left unchanged")
    except hooks_fs.HookWriteError as e:
        _say(f"[-] {e}")
    except Exception as e:  # per-hook boundary: never leave the CLI half-way through
        _say(f"[-] Error installing guard in {hook.name}: {e}")
    return HookOutcome(hook.name, False)


def _record_approvals(sha_file: Path, outcomes: Iterable[HookOutcome]) -> bool:
    approved = {o.name: o.clean_sha for o in outcomes if o.ok and o.clean_sha}
    if not approved:
        return True
    merged = {**hooks_fs.load_sha_allowlist(sha_file), **approved}
    try:
        hooks_fs.save_sha_allowlist(sha_file, merged)
        return True
    except OSError as e:
        _say(f"[-] Could not write {sha_file.name}: {e}")
        return False


def install_hooks(state_dir: Optional[Path] = None, hooks_dir: Optional[Path] = None) -> bool:
    """Explicit install/approval. True only when every present hook carries the current guard.

    NO_HOOKS is always cleared (explicit install intent). HOOK_NEEDS_REVIEW and
    .hook_review_alerted are cleared when nothing is left to review: every hook
    was patched/verified, or no vendor hooks exist at all.
    """
    state_dir = state_dir or get_state_dir()
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    hooks_fs.unlink_flags(state_dir, (NO_HOOKS,))
    hook_files = present_hooks(hooks_dir) if hooks_dir.is_dir() else []
    if not hook_files:
        where = hooks_dir if hooks_dir.is_dir() else f"{hooks_dir} (directory not found)"
        _say(f"[-] No target hook scripts found in {where}")
        hooks_fs.unlink_flags(state_dir, REVIEW_FLAGS)
        return False
    try:
        template = load_template_bytes()
    except OSError as e:
        _say(f"[-] Cannot read guard template: {e}")
        return False
    outcomes = tuple(_install_one(hook, template) for hook in hook_files)
    recorded = _record_approvals(state_dir / SHA_FILE_NAME, outcomes)
    all_ok = recorded and all(o.ok for o in outcomes)
    if all_ok:
        hooks_fs.unlink_flags(state_dir, REVIEW_FLAGS)
    else:
        _say("[-] Some hooks were not patched; HOOK_NEEDS_REVIEW (if set) is kept")
    return all_ok


def _remove_redundant_pristine(hook: Path, restored: bytes) -> None:
    pristine = hook.with_name(hook.name + PRISTINE_SUFFIX)
    try:
        if pristine.is_file() and pristine.read_bytes() == restored:
            pristine.unlink()
        elif pristine.exists():
            _say(f"[=] Keeping {pristine.name}: it differs from the restored hook")
    except OSError as e:
        _say(f"[-] WARNING: could not inspect {pristine.name}: {e}")


def _uninstall_one(hook: Path) -> bool:
    try:
        content = hook.read_bytes()
        if find_guard(content) is None:
            _say(f"[=] No dedup guard in {hook.name}; skipping")
            return True
        restored = strip_guard(content)
        hooks_fs.atomic_replace_hook(hook, restored, stat.S_IMODE(hook.stat().st_mode), expected=content)
        _remove_redundant_pristine(hook, restored)
        _say(f"[+] Uninstalled dedup guard from {hook.name}")
        return True
    except GuardTextError as e:
        _say(f"[-] Refusing to edit {hook.name}: {e}; manual inspection required")
    except hooks_fs.HookWriteError as e:
        _say(f"[-] {e}")
    except Exception as e:
        _say(f"[-] Error uninstalling guard from {hook.name}: {e}")
    return False


def uninstall_hooks(state_dir: Optional[Path] = None, hooks_dir: Optional[Path] = None) -> bool:
    """Strip the guard from every named hook; records sticky NO_HOOKS intent first."""
    state_dir = state_dir or get_state_dir()
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    intent_ok = _record_no_hooks(state_dir)
    hook_files = present_hooks(hooks_dir) if hooks_dir.is_dir() else []
    if not hook_files:
        _say(f"[=] No vendor hook scripts found in {hooks_dir}; nothing to uninstall")
        return intent_ok
    results: Tuple[bool, ...] = tuple(_uninstall_one(hook) for hook in hook_files)
    return intent_ok and all(results)


def _record_no_hooks(state_dir: Path) -> bool:
    try:
        hooks_fs.touch_private(state_dir / NO_HOOKS)
        return True
    except OSError as e:
        _say(f"[-] Could not record {NO_HOOKS}: {e}")
        return False
