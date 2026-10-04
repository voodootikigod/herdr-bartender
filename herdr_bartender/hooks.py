"""Vendor hook dedup guard: template access plus the installer / integrity public API.

The guard itself lives in hook_guard.sh next to this module. Implementation is
split by concern:

- hooks_text: byte-exact insert/strip/marker validation (shared with rollback).
- hooks_fs: atomic hook replacement and private state-file writes.
- hooks_install: explicit, approving ``install_hooks`` and ``uninstall_hooks`` (CLI).
- hooks_integrity: non-approving ``check_hook_integrity`` and
  ``repair_hooks_if_allowlisted`` for the background reconciler.
"""

from __future__ import annotations

from pathlib import Path

from .paths import get_vendor_hooks_dir

_GUARD_RESOURCE = Path(__file__).with_name("hook_guard.sh")

_guard_template_cache: str | None = None


def _load_guard_template() -> str:
    text = _GUARD_RESOURCE.read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text


def get_hook_guard_template() -> str:
    """Return the guard shell block, reading hook_guard.sh lazily on first use.

    Deliberately not done at import: a missing/unreadable resource must only break
    hook install/repatch, never the Herdr event hot path or --cleanup.
    Raises OSError when the resource cannot be read.
    """
    global _guard_template_cache
    if _guard_template_cache is None:
        _guard_template_cache = _load_guard_template()
    return _guard_template_cache


def __getattr__(name: str):
    # PEP 562: keep `from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE` working
    # without an import-time file read.
    if name == "HOOK_GUARD_TEMPLATE":
        return get_hook_guard_template()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .hooks_install import (  # noqa: E402  (re-exported public API)
    HOOK_NAMES,
    HOOK_NEEDS_REVIEW,
    HOOK_REVIEW_ALERTED,
    NO_HOOKS,
    SHA_FILE_NAME,
    install_hooks,
    uninstall_hooks,
)
from .hooks_integrity import (  # noqa: E402
    STATUS_ABSENT,
    STATUS_DISABLED,
    STATUS_INTACT,
    STATUS_NEEDS_PATCH,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_HOOKS,
    HookCheck,
    IntegrityResult,
    RepairResult,
    check_hook_integrity,
    flag_hook_review,
    repair_hooks_if_allowlisted,
    verify_vendor_hooks_intact,
)

__all__ = [
    "HOOK_NAMES",
    "HOOK_NEEDS_REVIEW",
    "HOOK_REVIEW_ALERTED",
    "NO_HOOKS",
    "SHA_FILE_NAME",
    "STATUS_ABSENT",
    "STATUS_DISABLED",
    "STATUS_INTACT",
    "STATUS_NEEDS_PATCH",
    "STATUS_NEEDS_REVIEW",
    "STATUS_NO_HOOKS",
    "HookCheck",
    "IntegrityResult",
    "RepairResult",
    "check_hook_integrity",
    "flag_hook_review",
    "get_hook_guard_template",
    "get_vendor_hooks_dir",
    "install_hooks",
    "repair_hooks_if_allowlisted",
    "uninstall_hooks",
    "verify_vendor_hooks_intact",
]
