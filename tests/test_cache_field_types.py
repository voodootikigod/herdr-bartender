"""Wrong-typed fields inside a valid-JSON cache (gate finding, adversarial review round 5).

``normalize_cache`` checked only the root shape and that each session is an object, so a hand-edited or legacy
cache with ``"seq": "corrupt"`` loaded fine and then raised ``ValueError`` from ``int()`` in Step A (the event was
neither applied nor spooled) and in every reconciler pass; a bad tombstone time broke every save. Known fields of a
wrong type are now dropped at load (numeric strings converted), so the defaults apply and nothing crashes.
"""

import json
import time
import unittest
from unittest import mock

from herdr_bartender.cache import safe_to_evict, sessions_to_prune
from herdr_bartender.cache_fields import sane_cache_fields
from herdr_bartender.cache_schema import normalize_cache
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.salvage import CORRUPT_PREFIX
from tests.support import SandboxTestCase

PANE = "w1:pTyped"


def corrupt_cache(sid: str, now: float, host: str = "dev-one") -> dict:
    return {
        "version": 4, "host": host, "next_generation": 3, "consecutive_failures": "lots",
        "sessions": {sid: {
            "pane_id": PANE, "workspace_id": "w1", "agent": "Claude (Herdr)", "desired_state": "Working",
            "seq": "corrupt", "delivered_seq": "2", "generation": [1], "delivery_attempts": {"n": 1},
            "last_event_at": "yesterday", "lease_deadline": "soon", "last_source_timestamp": "NaN",
            "title": 42, "desired_payload": "not an object", "custom_field": ["kept"],
        }},
        "tombstones": {"w1:pOld": {"closed_at_ns": "x", "closed_source_ts": 1.0},
                       "w1:pGood": {"closed_at_ns": time.time_ns(), "closed_source_ts": now}},
        "agent_exits": {"w1:pExit": "not an object"},
    }


class SaneFieldsTests(SandboxTestCase):
    start_bridge = False   # a dropped field is logged to plugin.log: keep it sandboxed

    def test_wrong_typed_known_fields_are_dropped_and_numeric_strings_converted(self):
        now = time.time()
        sid = "herdr:dev-one:" + PANE
        data = normalize_cache(corrupt_cache(sid, now), lambda: "dev-one")
        record = data["sessions"][sid]
        for field in ("seq", "generation", "delivery_attempts", "last_event_at", "lease_deadline",
                      "last_source_timestamp", "title", "desired_payload"):
            self.assertNotIn(field, record, field)
        self.assertEqual(record["delivered_seq"], 2, "a numeric string is converted")
        self.assertEqual(record["custom_field"], ["kept"], "unknown fields are left alone")
        self.assertEqual(record["pane_id"], PANE)
        self.assertEqual(data["consecutive_failures"], 0)
        self.assertEqual(sorted(data["tombstones"]), ["w1:pGood"])
        self.assertEqual(data["agent_exits"], {})

    def test_valid_records_are_unchanged(self):
        record = {"pane_id": PANE, "seq": 3, "delivered_seq": 3, "generation": 2, "last_event_at": 1.5,
                  "lease_deadline": None, "desired_payload": {"state": "Working"}, "title": "T", "salvaged": False}
        data = {"sessions": {"s": dict(record)}, "tombstones": {}, "agent_exits": {}}
        sane_cache_fields(data)
        self.assertEqual(data["sessions"]["s"], record)


