"""Deeply nested JSON at every trust boundary (round-4 finding: uncaught RecursionError).

``json.loads`` raises ``RecursionError`` (not a ``ValueError``) on deep nesting: about 1,000 levels on macOS's
Python 3.9, ~100k on this host's 3.14. ``DEEP`` overflows every supported interpreter, so each test fails
without the fix wherever it runs; ``MACOS_DEEP`` (2,000 levels, the size that crashes 3.9) pins the explicit
``jsonsafe.MAX_DEPTH`` bound that makes every interpreter give the same verdict.
"""

import json
import os
import time
import unittest

from herdr_bartender import jsonsafe
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.intake import parse_invocation
from herdr_bartender.orphans import OrphanFileError, journaled_export_sids, read_orphan_records
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.replay import auto_replay_due
from herdr_bartender.results import drain_results_dir
from herdr_bartender.spool import replay_spool_dir
from tests.support import SandboxTestCase

DEEP = 300_000       # ~600 KB: under STDIN_MAX_BYTES, past every interpreter's recursion / C-stack limit
MACOS_DEEP = 2_000   # ~4 KB: RecursionError on Python 3.9 (limit 1000); parses on 3.12+ without the bound


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def deep_orphan_file(depth: int) -> str:
    return '{"sessions": {"herdr:h:w1:pDeep": ' + nested(depth) + "}}"


class LoadsTests(unittest.TestCase):
    def test_interpreter_overflow_is_a_value_error(self):
        with self.assertRaises(ValueError):
            jsonsafe.loads(nested(DEEP))

    def test_depth_beyond_the_bound_is_a_value_error_on_every_interpreter(self):
        with self.assertRaises(ValueError):
            jsonsafe.loads(nested(MACOS_DEEP))
        with self.assertRaises(ValueError):
            jsonsafe.loads(nested(jsonsafe.MAX_DEPTH + 1))
        self.assertEqual(jsonsafe.MAX_DEPTH, 128)

    def test_the_bound_itself_and_ordinary_documents_decode(self):
        self.assertIsInstance(jsonsafe.loads(nested(jsonsafe.MAX_DEPTH)), list)
        self.assertEqual(jsonsafe.loads(b'{"a": [1, {"b": "c"}]}'), {"a": [1, {"b": "c"}]})
        with self.assertRaises(ValueError):
            jsonsafe.loads("{garbage")


class IntakeTests(SandboxTestCase):
    start_bridge = False

    def test_deep_stdin_envelope_is_unusable_not_a_crash(self):
        """R20: malformed stdin is a no-op; a deep envelope is malformed input like any other."""
        for depth in (DEEP, MACOS_DEEP):
            with self.subTest(depth=depth):
                envelope = '{"event": "pane.closed", "data": ' + nested(depth) + "}"
                name, data, _ = parse_invocation(["pane.closed"], envelope.encode(), {})
                self.assertEqual((name, data), ("pane.closed", None))

    def test_deep_legacy_env_json_is_unusable_not_a_crash(self):
        env = {"HERDR_PLUGIN_EVENT_JSON": '{"data": ' + nested(DEEP) + "}",
               "HERDR_PLUGIN_CONTEXT_JSON": nested(DEEP)}
        self.assertEqual(parse_invocation(["pane.closed"], b"", env), ("pane.closed", None, {}))

    def test_cli_with_a_deep_stdin_envelope_exits_0_without_a_traceback(self):
        """The real launcher: a ~600 KB nested envelope used to print a RecursionError traceback and exit 1."""
        envelope = '{"event": "pane.agent_status_changed", "data": ' + nested(DEEP) + "}"
        res = self.run_cli("pane.agent_status_changed", input=envelope.encode(), timeout=60.0)
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        self.assertNotIn(b"Traceback", res.stderr)
        self.assertFalse((self.state_dir / "active-sessions.json").exists(), "no cache mutation")


class OrphanFileTests(SandboxTestCase):
    def test_deep_orphan_file_is_an_orphan_file_error_and_not_replay_work(self):
        path = get_orphan_path()
        for depth in (DEEP, MACOS_DEEP):
            with self.subTest(depth=depth):
                text = deep_orphan_file(depth)
                path.write_text(text)
                with self.assertRaises(OrphanFileError):
                    read_orphan_records(path)
                self.assertFalse(auto_replay_due(path, time.time()), "an unreadable file is not replay work")
                self.assertEqual(path.read_text(), text, "left untouched for the operator")

    def test_deep_orphan_journal_entry_is_dropped(self):
        path = get_orphan_path()
        pending = path.parent / (path.name + ".pending")
        pending.mkdir(mode=0o700, exist_ok=True)
        (pending / "00000000000000000001_1.json").write_text(nested(DEEP))
        (pending / "00000000000000000002_1.json").write_text(json.dumps(
            {"op": "export", "sid": "herdr:h:w1:pOk", "session": {"pane_id": "w1:pOk"}}))
        self.assertEqual(journaled_export_sids(path), frozenset({"herdr:h:w1:pOk"}),
                         "the deep entry is dropped; the valid one is still read")

    def test_reconciler_pass_survives_a_deep_orphan_file(self):
        """Every reconciler pass used to crash at auto_replay_due ('Reconciler crashed'), so retries, TTL expiry
        and dismissals never ran. The pass now completes and still delivers owed work."""
        path = get_orphan_path()
        text = deep_orphan_file(DEEP)
        path.write_text(text)
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pDeepLive", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            data["sessions"][self.sid("w1:pDeepLive")].update({"desired_state": "Ended", "seq": 2,
                                                               "delivery_status": "in_flight"})
            self.cache_mgr.save(data)
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.sid("w1:pDeepLive"))], ["Working", "Ended"])
        self.assertEqual(path.read_text(), text, "the unreadable orphan file is left untouched")


class StateFileTests(SandboxTestCase):
    start_bridge = False

    def _write(self, directory: str, name: str, text: str):
        target = self.state_dir / directory
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        (target / name).write_text(text)
        return target

    def test_deep_spool_envelope_is_quarantined(self):
        spool = self._write("spool", "00000000000000000001_1_1.json", nested(DEEP))
        batch = replay_spool_dir(self.state_dir)
        self.assertEqual(len(batch.quarantined), 1)
        self.assertTrue((spool / "bad" / "00000000000000000001_1_1.json").exists())

    def test_deep_result_envelope_is_quarantined(self):
        results = self._write("results", "00000000000000000001_1_1.json", nested(DEEP))
        report = drain_results_dir(self.state_dir)
        self.assertEqual(report.quarantined, 1)
        self.assertTrue((results / "bad" / "00000000000000000001_1_1.json").exists())

    def test_deep_cache_is_salvaged_not_a_crash(self):
        cache_file = self.state_dir / "active-sessions.json"
        os.makedirs(self.state_dir, exist_ok=True)
        cache_file.write_text('{"sessions": {"x": ' + nested(DEEP) + "}}")
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"], {}, "salvaged: nothing recoverable from the nested text")
        self.assertTrue(list(self.state_dir.glob("active-sessions.json.corrupt.*")),
                        "the corrupt cache is quarantined for the operator")


if __name__ == "__main__":
    unittest.main()
