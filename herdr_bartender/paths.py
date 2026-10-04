"""Filesystem locations: state dir, orphan file, vendor hooks dir, launcher."""

from __future__ import annotations

import os
from pathlib import Path

VENDOR_HOOKS_DIR_ENV = "HERDR_BARTENDER_VENDOR_HOOKS_DIR"
ORPHAN_FILE_NAME = ".herdr-bartender-orphans.json"


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
    p.mkdir(parents=True, exist_ok=True)
    return p


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