class EndToEndTests(SandboxTestCase):
    def test_event_and_reconciler_pass_survive_a_wrong_typed_cache(self):
        sid = self.sid(PANE)
        (self.state_dir / "active-sessions.json").write_text(json.dumps(corrupt_cache(sid, time.time(), self.host)))
        handle_agent_status_changed({"agent_status": "done", "pane_id": PANE, "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            record = data["sessions"][sid]
        self.assertIsInstance(record["seq"], int)
        self.assertEqual(record["desired_state"], "Done")
        self.assertTrue(self.bridge.events_for(sid), "the event was applied and delivered, not lost")


class EvictionEvidenceTests(SandboxTestCase):
    """Gate finding (review round 7): ``delivered_seq == seq`` held for a record with neither field (None == None), so
    an Ended with no delivery evidence - e.g. one whose wrong-typed seq fields R55 dropped - was cap-evictable."""
    start_bridge = False

    def test_ended_without_sequence_evidence_is_not_evictable(self):
        for record in ({"desired_state": "Ended"}, {"desired_state": "Ended", "seq": None, "delivered_seq": None},
                       {"desired_state": "Ended", "seq": True, "delivered_seq": True}):
            with self.subTest(record=record):
                self.assertFalse(safe_to_evict(record))
        self.assertTrue(safe_to_evict({"desired_state": "Ended", "seq": 4, "delivered_seq": 4}), "control")
        self.assertTrue(safe_to_evict({"desired_state": "Ended", "delivered_state": "Ended"}), "control")

    def test_stale_delivered_state_does_not_cover_a_newer_undelivered_ended(self):
        """Gate finding (review round 9): an older Ended's late success leaves ``delivered_state: Ended`` while a newer
        Ended (higher seq) is still undelivered; that record must not count as confirmed."""
        newer = {"desired_state": "Ended", "delivered_state": "Ended", "seq": 4, "delivered_seq": 2}
        self.assertFalse(safe_to_evict(newer))
        self.assertTrue(safe_to_evict({**newer, "delivered_seq": 4}), "control: the current seq landed")
        self.assertTrue(safe_to_evict({"desired_state": "Ended", "delivered_state": "Ended", "seq": 4}),
                        "control: no seq evidence against the delivered state")
        self.assertTrue(safe_to_evict({**newer, "orphaned_ended": True}), "control: mirrored to the orphan file")

    def test_wrong_typed_sequence_fields_never_make_a_record_evictable(self):
        sid = "herdr:dev-one:" + PANE
        raw = {"sessions": {sid: {"pane_id": PANE, "desired_state": "Ended", "seq": "corrupt", "delivered_seq": "x",
                                  "last_event_at": 0}}}
        data = normalize_cache(raw, lambda: "dev-one")
        filler = {f"herdr:dev-one:w9:p{i}": {"desired_state": "Ended", "seq": 1, "delivered_seq": 1,
                                            "last_event_at": 1 + i} for i in range(2)}
        self.assertNotIn(sid, sessions_to_prune({**data["sessions"], **filler}, cap=1))


class PruneOrderTests(SandboxTestCase):
    """Gate finding (review round 8): R55 keeps ``last_event_at: null``, and the cap prune sorted it against numbers
    (``TypeError``) on every save once the cache held more than 256 sessions."""
    start_bridge = False

    def test_null_or_missing_event_time_sorts_oldest_without_raising(self):
        sessions = {f"s{i:03d}": {"desired_state": "Ended", "seq": 1, "delivered_seq": 1, "last_event_at": 10.0 + i}
                    for i in range(257)}
        sessions["s-null"] = {"desired_state": "Ended", "seq": 1, "delivered_seq": 1, "last_event_at": None}
        sessions["s-missing"] = {"desired_state": "Ended", "seq": 1, "delivered_seq": 1}
        self.assertEqual(sessions_to_prune(sessions)[:2], ["s-null", "s-missing"])
        self.assertEqual(len(sessions_to_prune(sessions)), 3)

    def test_a_save_over_the_cap_with_a_null_event_time_succeeds(self):
        with self.cache_mgr as data:
            for i in range(257):
                data["sessions"][f"herdr:h:w1:p{i}"] = {"desired_state": "Ended", "seq": 1, "delivered_seq": 1,
                                                       "last_event_at": 10.0 + i}
            data["sessions"]["herdr:h:w1:pNull"] = {"desired_state": "Ended", "seq": 1, "delivered_seq": 1,
                                                   "last_event_at": None}
            self.cache_mgr.save(data)
            self.assertEqual(len(data["sessions"]), 256)
            self.assertNotIn("herdr:h:w1:pNull", data["sessions"])


class FlagAndListEntryTests(SandboxTestCase):
    """Gate finding (review round 10): R55 left boolean safety flags and list entries unchecked, so ``"salvaged":
    "false"`` (truthy) made a live session cap-evictable and ``pending_vendor_cleanups: [42]`` crashed Step C."""

    def test_non_boolean_safety_flags_are_dropped(self):
        raw = {"sessions": {
            "live": {"desired_state": "Working", "seq": 3, "delivered_seq": 3, "salvaged": "false"},
            "ended": {"desired_state": "Ended", "seq": 4, "delivered_seq": 3, "orphaned_ended": "false",
                      "orphan_mirror_owed": 0},
            "real": {"desired_state": "Ended", "seq": 4, "delivered_seq": 3, "orphaned_ended": True}}}
        sessions = normalize_cache(raw, lambda: "dev-one")["sessions"]
        self.assertNotIn("salvaged", sessions["live"])
        self.assertNotIn("orphaned_ended", sessions["ended"])
        self.assertNotIn("orphan_mirror_owed", sessions["ended"])
        self.assertFalse(safe_to_evict(sessions["live"]))
        self.assertFalse(safe_to_evict(sessions["ended"]))
        self.assertTrue(safe_to_evict(sessions["real"]), "control: a real boolean is kept")

    def test_malformed_list_entries_are_dropped(self):
        good_cleanup = {"pane_id": "w1:pX", "is_pane_closed": True, "timestamp": 1.0}
        good_comp = {"session_id": "herdr:h:w1:pY", "pane_id": "w1:pY"}
        raw = {"sessions": {}, "pending_vendor_cleanups": [42, {"pane_id": 5}, "x", good_cleanup],
               "pending_compensations": [42, {"session_id": ""}, [1], good_comp]}
        data = normalize_cache(raw, lambda: "dev-one")
        self.assertEqual(data["pending_vendor_cleanups"], [good_cleanup])
        self.assertEqual(data["pending_compensations"], [good_comp])

    def test_step_c_saves_a_confirmed_delivery_despite_a_malformed_cleanup_entry(self):
        sid = self.sid(PANE)
        handle_agent_status_changed({"agent_status": "working", "pane_id": PANE, "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        cache_file = self.state_dir / "active-sessions.json"
        raw = json.loads(cache_file.read_text())
        raw["pending_vendor_cleanups"] = [42]
        cache_file.write_text(json.dumps(raw))
        handle_agent_status_changed({"agent_status": "done", "pane_id": PANE, "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            record = data["sessions"][sid]
        self.assertEqual((record["desired_state"], record["delivered_seq"]), ("Done", record["seq"]))


class DismissalCapTests(SandboxTestCase):
    """Gate finding (review round 6): sorting > 64 dismissals by a mixed-type timestamp raised TypeError at load."""
    start_bridge = False

    def test_mixed_type_dismissal_timestamps_load_and_keep_the_newest(self):
        dismissed = {f"uuid-{i:03d}": {"timestamp": float(i)} for i in range(70)}
        dismissed["uuid-obj"] = {"timestamp": {"bad": 1}}
        dismissed["uuid-str"] = {"timestamp": "later"}
        dismissed["uuid-raw"] = "not an object"
        raw = {"sessions": {}, "dismissed_vendor_uuids": dismissed}
        data = normalize_cache(raw, lambda: "dev-one")
        kept = data["dismissed_vendor_uuids"]
        self.assertEqual(len(kept), 64)
        self.assertEqual(sorted(kept)[-1], "uuid-069", "the newest numeric stamps are kept")
        self.assertNotIn("uuid-obj", kept, "an unusable stamp sorts oldest")

    def test_a_cache_with_such_dismissals_still_opens(self):
        dismissed = {f"uuid-{i:03d}": {"timestamp": i} for i in range(70)}
        dismissed["uuid-obj"] = {"timestamp": [1]}
        (self.state_dir / "active-sessions.json").write_text(json.dumps(
            {"version": 4, "host": self.host, "sessions": {}, "dismissed_vendor_uuids": dismissed}))
        with self.cache_mgr as data:
            self.assertEqual(len(data["dismissed_vendor_uuids"]), 64)


class UnanticipatedShapeTests(SandboxTestCase):
    start_bridge = False

    def test_type_error_while_normalizing_is_salvaged_not_a_crash(self):
        """Defense in depth (review round 6): a TypeError from a shape no normalizer anticipated takes the corrupt
        cache path (quarantine + salvage) like a ValueError, instead of failing every cache user."""
        (self.state_dir / "active-sessions.json").write_text(json.dumps({"version": 4, "sessions": {}}))
        with mock.patch("herdr_bartender.cache.normalize_cache", side_effect=TypeError("unanticipated")):
            with self.cache_mgr as data:
                self.assertEqual(data["sessions"], {})
        self.assertTrue(list(self.state_dir.glob(CORRUPT_PREFIX + "*")), "the bad cache was quarantined")


if __name__ == "__main__":
    unittest.main()
