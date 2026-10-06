"""Round-4 low findings: plan limits pinned with literals (a test computed from the constant cannot catch a
changed constant), and R7's "stricter bounds" rule for an existing tombstone.

* Spool cap: Plan §4.3 L391 "capped at 100 envelopes".
* agent_exits TTL: Plan §4.3 L102/L424 "entries are pruned after 60s"; the existing test pins only "at exactly
  60s the entry still applies", so a longer TTL went unnoticed.
* R7 (Plan L91/L398/L577): re-recording a tombstone keeps the EARLIER origin and the STRICTER (larger)
  ``closed_source_ts`` and ``last_source_timestamp``.
"""

import json
import time
import unittest

from herdr_bartender.cache_schema import new_cache
from herdr_bartender.delivery_state import record_tombstone
from herdr_bartender.intake import resolve_identity
from herdr_bartender.spool import enqueue_spool
from herdr_bartender.staging import stage_status
from tests.support import SandboxTestCase

PANE = "w1:pPinned"
SIXTY_SECONDS_NS = 60_000_000_000


class SpoolCapTests(SandboxTestCase):
    start_bridge = False

    def test_spool_holds_at_most_100_envelopes(self):
        spool = self.state_dir / "spool"
        spool.mkdir(parents=True)
        t0 = time.time_ns()
        for i in range(120):
            envelope = {"event_name": "pane.agent_status_changed", "context": {}, "arrival_ns": t0 + i,
                        "enqueued_ns": t0 + i, "event_data": {"pane_id": f"w1:p{i}", "agent_status": "working"}}
            (spool / f"{i:020d}_1_1.json").write_text(json.dumps(envelope))
        enqueue_spool("pane.agent_status_changed", {"pane_id": "w1:pNew", "agent_status": "working"}, {},
                      arrival_ns=t0 + 1_000)
        names = sorted(p.name for p in spool.glob("*.json"))
        self.assertEqual(len(names), 100)
        self.assertNotIn(f"{20:020d}_1_1.json", names, "the 21 oldest status envelopes were pruned")
        self.assertIn(f"{21:020d}_1_1.json", names)


class AgentExitWindowTests(SandboxTestCase):
    start_bridge = False   # stage_status logs the stale-event rejection: keep plugin.log sandboxed

    def _stage(self, exit_age_ns: int):
        arr = time.time_ns()
        data = new_cache("testhost")
        data["agent_exits"][PANE] = {"exit_at_ns": arr - exit_age_ns, "exit_source_ts": 500.0}
        event = {"agent_status": "working", "pane_id": PANE, "workspace_id": "w1", "agent": "claude",
                 "timestamp": 400.0}   # a stale event: source ts <= exit_source_ts
        identity, _ = resolve_identity(event, {}, env={})
        return data, stage_status(data, identity, event, {}, arr, herdr_alive=lambda: True)

    def test_stale_event_is_dropped_at_exactly_60s(self):
        data, stage = self._stage(SIXTY_SECONDS_NS)
        self.assertFalse(stage.staged)
        self.assertIn(PANE, data["agent_exits"])

    def test_entry_expires_one_nanosecond_past_60s(self):
        data, stage = self._stage(SIXTY_SECONDS_NS + 1)
        self.assertTrue(stage.staged, "the 60s agent_exits window is over: the entry no longer applies")
        self.assertNotIn(PANE, data["agent_exits"], "and it is popped")


class TombstoneRerecordTests(unittest.TestCase):
    def test_existing_tombstone_keeps_earlier_origin_and_stricter_source_bounds(self):
        data = {"tombstones": {PANE: {"closed_at_ns": 100, "closed_source_ts": 50.0, "last_source_timestamp": 40.0}}}
        entry = record_tombstone(data, PANE, 200, 30.0, 45.0)
        self.assertEqual(entry, {"closed_at_ns": 100, "closed_source_ts": 50.0, "last_source_timestamp": 45.0})
        entry = record_tombstone(data, PANE, 50, 60.0, 10.0)
        self.assertEqual(entry, {"closed_at_ns": 50, "closed_source_ts": 60.0, "last_source_timestamp": 45.0})
        self.assertEqual(data["tombstones"][PANE], entry)

    def test_first_record_is_taken_as_given(self):
        data = {}
        self.assertEqual(record_tombstone(data, PANE, 7, 1.5, 0.5),
                         {"closed_at_ns": 7, "closed_source_ts": 1.5, "last_source_timestamp": 0.5})


if __name__ == "__main__":
    unittest.main()
