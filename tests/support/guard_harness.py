"""Extra helpers for hook-guard tests: per-test PATH shims, umask wrappers, FIFO writers.

Everything here writes only inside the caller-provided sandbox directory.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .guard import run_guard


def make_shim(bin_dir: Path, name: str, body: str) -> Path:
    """Create an executable ``bin_dir/name`` bash shim with ``body``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text("#!/usr/bin/env bash\n" + body + "\n")
    os.chmod(shim, 0o755)
    return shim


def path_with(bin_dir: Path) -> str:
    """PATH with ``bin_dir`` first, then the current (already shimmed) PATH."""
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def failing_mktemp_env(bin_dir: Path) -> dict:
    """env_extra that makes every ``mktemp`` call in the guard fail (exit 1, no output)."""
    make_shim(bin_dir, "mktemp", 'echo "mktemp: simulated failure" >&2\nexit 1')
    return {"PATH": path_with(bin_dir)}


def run_guard_with_umask(script: Path, umask: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run ``script`` from a caller shell whose umask is ``umask`` (e.g. "022")."""
    wrapper = script.with_name(script.name + f".umask{umask}.sh")
    wrapper.write_text(f'#!/usr/bin/env bash\numask {umask}\nexec "{script}" "$@"\n')
    os.chmod(wrapper, 0o755)
    return run_guard(wrapper, *args, **kwargs)


def start_fifo_writer(test_case, fifo_path: Path, shell: str) -> subprocess.Popen:
    """Spawn ``bash -c shell`` (expected to write into ``fifo_path``); its process group is killed/reaped in cleanup."""
    return spawn_group(test_case, ["bash", "-c", shell])


def leftovers(state_dir: Path, *patterns: str) -> list:
    """Paths under ``state_dir`` (recursively) matching any glob pattern."""
    found = []
    for pattern in patterns:
        found.extend(str(p) for p in state_dir.rglob(pattern))
    return sorted(found)


def file_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o7777



def spawn_group(test_case, argv: list) -> subprocess.Popen:
    """Spawn ``argv`` as the leader of a new process group; cleanup SIGKILLs the whole group, then reaps.

    Killing only a ``bash -c "...; :"`` child orphans its running ``sleep`` (the trailing ``:`` stops bash
    from exec-ing it), so the group is signalled instead and nothing outlives the test.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    test_case.addCleanup(_kill_group, proc)
    return proc


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # leader not yet reaped, so its pid still names the group
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
