"""Test-only interpreter hook (on PYTHONPATH inside the sandbox; never shipped with the plugin).

Any Python process a sandboxed test starts (``bin/herdr-bartender`` via ``run_cli``, or a
``python -c`` snippet) that imports ``herdr_bartender.handoff`` gets, right after that
import, a spawner that only records the requested argv in ``$HB_TEST_SANDBOX/spawns.log``,
so no real detached reconciler ever outlives a test. Processes that never import the
package are untouched. Production code has no test branch: it uses whatever spawner
``herdr_bartender.handoff`` holds.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys

_TARGET = "herdr_bartender.handoff"


def _install_recording_spawner(handoff, sandbox):
    class _FileRecordingSpawner(handoff.Spawner):
        def spawn(self, argv):
            with open(os.path.join(sandbox, "spawns.log"), "a", encoding="utf-8") as log:
                log.write(json.dumps(list(argv)) + "\n")
            return True

    handoff.set_spawner(_FileRecordingSpawner())


class _SpawnerHook(importlib.abc.MetaPathFinder):
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        run = spec.loader.exec_module
        sandbox = self.sandbox

        def exec_module(module):
            run(module)
            _install_recording_spawner(module, sandbox)

        spec.loader.exec_module = exec_module
        return spec


def _run_shadowed_sitecustomize():
    """Run the interpreter's own sitecustomize, which this file hides by sitting first on PYTHONPATH.

    Homebrew's Python, for one, uses it to set ``sys.executable`` to its ``opt/`` path; without it a sandboxed
    child reports the Cellar path and its reconciler argv no longer matches the test process's.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    search = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", search)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


_run_shadowed_sitecustomize()

_SANDBOX = os.environ.get("HB_TEST_SANDBOX")
if _SANDBOX:
    sys.meta_path.insert(0, _SpawnerHook(_SANDBOX))
