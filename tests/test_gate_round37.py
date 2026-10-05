"""npx adversarial-review gate round 37: a same-session removal never overwrites a newer export (R85); the hook
guard captures at most 1 MiB of stdin and splices the rest through, byte-exact (R85)."""

import json
import unittest

from herdr_bartender import orphans
from herdr_bartender.orphans import (
    _lock_path,
    export_orphan_record,
    flush_pending_orphan_ops,
    journaled_export_records,
    read_orphan_records,
    remove_orphan_record,
    write_orphan_sessions,
)
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase, run_guard
from tests.support.guard_harness import leftovers
from tests.support.lock_holder import hold_lock
from tests.test_hooks_guard import _GuardCase
from tests import test_hooks_guard_predicates as predicates

SID = "herdr:h:w1:pRm"


class RemovalNeverOverwritesNewerExportTests(SandboxTestCase):
    def test_journaled_newer_export_survives_an_older_removal(self):
        holder = hold_lock(self, _lock_path(get_orphan_path()), seconds=30)
        export_orphan_record(SID, {"pane_id": "w1:pRm", "seq": 7, "desired_state": "Ended"})
        remove_orphan_record(SID, upto_seq=5)   # an earlier turn's Ended confirmed
        self.assertEqual([r["seq"] for r in journaled_export_records()[SID]], [7], "the newer export stays queued")
        holder.release()
        self.assertTrue(flush_pending_orphan_ops())
        self.assertEqual(read_orphan_records(get_orphan_path())[SID]["seq"], 7)

    def test_fold_keeps_a_newer_record_against_an_older_removal(self):
        write_orphan_sessions(get_orphan_path(), {SID: {"pane_id": "w1:pRm", "seq": 7, "desired_state": "Ended"}})
        self.assertTrue(remove_orphan_record(SID, blocking=True, upto_seq=5))
        self.assertIn(SID, read_orphan_records(get_orphan_path()))
        self.assertTrue(remove_orphan_record(SID, blocking=True, upto_seq=7))
        self.assertNotIn(SID, read_orphan_records(get_orphan_path()))


class CaptureSizeBoundTests(_GuardCase):
    PAYLOAD = bytes(range(256)) * 12288 + b"tail\n"   # 3 MiB + 5 bytes

    def _check(self, env_extra):
        self.set_herdr_dead()
        script = self.script("guard-big.sh", "cat")
        res = run_guard(script, input=self.PAYLOAD, env_extra={"HERDR_PANE_ID": "w1:pBig", **env_extra}, text=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, self.PAYLOAD, f"got {len(res.stdout)} bytes, want {len(self.PAYLOAD)}")
        self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*", ".guard_stdin.*.fifo"), [])

    def test_perl_capture_is_bounded_and_byte_exact(self):
        self._check({})

    def test_python_capture_is_bounded_and_byte_exact(self):
        self._check({"PATH": predicates.PythonCaptureFallbackTests.no_perl_path(self)})


if __name__ == "__main__":
    unittest.main()
