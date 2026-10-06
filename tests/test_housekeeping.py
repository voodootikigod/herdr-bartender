"""Startup sweep of stale temporary files (Plan §6.2 step 4)."""

import os
import time
import unittest

from herdr_bartender.cache import BoundedSessionCache
from herdr_bartender.housekeeping import STALE_TMP_SECONDS, sweep_stale_temp_files
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase


class StaleTempSweepTests(SandboxTestCase):
    start_bridge = False

    def _make(self, path, age):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("partial")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def _fixtures(self, age):
        orphan = get_orphan_path()
        return [
            self._make(self.state_dir / f"active-sessions.json.tmp.{age}", age),
            self._make(self.state_dir / f".guard_stdin.{age}", age),
            self._make(self.state_dir / "spool" / f"0001_{age}.json.tmp", age),
            self._make(self.state_dir / "results" / f"0001_{age}.json.tmp", age),
            self._make(orphan.with_name(f"{orphan.name}.tmp.{age}"), age),
            self._make(self.state_dir / f"active-sessions.json.salvage.{age}", age),
            self._make(orphan.with_name(f"{orphan.name}.pending") / f".journal-{age}.tmp", age),
            self._make(self.state_dir / "panes" / f"77312e70.vendor_active.claim-1-{age}", age),
        ]

    def test_stale_tmp_files_older_than_60s_are_swept(self):
        """Plan §6.2 step 4 (gaps stale-tmp-sweep, test-watchdog-tmp-orphanlock): cache, salvage, spool, results,
        orphan and orphan-journal tmp files, interrupted .vendor_active claims and guard stdin captures >60s old
        are unlinked; fresh ones (a live writer) and finished envelopes are kept."""
        self.assertEqual(STALE_TMP_SECONDS, 60)
        stale = self._fixtures(120)
        fresh = self._fixtures(5)
        keep = self._make(self.state_dir / "spool" / "0002_ready.json", 600)
        removed = sweep_stale_temp_files(self.state_dir, now=time.time())
        self.assertEqual(sorted(removed), sorted(stale))
        for path in stale:
            self.assertFalse(path.exists(), path)
        for path in fresh + [keep]:
            self.assertTrue(path.exists(), path)

    def test_cache_construction_sweeps_on_startup(self):
        """Plan §6.2 step 4: constructing the cache (process startup) runs the sweep."""
        stale = self._fixtures(300)
        BoundedSessionCache(self.state_dir)
        for path in stale:
            self.assertFalse(path.exists(), path)


if __name__ == "__main__":
    unittest.main()
