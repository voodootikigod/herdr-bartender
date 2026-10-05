"""Non-approving hook integrity check and repair for the background reconciler.

Plan §5.1 item 10 / §7.3, gaps reconciler-allowlist-fails-open and
hook-integrity-allowlist-bypass. Fail closed: a hook is re-patched only when the
SHA-256 of its guard-free bytes equals its own entry in vendor-hook-sha.json.
A missing or corrupt allowlist, a hook without an entry, a mismatch, malformed
markers or an unreadable hook all mean HOOK_NEEDS_REVIEW, and then *no* hook
is patched. This module never writes the allowlist and never clears the
review flags; only the explicit ``install_hooks`` does.

R42: the guard is optional. With no allowlist file and no guard in any hook the
user never opted in (``STATUS_NOT_INSTALLED``): nothing is patched, flagged,
alerted or logged. The review warning is logged once per change of its causes,
which HOOK_NEEDS_REVIEW records for ``--status``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import hooks_fs
from .hooks_install import (
    HOOK_NEEDS_REVIEW,
    HOOK_REVIEW_ALERTED,
    NO_HOOKS,
    SHA_FILE_NAME,
    load_template_bytes,
    patched_content,
    present_hooks,
)
from .hooks_text import GuardTextError, guard_block, is_legacy_layout, legacy_clean_variants, strip_guard
from .log import log_debug, log_warning
from .paths import get_state_dir, get_vendor_hooks_dir

STATUS_INTACT = "intact"
STATUS_NEEDS_PATCH = "needs_patch_known_clean"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_NO_HOOKS = "no_hooks"
STATUS_DISABLED = "disabled"
STATUS_ABSENT = "absent"  # no vendor hooks installed on this host: nothing to protect
STATUS_NOT_INSTALLED = "not_installed"  # R42: dedup never opted into (no allowlist file, no guard in any hook)

HOOK_INTACT = "intact"
HOOK_STALE_GUARD = "stale_guard"
HOOK_MISSING_GUARD = "missing_guard"
HOOK_MALFORMED = "malformed"
HOOK_UNREADABLE = "unreadable"
_PATCHABLE = (HOOK_STALE_GUARD, HOOK_MISSING_GUARD)

ALERT_TIMEOUT_SECONDS = 2.0
_REASONS_MAX_BYTES = 4096
ALERT_SCRIPT = (
    'display notification "Vendor hook updated. Run \\"herdr-bartender --install-hooks\\" '
    'to re-enable dedup." with title "Herdr Bartender Bridge"'
)


@dataclass(frozen=True)
class HookCheck:
    name: str
    status: str
    clean_sha: Optional[str] = None
    known_sha: Optional[str] = None
    detail: str = ""

    @property
    def allowlisted(self) -> bool:
        return self.known_sha is not None and self.clean_sha == self.known_sha

    @property
    def needs_patch(self) -> bool:
        return self.status in _PATCHABLE


@dataclass(frozen=True)
class IntegrityResult:
    status: str
    hooks: Tuple[HookCheck, ...] = ()

    def as_dict(self) -> dict:
        return {"status": self.status, "hooks": [{**asdict(h), "allowlisted": h.allowlisted} for h in self.hooks]}


@dataclass(frozen=True)
class RepairResult:
    status: str
    repaired: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    review_flagged: bool = False
    alert_sent: bool = False


def approved_clean(content: bytes, known_sha: Optional[str]) -> Tuple[bytes, str]:
    """(guard-free bytes, their SHA): the variant the allowlist approved, else ``strip_guard``'s.

    R37: a legacy (monolith) layout may have been approved with blank lines at our insertion point
    (``hooks_text.legacy_clean_variants``); only whitespace there differs, never vendor content.
    """
    stripped = strip_guard(content)
    for clean in (stripped, *legacy_clean_variants(content)):
        sha = hooks_fs.sha256_bytes(clean)
        if known_sha is not None and sha == known_sha:
            return clean, sha
    return stripped, hooks_fs.sha256_bytes(stripped)


def _check_one(hook: Path, known: dict, template: Optional[bytes]) -> HookCheck:
    known_sha = known.get(hook.name)
    try:
        content = hook.read_bytes()
        block = guard_block(content)
        _, clean_sha = approved_clean(content, known_sha)
        legacy = is_legacy_layout(content)
    except GuardTextError as e:
        return HookCheck(hook.name, HOOK_MALFORMED, known_sha=known_sha, detail=str(e))
    except OSError as e:
        return HookCheck(hook.name, HOOK_UNREADABLE, known_sha=known_sha, detail=str(e))
    if block is None:
        status = HOOK_MISSING_GUARD
    elif legacy or (template is not None and block != template):
        status = HOOK_STALE_GUARD   # R37: a legacy layout is re-laid out even when its block is current
    else:
        status = HOOK_INTACT
    return HookCheck(hook.name, status, clean_sha, known_sha)


def _overall(checks: Tuple[HookCheck, ...], opted_in: bool) -> str:
    if not checks:
        return STATUS_ABSENT
    if not opted_in and all(c.status == HOOK_MISSING_GUARD for c in checks):
        return STATUS_NOT_INSTALLED
    if any(c.status in (HOOK_MALFORMED, HOOK_UNREADABLE) for c in checks):
        return STATUS_NEEDS_REVIEW
    # R61: a current guard is not proof of approved content - every guarded or patchable hook's guard-free bytes
    # must still match its own allowlist entry, or the operator reviews it.
    if any((c.needs_patch or c.status == HOOK_INTACT) and not c.allowlisted for c in checks):
        return STATUS_NEEDS_REVIEW
    return STATUS_NEEDS_PATCH if any(c.needs_patch for c in checks) else STATUS_INTACT


def _optional_template() -> Optional[bytes]:
    try:
        return load_template_bytes()
    except OSError as e:
        log_warning(f"guard template unreadable ({e}); stale-guard detection skipped")
        return None


def check_hook_integrity(state_dir: Optional[Path] = None, hooks_dir: Optional[Path] = None) -> IntegrityResult:
    """Pure inspection (no writes). See module docstring for the status rules."""
    state_dir = state_dir or get_state_dir()
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    if (state_dir / "DISABLED").exists():
        return IntegrityResult(STATUS_DISABLED)
    if (state_dir / NO_HOOKS).exists():
        return IntegrityResult(STATUS_NO_HOOKS)
    hook_files = present_hooks(hooks_dir) if hooks_dir.is_dir() else []
    sha_file = state_dir / SHA_FILE_NAME
    known = hooks_fs.load_sha_allowlist(sha_file)
    template = _optional_template() if hook_files else None
    checks = tuple(_check_one(h, known, template) for h in hook_files)
    return IntegrityResult(_overall(checks, opted_in=os.path.lexists(sha_file)), checks)


def verify_vendor_hooks_intact(hooks_dir: Optional[Path] = None) -> Tuple[bool, list]:
    """(all present hooks carry the CURRENT guard, names that do not). (False, []) without a hooks dir.

    A stale or legacy-layout guard is not intact (``--health``); an unreadable template skips that comparison.
    """
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    if not hooks_dir.is_dir():
        return False, []
    hook_files = present_hooks(hooks_dir)
    template = _optional_template() if hook_files else None
    checks = tuple(_check_one(h, {}, template) for h in hook_files)
    missing = [c.name for c in checks if c.status != HOOK_INTACT]
    return not missing, missing


def _repair_one(hook: Path, expected_sha: str, template: bytes) -> bool:
    """Re-patch one hook iff its *current* clean bytes still hash to ``expected_sha``.

    The current guard goes over exactly the approved guard-free bytes (R37: a legacy layout is re-laid out).
    """
    try:
        content = hook.read_bytes()
        clean, clean_sha = approved_clean(content, expected_sha)
        if clean_sha != expected_sha:
            log_warning(f"{hook.name} changed since the integrity check; repair refused")
            return False
        new_content = patched_content(content, template, clean)
        mode = stat.S_IMODE(hook.stat().st_mode) | 0o100
        hooks_fs.atomic_replace_hook(hook, new_content, mode, expected=content)
        log_debug(f"Re-patched allowlisted vendor hook {hook.name}")
        return True
    except (GuardTextError, OSError, subprocess.SubprocessError) as e:
        log_warning(f"automatic repair of {hook.name} failed: {e}")
        return False


def _send_review_alert() -> bool:
    try:
        subprocess.run(["osascript", "-e", ALERT_SCRIPT], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=ALERT_TIMEOUT_SECONDS)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log_warning(f"HOOK_NEEDS_REVIEW notification failed: {e}")
        return False


def review_reasons(result: IntegrityResult) -> str:
    """One line naming each hook's status, e.g. ``claude-event-hook.sh=missing_guard/unknown-sha``."""
    return ", ".join(f"{c.name}={c.status}{'' if c.allowlisted else '/unknown-sha'}" for c in result.hooks)


