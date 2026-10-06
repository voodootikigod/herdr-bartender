"""Round-4 low findings: validation regexes and the orphan replay payload.

* Python's ``$`` also matches just before a trailing newline, so ``re.match(r'^...$', "valid\\n")`` accepted a
  session id, pane id, vendor UUID (...) carrying a trailing newline. Every validation regex now ends in ``\\Z``.
* Orphan replay POSTed the orphan file's ``agent`` verbatim (control and bidi characters, any length); it now
  gets the intake sanitizer (``sanitize_agent``: controls and invisible format characters stripped, <= 64).
"""

import json
import unittest
from pathlib import Path

from herdr_bartender import config, hooks_fs, intake, salvage
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.replay import run_replay_orphans
from herdr_bartender.vendor import VendorFile, parse_vendor_uuid
from tests.support import SandboxTestCase

VALID = {
    "config._PORT_REGEX": (config._PORT_REGEX, "7777"),
    "config.PANE_ID_REGEX": (config.PANE_ID_REGEX, "w1:p1"),
    "config.CONTAINER_ID_REGEX": (config.CONTAINER_ID_REGEX, "w1:t1"),
    "config.SESSION_ID_REGEX": (config.SESSION_ID_REGEX, "herdr:host:w1:p1"),
    "config.VENDOR_UUID_REGEX": (config.VENDOR_UUID_REGEX, "0123456789abcdef"),
    "intake.WIRE_SESSION_ID_RE": (intake.WIRE_SESSION_ID_RE, "herdr:host:w1:p1"),
    "intake.HOST_RE": (intake.HOST_RE, "host"),
    "intake.EVENT_NAME_VALID_RE": (intake.EVENT_NAME_VALID_RE, "pane.closed"),
    "hooks_fs._SHA_RE": (hooks_fs._SHA_RE, "0" * 64),
    "salvage.MARKER_NAME_RE": (salvage.MARKER_NAME_RE, "77313a7031"),
}


class TrailingNewlineTests(SandboxTestCase):
    start_bridge = False   # parse_vendor_uuid logs the malformed id: keep plugin.log sandboxed

    def test_every_validation_regex_rejects_a_trailing_newline(self):
        for name, (regex, valid) in VALID.items():
            with self.subTest(regex=name):
                self.assertTrue(regex.match(valid), "control: the plain value is valid")
                self.assertIsNone(regex.match(valid + "\n"), "a trailing newline is not part of a valid value")

    def test_vendor_record_with_a_trailing_newline_in_the_uuid_is_a_bare_touch(self):
        record = VendorFile(Path("x.vendor_active"), json.dumps({"vendor_session_id": "0123456789abcdef\n"}).encode())
        self.assertIsNone(parse_vendor_uuid(record))
        good = VendorFile(Path("x.vendor_active"), json.dumps({"vendor_session_id": "0123456789abcdef"}).encode())
        self.assertEqual(parse_vendor_uuid(good), "0123456789abcdef")

    def test_event_name_with_a_trailing_newline_is_not_dispatched(self):
        self.assertEqual(intake.valid_event_name("pane.closed\n"), "")


class OrphanReplayPayloadTests(SandboxTestCase):
    def _replay(self, records):
        path = get_orphan_path()
        path.write_text(json.dumps({"version": 1, "sessions": records}))
        return run_replay_orphans(str(path), bridge_url=self.mock_url, quiet=True)

    def test_session_id_with_a_trailing_newline_is_never_posted(self):
        sid = self.sid("w1:pNl")
        self._replay({sid + "\n": {"agent": "Claude (Herdr)", "pane_id": "w1:pNl"}})
        self.assertEqual(self.bridge.posts(), [], "an invalid session id is skipped, never sent")

    def test_agent_is_sanitized_and_bounded(self):
        sid = self.sid("w1:pAgent")
        hostile = "\x1b]0;pwned\x07Cla‮ude\x00 (Herdr)" + "x" * 500
        self._replay({sid: {"agent": hostile, "pane_id": "w1:pAgent"}})
        (posted,) = [p for p in self.bridge.posts() if p.get("session_id") == sid]
        agent = posted["agent"]
        self.assertLessEqual(len(agent), 64)
        self.assertTrue(agent.startswith("Claude (Herdr)"), agent)
        self.assertFalse(any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or c == "‮" for c in agent), repr(agent))

    def test_blank_or_control_only_agent_falls_back_to_herdr(self):
        sid = self.sid("w1:pBlank")
        self._replay({sid: {"agent": "\x1b[31m ​", "pane_id": "w1:pBlank"}})
        (posted,) = [p for p in self.bridge.posts() if p.get("session_id") == sid]
        self.assertEqual(posted["agent"], "Herdr")


if __name__ == "__main__":
    unittest.main()
