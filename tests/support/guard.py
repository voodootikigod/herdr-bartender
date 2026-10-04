"""Helpers for exercising the vendor-hook dedup guard (HOOK_GUARD_TEMPLATE)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE


def write_guard_script(path: Path, tail: str, preamble: str = "", guard: str = HOOK_GUARD_TEMPLATE,
                       shebang: str = "#!/bin/bash\nset -u") -> Path:
    """Write ``<shebang><preamble><guard><tail>`` as an executable vendor-hook stand-in."""
    pre = f"{preamble}\n" if preamble else ""
    path.write_text(f"{shebang}\n{pre}{guard}\n{tail}\n")
    os.chmod(path, 0o755)
    return path


def run_guard(script: Path, *args: str, input=None, env_extra: dict | None = None,
              stdin=None, text: bool = True, timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Run a guard script with the sandboxed environment.

    stdin defaults to /dev/null (never the test runner's own stdin) unless
    ``input`` or ``stdin`` is given.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    if input is None and stdin is None:
        stdin = subprocess.DEVNULL
    return subprocess.run(
        [str(script), *args],
        input=input,
        stdin=stdin,
        capture_output=True,
        text=text,
        env=env,
        timeout=timeout,
    )
