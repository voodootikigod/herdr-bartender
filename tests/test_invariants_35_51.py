"""Plan §10.1 invariants #35-#51: tests that close the gaps left by the coverage audit.

Weak in the audit:

* #40: the existing test only reached the in-window ``closed_source_ts`` rule, so removing the
  dedicated tombstone ``last_source_timestamp`` check went unnoticed. These tests deliver the
  trailing event after the 60s positive-admission window, where nothing else rejects it. They
  also cover the equality boundary (ts == last_source_timestamp) inside and outside the window.
* #41: an unlink of ``.vendor_active`` followed by the guard's own bare re-touch was invisible.
  These tests seed a UUID record and assert it is byte-identical afterwards.
* #51: the "rejected with warning" clause was never asserted, and production logged the refusal
  as a plain debug line. That has been fixed in ``staging.stage_status``. These tests read the
  sandboxed log, both in-process and through the real launcher.
"""

import json
import os
import time
import unittest

from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.log import LOG_FILE_NAME
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.staging import TOMBSTONE_WINDOW_NS
from tests.support import SandboxTestCase, run_guard, write_guard_script
from tests.support.guard_harness import failing_mktemp_env, leftovers

STATUS = "pane.agent_status_changed"
# A fixed wall clock so arrival stamps and source timestamps are exact (no float drift from time.time()).
FAKE_START = 1_800_000_000.0
FAKE_START_NS = int(FAKE_START * 1e9)
PAST_WINDOW_NS = TOMBSTONE_WINDOW_NS + 1_000_000_000


class _TombstoneCase(SandboxTestCase):
    """Shared helpers: a frozen wall clock, status/close handlers and cache snapshots (no tests of its own)."""

    def setUp(self):
        super().setUp()
        # Frozen: the save-time tombstone prune (60s TTL vs clock.time_ns()) never removes the
        # tombstone, so only the Step A gates decide what happens to the late arrival.
        self.clock = self.use_fake_clock(start=FAKE_START)

    def _status(self, pane, arrival_ns, timestamp, status="working", agent="claude"):
        handle_agent_status_changed(
            {"agent_status": status, "pane_id": pane, "workspace_id": pane.split(":")[0], "agent": agent,
             "timestamp": timestamp},
            {}, bridge_url=self.mock_url, arrival_ns=arrival_ns,
        )

    def _close(self, pane, arrival_ns):
        handle_pane_closed({"pane_id": pane, "workspace_id": pane.split(":")[0]}, {},
                           bridge_url=self.mock_url, arrival_ns=arrival_ns)

    def _snapshot(self, pane):
        with self.cache_mgr as data:
            return (dict(data.get("tombstones", {}).get(pane) or {}),
                    dict(data.get("sessions", {}).get(self.sid(pane)) or {}))

    def _assert_not_resurrected(self, pane, tomb_before, msg):
        tomb, session = self._snapshot(pane)
        self.assertEqual(tomb, tomb_before, f"{msg}: the tombstone must stay, unchanged")
        self.assertIn(session.get("desired_state"), (None, "Ended"), f"{msg}: the closed pane must not be re-admitted")
        live = [e["state"] for e in self.bridge.events_for(self.sid(pane)) if e["state"] != "Ended"]
        self.assertEqual(live, ["Working"], f"{msg}: nothing but the pre-close Working may reach the bridge")

    def _close_with_source_ts(self, pane, source_ts):
        self._status(pane, FAKE_START_NS, source_ts)
        close_ns = FAKE_START_NS + 1_000_000
        self._close(pane, close_ns)
        tomb, _ = self._snapshot(pane)
        self.assertEqual(tomb.get("last_source_timestamp"), source_ts)
        self.assertEqual(tomb.get("closed_at_ns"), close_ns)
        return close_ns, tomb


