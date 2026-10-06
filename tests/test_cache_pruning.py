"""Save-time pruning: 256-session cap safety and the 60s tombstone TTL (Plan §4.1, §4.2)."""

import json
import unittest

from herdr_bartender.cache import SESSION_CAP, TOMBSTONE_TTL_NS, prepare_for_save, sessions_to_prune
from herdr_bartender.cache_schema import new_cache
from tests.support import SandboxTestCase

NOW = 2_000_000_000.0
NOW_NS = int(NOW * 1e9)


def live(at, state="Working", seq=3, delivered_seq=2):
    return {"desired_state": state, "delivered_state": "Working", "seq": seq, "delivered_seq": delivered_seq,
            "last_event_at": at}


def ended_delivered(at):
    return {"desired_state": "Ended", "delivered_state": "Ended", "seq": 4, "delivered_seq": 4, "last_event_at": at}


def ended_undelivered(at):
    return {"desired_state": "Ended", "delivered_state": "Working", "seq": 4, "delivered_seq": 3, "last_event_at": at}


def salvaged(at):
    return {"desired_state": "Idle", "delivered_state": "Idle", "seq": 1, "delivered_seq": 1, "salvaged": True,
            "last_event_at": at}


class SessionCapTests(unittest.TestCase):
    def _sessions(self, extra):
        sessions = {f"live-{i}": live(1000.0 + i) for i in range(SESSION_CAP - len(extra) + 3)}
        sessions.update(extra)
        return sessions

    def test_cap_evicts_only_safe_records_oldest_first(self):
        """Plan §4.1 256-cap (finding c12): only Ended-and-delivered (or orphaned) and salvaged records are evictable,
        oldest first; undelivered Working/Ended sessions are never dropped."""
        extra = {
            "ended-old": ended_delivered(10.0), "salvaged-mid": salvaged(20.0), "orphaned": {
                **ended_undelivered(30.0), "orphaned_ended": True},
            "ended-new": ended_delivered(5000.0), "undelivered-ended": ended_undelivered(1.0),
            "undelivered-working": live(0.5),
        }
        sessions = self._sessions(extra)
        self.assertEqual(len(sessions), SESSION_CAP + 3)
        self.assertEqual(sessions_to_prune(sessions), ["ended-old", "salvaged-mid", "orphaned"])

    def test_cache_stays_above_cap_when_nothing_is_safe(self):
        """Plan §4.1: live sessions are never evicted, even if the cache stays above 256."""
        sessions = {f"live-{i}": live(float(i)) for i in range(SESSION_CAP + 5)}
        sessions["undelivered-ended"] = ended_undelivered(0.0)
        self.assertEqual(sessions_to_prune(sessions), [])
        data = {**new_cache("host"), "sessions": sessions}
        prepare_for_save(data, NOW, NOW_NS, None)
        self.assertEqual(len(data["sessions"]), SESSION_CAP + 6)

    def test_prepare_for_save_prunes_in_place(self):
        """Plan §4.1: the prune mutates data['sessions'] in place (callers keep their reference)."""
        sessions = self._sessions({"ended-old": ended_delivered(1.0), "salvaged": salvaged(2.0),
                                   "ended-older": ended_delivered(0.0)})
        data = {**new_cache("host"), "sessions": sessions}
        ref = data["sessions"]
        prepare_for_save(data, NOW, NOW_NS, None)
        self.assertIs(data["sessions"], ref)
        self.assertEqual(len(ref), SESSION_CAP)
        self.assertNotIn("ended-older", ref)
        self.assertNotIn("ended-old", ref)
        self.assertNotIn("salvaged", ref)


class TombstoneTtlTests(SandboxTestCase):
    start_bridge = False

    def test_tombstones_older_than_60s_are_dropped_on_save(self):
        """Plan §4.1 tombstone TTL (finding c11): a tombstone 61s old is dropped at save, a 59s one is kept;
        the legacy int/float (closed_at_ns) forms age the same way."""
        self.assertEqual(TOMBSTONE_TTL_NS, 60_000_000_000)
        clock = self.use_fake_clock(start=NOW)
        now_ns = clock.time_ns()
        with self.cache_mgr as data:
            data["tombstones"] = {
                "w1:pOld": {"closed_at_ns": now_ns - 61_000_000_000, "closed_source_ts": 0.0},
                "w1:pFresh": {"closed_at_ns": now_ns - 59_000_000_000, "closed_source_ts": 0.0},
                "w1:pLegacyOld": now_ns - 61_000_000_000,
                "w1:pLegacyFresh": float(now_ns - 59_000_000_000),
            }
            self.cache_mgr.save(data)
        on_disk = json.loads(self.cache_mgr.cache_file.read_text())["tombstones"]
        self.assertEqual(sorted(on_disk), ["w1:pFresh", "w1:pLegacyFresh"])


if __name__ == "__main__":
    unittest.main()
