"""Filesystem locations: state dir, orphan file, vendor hooks dir, launcher."""

from __future__ import annotations

import os
from pathlib import Path

VENDOR_HOOKS_DIR_ENV = "HERDR_BARTENDER_VENDOR_HOOKS_DIR"
ORPHAN_FILE_NAME = ".herdr-bartender-orphans.json"
PRIVATE_UMASK = 0o077
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
OWNERSHIP_MARKER = ".herdr-bartender-owned"   # R72: written only into a state dir this plugin created


def apply_private_umask() -> int:
    """Set the process umask to 077 (Plan §8 L1089); returns the previous mask.

    Call it as the first statement of the process entry point so every file the
    plugin creates (cache, markers, spool, results, logs) is 0600 and every
    directory 0700.
    """
    return os.umask(PRIVATE_UMASK)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make the leaf directory 0700 (Plan §8 L1089).

    Parents keep their default mode: they may be shared (e.g. $XDG_STATE_HOME/herdr).
    Tightening an existing directory is best-effort: a directory we do not own
    (or cannot chmod) stays usable rather than breaking the hook path.
    """
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    try:
        st = path.stat()
        if st.st_mode & 0o077 and st.st_uid == os.getuid():
            os.chmod(path, PRIVATE_DIR_MODE)
    except OSError:
        pass  # best-effort hardening; callers still get a usable directory
    return path


def get_state_dir() -> Path:
    state_env = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if state_env:
        p = Path(state_env)
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state:
            p = Path(xdg_state) / "herdr" / "plugins" / "herdr-bartender"
        else:
            p = Path.home() / ".local" / "state" / "herdr" / "plugins" / "herdr-bartender"
    existed = os.path.lexists(p)
    ensure_private_dir(p)
    if not existed:
        _mark_owned(p)
    return p


def _mark_owned(state_dir: Path) -> None:
    """R72: record that this plugin created ``state_dir`` (rollback removes an overridden dir only if marked).

    A directory that already existed (a custom or shared HERDR_PLUGIN_STATE_DIR) is never claimed.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(str(state_dir / OWNERSHIP_MARKER), flags, PRIVATE_FILE_MODE))
    except OSError:
        pass   # already marked, or not writable: the rollback then keeps the directory (the safe side)


def get_orphan_path() -> Path:
    """Canonical orphan export file, derived from $HOME at call time."""
    return Path.home() / ORPHAN_FILE_NAME


def get_vendor_hooks_dir() -> Path:
    override = os.environ.get(VENDOR_HOOKS_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "Bartender" / "NotchBar" / "AgentStatus" / "hooks"


def package_root() -> Path:
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    return package_root().parent


def launcher_path() -> Path:
    """The bin/herdr-bartender executable used to spawn helper processes."""
    return repo_root() / "bin" / "herdr-bartender"