class TombstoneSourceTimestampTests(_TombstoneCase):
    """#40: the tombstone's last_source_timestamp rejects trailing events on its own."""

    def test_p40_last_source_timestamp_rejects_after_admission_window(self):
        """Plan §10.1 #40: after the 60s window, a positive working+agent event with ts <= the tombstone's
        last_source_timestamp is still rejected, and the tombstone survives. Equality is covered too."""
        pane = "w1:pTombLate"
        source_ts = 5000.0
        close_ns, tomb = self._close_with_source_ts(pane, source_ts)
        late_ns = close_ns + PAST_WINDOW_NS
        # 4999.95 is within the 0.1s source-staleness tolerance: only the tombstone rule can drop it.
        for offset, ts in enumerate((source_ts, source_ts - 0.05, source_ts - 1000.0)):
            with self.subTest(timestamp=ts):
                self._status(pane, late_ns + offset, ts)
                self._assert_not_resurrected(pane, tomb, f"ts {ts} <= last_source_timestamp {source_ts}")

        # Control: the same positive event with a newer source ts is admitted and pops the tombstone,
        # so the rejections above came from the tombstone's source bound and nothing else.
        self._status(pane, late_ns + 10, source_ts + 0.5)
        tomb_after, session = self._snapshot(pane)
        self.assertEqual(tomb_after, {}, "a newer post-window event pops the tombstone")
        self.assertEqual(session.get("desired_state"), "Working")

    def test_p40_equality_rejected_inside_admission_window(self):
        """Plan §10.1 #40: inside the 60s window, ts == the tombstone's last_source_timestamp is rejected.

        The pre-close source ts is ahead of the close's arrival time (sender clock skew), so
        closed_source_ts == last_source_timestamp. The boundary is the equality itself.
        """
        pane = "w1:pTombEq"
        source_ts = FAKE_START + 0.5
        close_ns, tomb = self._close_with_source_ts(pane, source_ts)
        self.assertEqual(tomb.get("closed_source_ts"), source_ts)
        for offset, ts in enumerate((source_ts, source_ts - 0.05)):
            with self.subTest(timestamp=ts):
                self._status(pane, close_ns + 2_000 + offset, ts)
                self._assert_not_resurrected(pane, tomb, f"in-window ts {ts} <= {source_ts}")

        self._status(pane, close_ns + 3_000, source_ts + 0.001)
        tomb_after, session = self._snapshot(pane)
        self.assertEqual((tomb_after, session.get("desired_state")), ({}, "Working"),
                         "a strictly newer positive event inside the window is admitted")


class TombstoneWindowLiteralTests(_TombstoneCase):
    """Plan §10.1 #27 (§4.3 L283 / L96): the positive-admission window is 60s, pinned with literals.

    Round-2 finding (tests): every window test derived its offsets from the imported constant, so halving
    TOMBSTONE_WINDOW_NS to 30s survived the suite. These offsets are literals: 59s is inside, 61s outside.
    """

    def test_window_constant_is_60_seconds(self):
        self.assertEqual(TOMBSTONE_WINDOW_NS, 60_000_000_000)

    def _closed_pane(self, pane):
        self._status(pane, FAKE_START_NS, None)
        close_ns = FAKE_START_NS + 1_000_000
        self._close(pane, close_ns)
        tomb, _ = self._snapshot(pane)
        self.assertEqual(tomb.get("closed_at_ns"), close_ns)
        return close_ns, tomb

    def test_non_working_event_rejected_at_59s(self):
        """A late idle status 59s after the close may not re-admit the pane (non-working inside the window)."""
        for status in ("idle", "done"):
            with self.subTest(status=status):
                pane = f"w1:pTombWin59{status}"
                close_ns, tomb = self._closed_pane(pane)
                self._status(pane, close_ns + 59_000_000_000, None, status=status)
                self._assert_not_resurrected(pane, tomb, f"{status} at closed_at + 59s")

    def test_non_working_event_evaluated_outside_the_window_at_61s(self):
        """Control: the same event 61s after the close skips the positive-admission gates and pops the tombstone."""
        for status in ("idle", "done"):
            with self.subTest(status=status):
                pane = f"w1:pTombWin61{status}"
                close_ns, _ = self._closed_pane(pane)
                self._status(pane, close_ns + 61_000_000_000, None, status=status)
                tomb_after, _ = self._snapshot(pane)
                self.assertEqual(tomb_after, {}, f"{status} at closed_at + 61s is past the window: tombstone popped")


