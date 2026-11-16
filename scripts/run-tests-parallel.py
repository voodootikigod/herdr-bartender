#!/usr/bin/env python3
"""Parallel stdlib unittest runner across isolated SandboxTestCase modules."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_WORKERS = min(6, max(2, (os.cpu_count() or 4) // 2))
MODULE_TIMEOUT_SECONDS = 240


def _run_module(module_name: str) -> tuple[str, int, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", module_name],
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=MODULE_TIMEOUT_SECONDS,
    )
    return module_name, proc.returncode, proc.stderr, time.monotonic() - t0


def main() -> int:
    t0 = time.monotonic()
    tests_dir = REPO_ROOT / "tests"
    modules = sorted(f"tests.{p.stem}" for p in tests_dir.glob("test_*.py") if p.is_file())
    if not modules:
        print("No test modules found under tests/", file=sys.stderr)
        return 2

    failures: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for mod, code, err, elapsed in pool.map(_run_module, modules):
            if code != 0:
                # Retry once serially in case a timing-sensitive 1.5s watchdog test saw transient CPU contention
                mod2, code2, err2, _ = _run_module(mod)
                if code2 != 0:
                    failures.append((mod2, err2))

    total = time.monotonic() - t0
    if failures:
        for mod, err in failures:
            print(f"\n=== FAIL: {mod} ===\n{err}", file=sys.stderr)
        print(f"\nFAILED ({len(failures)}/{len(modules)} module(s)) in {total:.1f}s", file=sys.stderr)
        return 1

    print(f"OK ({len(modules)} test modules passed in {total:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
