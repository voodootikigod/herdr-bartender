"""The orphan file is never lost to a shape the writer does not understand (Plan §9.2, R10), and its writes are durable.

Gaps (W4 review): one parser for writer and reader (``{"version":1,"sessions":{}}``, a bare ``{sid: record}`` mapping,
the old monolith's list form); an unparseable file is left untouched with its journal; the rename is made durable by
fsyncing the parent directory before an export is reported done (so the R12 evict can follow it).
"""

import json
import os
import stat
import unittest
from unittest import mock

from herdr_bartender import orphans
from herdr_bartender.orphans import (
    _lock_path,
    export_orphan_record,
    flush_pending_orphan_ops,
    orphan_pane_ids,
    pending_dir_for,
)
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.replay import run_replay_orphans
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import read_cache, seed, session


class OrphanShapeCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.path = get_orphan_path()
        self.l1, self.l2 = self.sid("w1:pL1"), self.sid("w1:pL2")

    def sessions_in_file(self):
        return json.loads(self.path.read_text())["sessions"]

    def journal_entries(self):
        pending = pending_dir_for(self.path)
        return sorted(pending.glob("*.json")) if pending.is_dir() else []

    def journal_export(self, sid):
        holder = hold_lock(self, _lock_path(self.path))
        self.assertIs(export_orphan_record(sid, {"agent": "Herdr", "pane_id": sid.rsplit(":", 2)[-2:][0]}), False)
        holder.release()


class LegacyShapeTests(OrphanShapeCase):
    def test_bare_mapping_survives_an_export(self):
        self.path.write_text(json.dumps({self.l1: {"agent": "Herdr"}, self.l2: {"agent": "Herdr"}}))
        new = self.sid("w1:pNew")
        self.assertIs(export_orphan_record(new, {"agent": "Herdr"}), True)
        self.assertEqual(sorted(self.sessions_in_file()), sorted([self.l1, self.l2, new]))
        self.assertEqual(json.loads(self.path.read_text())["version"], 1, "normalised to the canonical shape")

    def test_list_shape_and_journal_survive_a_replay(self):
        """The journal fold of a replay understands the legacy list form: nothing is dropped (all rejected here)."""
        self.path.write_text(json.dumps([{"session_id": self.l1, "agent": "Herdr"},
                                         {"session_id": self.l2, "agent": "Herdr"}]))
        journaled = self.sid("w1:pJ")
        self.journal_export(journaled)
        self.bridge.return_code = 400
        self.assertIs(run_replay_orphans(str(self.path), bridge_url=self.mock_url, quiet=True), False)
        self.assertEqual(sorted(self.sessions_in_file()), sorted([self.l1, self.l2, journaled]))

    def test_reconciler_horizon_export_keeps_legacy_records(self):
        self.path.write_text(json.dumps({self.l1: {"agent": "Herdr"}}))
        sid = self.sid("w1:pHz")
        seed(self.cache_mgr, {sid: session("w1:pHz", "Ended", seq=3, delivered=False, now=1.0,
                                           delivery_status="retryable_exhausted", orphaned_at=1.0)})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertNotIn(sid, read_cache(self.cache_mgr)["sessions"])
        self.assertEqual(sorted(self.sessions_in_file()), sorted([self.l1, sid]))


class UnparseableFileTests(OrphanShapeCase):
    CONTENTS = ('{"version": 1, "sess', json.dumps({"version": 1, "sessions": ["x"]}), json.dumps(42))

    def test_export_journals_instead_of_overwriting(self):
        for content in self.CONTENTS:
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertIs(export_orphan_record(self.sid("w1:pNew"), {"agent": "Herdr"}), False)
                self.assertEqual(self.path.read_text(), content)
                self.assertTrue(self.journal_entries(), "the export waits in the journal")
                self.assertIs(flush_pending_orphan_ops(blocking=True), False)
                self.assertEqual(self.path.read_text(), content)
                self.assertTrue(self.journal_entries())

    def test_replay_leaves_file_and_journal_untouched(self):
        content = self.CONTENTS[0]
        self.path.write_text(content)
        self.journal_export(self.sid("w1:pJ"))
        before = [p.read_text() for p in self.journal_entries()]
        self.assertIs(run_replay_orphans(str(self.path), bridge_url=self.mock_url, quiet=True), False)
        self.assertEqual(self.path.read_text(), content)
        self.assertEqual([p.read_text() for p in self.journal_entries()], before)
        self.assertEqual(self.bridge.history, [])

    def test_pane_ids_unknown_for_an_unparseable_file(self):
        self.path.write_text(self.CONTENTS[0])
        self.assertIsNone(orphan_pane_ids(), "prune protection deferred, never 'no orphan panes'")


class DurabilityTests(OrphanShapeCase):
    def _record_directory_fsyncs(self):
        synced = []
        real = os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced.append(os.fstat(fd).st_ino)
            return real(fd)

        return synced, mock.patch.object(orphans.os, "fsync", side_effect=fsync)

    def test_export_fsyncs_the_orphan_directory(self):
        synced, patch = self._record_directory_fsyncs()
        with patch:
            self.assertIs(export_orphan_record(self.sid("w1:pD"), {"agent": "Herdr"}), True)
        self.assertIn(self.path.parent.stat().st_ino, synced)

    def test_journal_entry_fsyncs_the_journal_directory(self):
        synced, patch = self._record_directory_fsyncs()
        holder = hold_lock(self, _lock_path(self.path))
        with patch:
            self.assertIs(export_orphan_record(self.sid("w1:pD"), {"agent": "Herdr"}), False)
        holder.release()
        self.assertIn(pending_dir_for(self.path).stat().st_ino, synced)


if __name__ == "__main__":
    unittest.main()