class MktempFailOpenTests(SandboxTestCase):
    """#41: under mktemp failure the guard fails open and leaves .vendor_active untouched."""

    start_bridge = False

    def setUp(self):
        super().setUp()
        self.panes = self.state_dir / "panes"
        self.panes.mkdir(parents=True, exist_ok=True)
        self.shim_bin = self.tmp / "guard-bin"
        self.script = write_guard_script(self.tmp / "guard-failopen-uuid.sh",
                                         'printf "ARGV:%s|" "${1:-}"; printf "STDIN:"; cat')

    def _seed(self, pane, record):
        hex_id = get_hex_pane_id(pane)
        (self.panes / hex_id).write_text(str(int(time.time())))   # fresh marker: Herdr is healthy
        vendor_active = self.panes / f"{hex_id}.vendor_active"
        vendor_active.write_bytes(record)
        old = time.time() - 30
        os.utime(vendor_active, (old, old))
        return vendor_active

    def test_p41_mktemp_failure_preserves_uuid_vendor_active(self):
        """Plan §10.1 #41: a mktemp failure fails open (vendor runs, stdin byte-exact, no error) and never
        unlinks .vendor_active. A UUID record survives byte-identical in the same inode, so an unlink
        followed by a bare re-touch is detected. Only the refresh touch may change it."""
        record = b'{"vendor_session_id":"vendorUUID-0123456789abcdef"}'
        cases = (
            ("argv-token", ("Working",), 'plain stdin\nline2 "quoted" \\ tail\n'),
            ("stdin-json", (), json.dumps({"hook_event_name": "PreToolUse", "tool": "Bash"}) + "\n"),
        )
        for label, argv, payload in cases:
            with self.subTest(mode=label):
                pane = f"w1:pFail{label.replace('-', '')}"
                vendor_active = self._seed(pane, record)
                before = os.stat(vendor_active)
                res = run_guard(self.script, *argv, input=payload,
                                env_extra={"HERDR_PANE_ID": pane, **failing_mktemp_env(self.shim_bin)})
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stderr, "", "fail-open must not raise or print an error")
                self.assertEqual(res.stdout, f"ARGV:{(argv or ('',))[0]}|STDIN:{payload}",
                                 "healthy Herdr + UUID record must still pass through, stdin untruncated")
                self.assertTrue(vendor_active.exists(), "fail-open must not unlink .vendor_active")
                self.assertEqual(vendor_active.read_bytes(), record, "the UUID record must be byte-identical")
                after = os.stat(vendor_active)
                self.assertEqual(after.st_ino, before.st_ino, ".vendor_active must not be replaced")
                self.assertGreater(after.st_mtime, before.st_mtime, "the fall-through refresh touch ran")
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*", ".va.tmp.*"), [])


class CapacityWarningTests(SandboxTestCase):
    """#51: session 257 is refused with a WARNING log naming the cap and the refused session."""

    def _seed_live_sessions(self, count=256):
        now = time.time()
        with self.cache_mgr as data:
            data["sessions"] = {
                self.sid(f"wCap:p{i}"): {"desired_state": "Working", "seq": 1, "delivered_seq": 1,
                                         "delivered_state": "Working", "pane_id": f"wCap:p{i}",
                                         "agent": "Claude (Herdr)", "last_event_at": now}
                for i in range(count)
            }
            self.cache_mgr.save(data)

    def _warnings(self):
        log_file = self.state_dir / LOG_FILE_NAME
        lines = log_file.read_text(encoding="utf-8").splitlines() if log_file.exists() else []
        return [line for line in lines if "WARNING:" in line]

    def _assert_refused_with_warning(self, refused_sid):
        with self.cache_mgr as data:
            self.assertNotIn(refused_sid, data["sessions"])
            self.assertEqual(len(data["sessions"]), 256)
            self.assertTrue(all(s["desired_state"] == "Working" for s in data["sessions"].values()),
                            "live sessions are never evicted to make room")
        self.assertEqual(self.bridge.events_for(refused_sid), [])
        hits = [w for w in self._warnings() if "256" in w and refused_sid in w]
        self.assertEqual(len(hits), 1, f"want one capacity WARNING naming {refused_sid}, got {self._warnings()}")

    def test_p51_session_257_refused_with_warning_log(self):
        """Plan §10.1 #51: with 256 live sessions, session 257 is refused and a WARNING line names the
        256-session cap and the refused session id (Plan §4.1 L109 "refused with a warning log")."""
        self._seed_live_sessions()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wCap:p257", "workspace_id": "wCap", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        self._assert_refused_with_warning(self.sid("wCap:p257"))

    def test_p51_launcher_refusal_logs_warning(self):
        """Plan §10.1 #51: the same refusal through the real bin/herdr-bartender process exits 0 and logs
        the capacity WARNING."""
        self._seed_live_sessions()
        envelope = {"event": STATUS, "context": {},
                    "data": {"agent_status": "working", "pane_id": "wCap:p300", "workspace_id": "wCap",
                             "agent": "claude"}}
        proc = self.run_cli(STATUS, input=json.dumps(envelope))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_refused_with_warning(self.sid("wCap:p300"))

    def test_p51_no_capacity_warning_below_cap(self):
        """Plan §10.1 #51 (control): below the cap the same event is admitted and no capacity WARNING is logged."""
        self._seed_live_sessions(255)
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wCap:p257", "workspace_id": "wCap", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertIn(self.sid("wCap:p257"), data["sessions"])
        self.assertEqual([w for w in self._warnings() if "capacity" in w], [])


if __name__ == "__main__":
    unittest.main()
