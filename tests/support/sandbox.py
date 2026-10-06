"""Sandboxed base TestCase.

Every test gets a private temp tree. HOME, XDG_STATE_HOME, HERDR_PLUGIN_STATE_DIR
and HERDR_BARTENDER_VENDOR_HOOKS_DIR all point inside it. Inherited HERDR_*,
NOTCHBAR_* and proxy variables are scrubbed. PATH is prefixed with
tests/support/shims, so pgrep/ps/osascript/herdr are fakes driven by files in
the sandbox. Runtime globals are reset, and everything is restored in cleanups.

Reconciler hand-offs never start real processes: in-process the
``handoff`` spawner is a ``RecordingSpawner`` (``self.spawner``); subprocesses get
a file-recording spawner through ``tests/support/sitecustom`` on PYTHONPATH
(``self.subprocess_spawns()``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_bartender import clock, handoff, process, runtime
from herdr_bartender.cache import BoundedSessionCache
from herdr_bartender.config import get_sanitized_hostname
from herdr_bartender.paths import get_state_dir

from .fake_clock import FakeClock
from .mock_bridge import MockBridge
from .spawner import RecordingSpawner, read_spawn_log

SUPPORT_DIR = Path(__file__).resolve().parent
SHIM_DIR = SUPPORT_DIR / "shims"
SITECUSTOM_DIR = SUPPORT_DIR / "sitecustom"
REPO_ROOT = SUPPORT_DIR.parent.parent
LAUNCHER = REPO_ROOT / "bin" / "herdr-bartender"

SCRUB_PREFIXES = ("HERDR_", "NOTCHBAR_")
SCRUB_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "XDG_STATE_HOME",
)
DEFAULT_LSTART = "Sat Oct  4 09:00:00 2026"
TEST_DEADLINE_SECONDS = 60.0
DEFAULT_BARTENDER_PID = 424200


class SandboxTestCase(unittest.TestCase):
    """Base class: fresh sandbox, PATH shims, mock bridge and reset runtime state."""

    start_bridge = True
    # Register a fake `Bartender 6` and a live fake Herdr by default so the bridge
    # liveness gate and is_herdr_alive() decide from the shim process table alone.
    # Tests that manage the process table themselves set this to False.
    default_liveness = True

    def setUp(self) -> None:
        super().setUp()
        self._preserve_umask()
        self._install_env()
        self._reset_runtime()
        self._install_spawner()
        if self.default_liveness:
            self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
            self.set_herdr_alive()
        if self.start_bridge:
            self.bridge = MockBridge().start()
            self.addCleanup(self.bridge.stop)
            os.environ["NOTCHBAR_AGENTS_PORT"] = str(self.bridge.port)
            self.mock_url = self.bridge.url
        self.state_dir = get_state_dir()
        self._assert_sandboxed(self.state_dir)
        self.cache_mgr = BoundedSessionCache(self.state_dir)
        self.host = get_sanitized_hostname()

    # -- setup helpers ---------------------------------------------------------
    def _install_env(self) -> None:
        saved = dict(os.environ)
        self.addCleanup(self._restore_env, saved)
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.xdg_state = self.tmp / "xdg-state"
        self.state_root = self.tmp / "state"
        self.vendor_hooks_dir = self.tmp / "vendor-hooks"
        self.sandbox = self.tmp / "sandbox"
        for d in (self.home, self.xdg_state, self.sandbox):
            d.mkdir(parents=True)
        self.procs_file = self.sandbox / "procs.list"
        self.procs_file.write_text("")

        for key in list(os.environ):
            if key.startswith(SCRUB_PREFIXES) or key in SCRUB_NAMES:
                del os.environ[key]
        real_path = saved.get("PATH", "/usr/bin:/bin")
        python_path = os.pathsep.join(p for p in (str(SITECUSTOM_DIR), saved.get("PYTHONPATH", "")) if p)
        os.environ.update({
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.xdg_state),
            "HERDR_PLUGIN_STATE_DIR": str(self.state_root),
            "HERDR_BARTENDER_VENDOR_HOOKS_DIR": str(self.vendor_hooks_dir),
            "HB_TEST_SANDBOX": str(self.sandbox),
            "HB_REAL_PATH": real_path,
            "PATH": f"{SHIM_DIR}{os.pathsep}{real_path}",
            "PYTHONPATH": python_path,
        })

    def _preserve_umask(self) -> None:
        """In-process runtime.mark_process_start() applies umask 077; undo it after each test."""
        saved = os.umask(0o022)
        os.umask(saved)
        self.addCleanup(os.umask, saved)

    @staticmethod
    def _restore_env(saved: dict) -> None:
        os.environ.clear()
        os.environ.update(saved)

    def _reset_runtime(self) -> None:
        saved = runtime.snapshot()
        self.addCleanup(runtime.restore, saved)
        runtime.PROCESS_DEADLINE_SECONDS = TEST_DEADLINE_SECONDS
        runtime.START_TIME = clock.monotonic()
        runtime.IN_CRITICAL_SECTION = False
        runtime.PENDING_WATCHDOG_EXIT = False
        process.reset_caches()
        self.addCleanup(process.reset_caches)

    def _install_spawner(self) -> None:
        self.spawner = RecordingSpawner()
        previous = handoff.set_spawner(self.spawner)
        self.addCleanup(handoff.set_spawner, previous)

    def subprocess_spawns(self) -> list:
        """argv lists that sandboxed subprocesses asked the hand-off spawner to start."""
        return read_spawn_log(self.sandbox)

    def _assert_sandboxed(self, path: Path) -> None:
        resolved = Path(path).resolve()
        if self.tmp not in resolved.parents and resolved != self.tmp:
            raise RuntimeError(f"refusing to run: {resolved} escapes sandbox {self.tmp}")

    # -- fake processes (pgrep/ps shims) --------------------------------------
    def add_fake_process(
        self,
        name: str,
        pid: int | None = None,
        lstart: str = DEFAULT_LSTART,
        comm: str = "",
        cmdline: str = "",
        live: bool = False,
        ancestor: bool = False,
    ) -> int:
        """Register a process the pgrep/ps shims will report.

        With ``live=True`` a real ``sleep`` child is spawned so os.kill-based
        liveness checks see the PID as alive; it is killed in cleanup. With
        ``ancestor=True`` the process is an ancestor of every pgrep caller: the shim,
        like macOS pgrep, then matches it only when ``-a`` is given.
        """
        if live:
            child = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self._reap, child)
            pid = child.pid
        if pid is None:
            raise ValueError("pid is required unless live=True")
        with open(self.procs_file, "a", encoding="utf-8") as f:
            f.write(f"{pid}|{name}|{lstart}|{comm}|{cmdline}\n")
        if ancestor:
            with open(self.sandbox / "ancestors.list", "a", encoding="utf-8") as f:
                f.write(f"{pid}\n")
        return pid

    @staticmethod
    def _reap(child: subprocess.Popen) -> None:
        child.kill()
        child.wait()

    def set_herdr_alive(self, ancestor: bool = False) -> int:
        app = "/Applications/Herdr.app/Contents/MacOS/herdr"
        return self.add_fake_process("herdr", comm=app, cmdline=app, live=True, ancestor=ancestor)

    def clear_fake_processes(self) -> None:
        self.procs_file.write_text("")
        (self.sandbox / "ancestors.list").unlink(missing_ok=True)

    def osascript_calls(self) -> list:
        log = self.sandbox / "osascript.log"
        return log.read_text().splitlines() if log.exists() else []

    # -- misc helpers ------------------------------------------------------------
    def use_fake_clock(self, start: float | None = None) -> FakeClock:
        return FakeClock(start=start).install(self)

    def sid(self, pane: str) -> str:
        return f"herdr:{self.host}:{pane}"

    def run_cli(self, *args: str, input: bytes | str | None = None, env: dict | None = None,
                timeout: float = 15.0) -> subprocess.CompletedProcess:
        """Run bin/herdr-bartender as a subprocess inside this sandbox."""
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *args],
            input=input,
            capture_output=True,
            env=child_env,
            timeout=timeout,
            text=isinstance(input, str) or input is None,
        )
