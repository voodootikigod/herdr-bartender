"""Orphan journal entries that cannot be applied right now (gate finding, adversarial review round 5).

``_commit_locked`` unlinked every journal entry it listed, including one it could not read (``OSError``: EIO,
EACCES) - the only durable copy of an owed Ended export - as soon as any other orphan operation committed. Now an
unreadable entry stays queued for the next lock holder (the flush reports the journal as not drained, so the
reconciler retries on a later run instead of spinning), and an entry that can never be applied (undecodable or
malformed) is moved to ``<journal>/bad/`` instead of being deleted.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import orphans
from herdr_bartender.orphans import export_orphan_record, flush_pending_orphan_ops, journaled_export_sids
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase

SID_A, SID_B = "herdr:h:w1:pA", "herdr:h:w1:pB"


def journal_entry(pending: Path, name: str, sid: str) -> Path:
    path = pending / name
    path.write_text(json.dumps({"op": "export", "sid": sid, "session": {"pane_id": sid.split(":", 2)[2]}}))
    return path


class JournalRetentionTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.path = get_orphan_path()
        self.pending = orphans.pending_dir_for(self.path)
        self.pending.mkdir(mode=0o700)

    def file_sids(self):
        return set(json.loads(self.path.read_text())["sessions"]) if self.path.exists() else set()

    def unreadable(self, *names):
        real = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name in names:
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        return mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text)

    def test_unreadable_entry_survives_another_commit_and_is_applied_later(self):
        stuck = journal_entry(self.pending, "00000000000000000001-1-000000-aa.json", SID_A)
        journal_entry(self.pending, "00000000000000000002-1-000001-bb.json", SID_B)
        with self.unreadable(stuck.name):
            self.assertIs(export_orphan_record("herdr:h:w1:pC", {"pane_id": "w1:pC"}, blocking=True), True)
            self.assertFalse(flush_pending_orphan_ops(), "the journal is not drained while an entry is unreadable")
        self.assertTrue(stuck.exists(), "the only copy of the owed export is kept for a retry")
        self.assertEqual(self.file_sids(), {SID_B, "herdr:h:w1:pC"})
        self.assertIs(flush_pending_orphan_ops(), True)
        self.assertEqual(self.file_sids(), {SID_A, SID_B, "herdr:h:w1:pC"})
        self.assertEqual(sorted(self.pending.glob("*.json")), [])

    def test_undecodable_or_malformed_entry_is_quarantined_not_deleted(self):
        garbled = self.pending / "00000000000000000001-1-000000-aa.json"
        garbled.write_text("{not json")
        malformed = self.pending / "00000000000000000002-1-000001-bb.json"
        malformed.write_text(json.dumps({"op": "export", "sid": 7}))
        journal_entry(self.pending, "00000000000000000003-1-000002-cc.json", SID_B)
        self.assertIs(flush_pending_orphan_ops(), True)
        self.assertEqual(self.file_sids(), {SID_B})
        self.assertEqual(sorted(self.pending.glob("*.json")), [])
        bad = self.pending / "bad"
        self.assertEqual(sorted(p.name for p in bad.iterdir()), [garbled.name, malformed.name])
        self.assertEqual((bad / garbled.name).read_text(), "{not json", "the bytes are kept for the operator")

    def test_lock_free_reader_never_moves_journal_entries(self):
        garbled = self.pending / "00000000000000000001-1-000000-aa.json"
        garbled.write_text("{not json")
        journal_entry(self.pending, "00000000000000000002-1-000001-bb.json", SID_B)
        self.assertEqual(journaled_export_sids(), frozenset({SID_B}))
        self.assertTrue(garbled.exists(), "only a lock holder quarantines")


if __name__ == "__main__":
    unittest.main()