def recorded_review_reasons(state_dir: Path) -> str:
    """The causes HOOK_NEEDS_REVIEW records ("" when missing, unreadable or a bare touch)."""
    try:
        raw = (state_dir / HOOK_NEEDS_REVIEW).read_bytes()[:_REASONS_MAX_BYTES]
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").strip()


def flag_hook_review(state_dir: Path, result: IntegrityResult) -> Tuple[bool, bool]:
    """Record the causes in HOOK_NEEDS_REVIEW, warn once per change of causes, alert once (.hook_review_alerted).

    Every reconciler pass (20s) re-checks; an unchanged review is not logged again (R42).
    """
    reasons = review_reasons(result)
    flag = state_dir / HOOK_NEEDS_REVIEW
    changed = not flag.exists() or recorded_review_reasons(state_dir) != reasons
    if changed:
        log_warning(f"vendor hook needs review ({reasons}); automatic re-patching skipped")
    try:
        hooks_fs.touch_private(flag)
        if changed:
            hooks_fs.write_private_atomic(flag, reasons.encode("utf-8") + b"\n", 0o600)
    except OSError as e:
        log_warning(f"could not create {HOOK_NEEDS_REVIEW}: {e}")
        return False, False
    alerted = state_dir / HOOK_REVIEW_ALERTED
    if alerted.exists():
        return True, False
    try:
        hooks_fs.touch_private(alerted)
    except OSError as e:
        log_warning(f"could not create {HOOK_REVIEW_ALERTED}: {e}")
        return True, False
    return True, _send_review_alert()


