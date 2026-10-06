"""Test spawners: record reconciler hand-offs instead of starting detached processes.

``RecordingSpawner`` is installed in-process by ``SandboxTestCase``. Subprocesses started by
the sandbox (``run_cli``) get ``FileRecordingSpawner`` through ``sitecustom/sitecustomize.py``,
which appends each argv as a JSON line to ``$HB_TEST_SANDBOX/spawns.log``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Sequence

from herdr_bartender import handoff, runtime

SPAWN_LOG_NAME = "spawns.log"


class RecordingSpawner(handoff.Spawner):
    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.in_critical_section: List[bool] = []

    def spawn(self, argv: Sequence[str]) -> bool:
        self.calls.append(list(argv))
        self.in_critical_section.append(runtime.IN_CRITICAL_SECTION)
        return True

    def reset(self) -> None:
        self.calls.clear()
        self.in_critical_section.clear()


def read_spawn_log(sandbox: Path) -> List[List[str]]:
    path = Path(sandbox) / SPAWN_LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
