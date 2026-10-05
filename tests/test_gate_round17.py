"""npx adversarial-review gate round 17: checked size == written size, and bounded state-file reads (R65)."""

import json
import os
import unittest
from unittest import mock

from herdr_bartender import boundedio, jsonsafe, orphans
from herdr_bartender.envelopes import MAX_ENVELOPE_BYTES, write_json_capped
from herdr_bartender.orphans import OrphanFileError, flush_pending_orphan_ops, pending_dir_for, read_orphan_records
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.nonblocking import call_without_blocking


class CheckedSizeIsWrittenSizeTests(SandboxTestCase):
    def test_persisted_envelope_never_exceeds_the_bound(self):
        """R65: a payload whose compact form fits but whose spaced form would not is written compact (in bounds)."""
        directory = self.tmp / "capped"
        directory.mkdir()
        obj = {f"k{i}": i for i in range(16750)}
        compact = len(json.dumps(obj, separators=(",", ":")))
        spaced = len(json.dumps(obj))
        self.assertTrue(compact <= MAX_ENVELOPE_BYTES < spaced, (compact, spaced))
        path = directory / "00000000000000000001_1_1.json"
        write_json_capped(directory, path, obj, cap=10, lock_timeout=0.2)
        self.assertLessEqual(path.stat().st_size, MAX_ENVELOPE_BYTES)
        self.assertEqual(json.loads(path.read_text()), obj)


class BoundedStateReadTests(SandboxTestCase):
    def test_oversized_orphan_file_is_refused_without_parsing(self):
        """R65: the orphan file is never decoded whole past ORPHAN_FILE_MAX_BYTES."""
        path = get_orphan_path()
        path.write_bytes(b'{"sessions": {"' + b"x" * (boundedio.ORPHAN_FILE_MAX_BYTES + 1) + b'": {}}}')
        with mock.patch.object(jsonsafe, "loads", wraps=jsonsafe.loads) as loads:
            with self.assertRaises(OrphanFileError):
                read_orphan_records(path)
        loads.assert_not_called()

    def test_oversized_cache_is_quarantined_and_salvaged_from_its_prefix(self):
        """R65: an active-sessions.json over CACHE_MAX_BYTES is quarantined, not parsed whole."""
        sid = self.sid("w1:pBig")
        head = json.dumps({"version": 4, "sessions": {sid: {"pane_id": "w1:pBig"}}})[:-2]
        self.cache_mgr.cache_file.write_bytes(head.encode() + b', "pad": "' +
                                              b"x" * (boundedio.CACHE_MAX_BYTES + 1) + b'"}}')
        with self.cache_mgr as data:
            self.assertIn(sid, data["sessions"], "salvaged from the bounded prefix")
            self.assertTrue(data["sessions"][sid].get("salvaged"))
        self.assertTrue(list(self.state_dir.glob("active-sessions.json.corrupt.*")))

    def test_fifo_journal_entry_is_quarantined_not_kept_forever(self):
        """R65: an unusable (non-regular) journal entry is poison, so the journal can still drain."""
        pending = pending_dir_for(get_orphan_path())
        pending.mkdir(parents=True, exist_ok=True)
        fifo = pending / "00000000000000000001-1-000000-aa.json"
        os.mkfifo(fifo)
        self.assertIs(call_without_blocking(self, fifo, lambda: flush_pending_orphan_ops(blocking=True)), True)
        self.assertFalse(fifo.exists())
        self.assertEqual([p.name for p in (pending / "bad").iterdir()], [fifo.name])


if __name__ == "__main__":
    unittest.main()
