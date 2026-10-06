"""npx adversarial-review gate round 16: strict capped directories and non-blocking bounded reads (R64)."""

import os
import subprocess
import sys
import unittest

from herdr_bartender import results, spool
from herdr_bartender.boundedio import capped_directory
from herdr_bartender.delivery_state import Outcome, Transmission
from herdr_bartender.envelopes import read_json
from herdr_bartender.results import ResultWriteError, write_result_envelope
from herdr_bartender.spool import SpoolWriteError, enqueue_spool, spool_dir
from herdr_bartender.vendor import read_vendor_file, vendor_active_path
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.nonblocking import call_without_blocking

WRITER = """
import os
import sys
sys.path.insert(0, sys.argv[1])
from herdr_bartender.spool import SpoolWriteError, enqueue_spool
ok = 0
for n in range(int(sys.argv[2])):
    try:
        enqueue_spool("pane.closed", {"pane_id": "w1:p%d_%d" % (os.getpid(), n), "workspace_id": "w1"}, {})
        ok += 1
    except SpoolWriteError:
        pass
print(ok)
"""


class StrictSpoolCeilingTests(SandboxTestCase):
    def test_concurrent_writers_never_exceed_the_close_ceiling(self):
        """R64/R66: count-then-write is serialised by spool/.lock, so racing writers stop exactly at the ceiling."""
        directory = spool_dir()
        for n in range(spool.CLOSE_HARD_CAP - 20):   # pre-filled distinct pending closes
            (directory / f"{n:020d}_1_1{spool.CLOSE_MARK}{n:032x}.json").write_text("{}")
        procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(REPO_ROOT), "15"], stdout=subprocess.PIPE,
                                  env=os.environ.copy()) for _ in range(6)]
        written = sum(int(p.communicate(timeout=120)[0] or 0) for p in procs)
        self.assertEqual(len(list(directory.glob("*.json"))), spool.CLOSE_HARD_CAP)
        self.assertEqual(written, 20, "exactly the remaining room was written")

    def test_planted_fifo_in_spool_is_not_read_blocking(self):
        """R64: a FIFO named like an envelope is rejected without blocking the reader."""
        fifo = spool_dir() / "00000000000000000001_1_1.json"
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            call_without_blocking(self, fifo, lambda: read_json(fifo))


class ResultsCeilingTests(SandboxTestCase):
    def test_results_directory_is_capped(self):
        """R64: Step C result envelopes stop at RESULTS_HARD_CAP instead of filling the state volume."""
        directory = results.results_dir()
        with capped_directory(directory, results.RESULTS_HARD_CAP + 1):
            pass
        for n in range(results.RESULTS_HARD_CAP):
            (directory / f"{n:020d}_1_1.json").write_text("{}")
        tx = Transmission(self.sid("w1:pR"), "w1:pR", "Working", 2, "Claude (Herdr)", None, 0, 3, 10, 5000)
        with self.assertRaises(ResultWriteError):
            write_result_envelope(tx, Outcome("success"))
        self.assertEqual(len(list(directory.glob("*.json"))), results.RESULTS_HARD_CAP)


class VendorSpecialFileTests(SandboxTestCase):
    def _path(self):
        path = vendor_active_path("w1:pFifo")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_fifo_vendor_active_does_not_block(self):
        """R64: a FIFO at .vendor_active is unusable (bare touch, never retired) and never blocks the lock holder."""
        path = self._path()
        os.mkfifo(path)
        record = call_without_blocking(self, path, lambda: read_vendor_file(path))
        self.assertIsNotNone(record)
        self.assertIsNone(record.content)

    def test_symlinked_vendor_active_is_not_followed(self):
        path = self._path()
        target = self.tmp / "elsewhere.json"
        target.write_text('{"vendor_session_id":"a82e9232-0d36-43e6-8b02-1b953babd13e"}')
        path.symlink_to(target)
        record = read_vendor_file(path)
        self.assertIsNone(record.content, "a symlink is never followed")


if __name__ == "__main__":
    unittest.main()