def repair_hooks_if_allowlisted(state_dir: Optional[Path] = None, hooks_dir: Optional[Path] = None) -> RepairResult:
    """Reconciler step (Plan §5.1 item 10): repair known-clean hooks, otherwise flag review.

    Never approves content (vendor-hook-sha.json is read-only here) and never
    clears HOOK_NEEDS_REVIEW / .hook_review_alerted.
    """
    state_dir = state_dir or get_state_dir()
    hooks_dir = hooks_dir or get_vendor_hooks_dir()
    result = check_hook_integrity(state_dir, hooks_dir)
    if result.status == STATUS_NEEDS_REVIEW:
        flagged, alert_sent = flag_hook_review(state_dir, result)
        return RepairResult(result.status, review_flagged=flagged, alert_sent=alert_sent)
    if result.status != STATUS_NEEDS_PATCH:
        return RepairResult(result.status)
    try:
        template = load_template_bytes()
    except OSError as e:
        log_warning(f"guard template unreadable; cannot repair hooks: {e}")
        return RepairResult(result.status, failed=tuple(c.name for c in result.hooks if c.needs_patch))
    targets = tuple(c for c in result.hooks if c.needs_patch)
    outcomes = tuple((c.name, _repair_one(hooks_dir / c.name, c.known_sha or "", template)) for c in targets)
    return RepairResult(
        result.status,
        repaired=tuple(name for name, ok in outcomes if ok),
        failed=tuple(name for name, ok in outcomes if not ok),
    )
