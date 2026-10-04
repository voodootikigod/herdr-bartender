"""Corrupt-cache quarantine and salvage (Plan §6.3, §10.1 #12/#50/#58)."""

import json
import os
import re
import time
import unittest
from unittest import mock

from herdr_bartender import cache as cache_mod
from herdr_bartender import runtime, watchdog
from herdr_bartender.cache_schema import SALVAGE_EPOCH_FLOOR
from herdr_bartender.orphans import run_replay_orphans
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.salvage import QUARANTINE_RETENTION_SECONDS, parse_pane_generations, prune_corrupt_quarantine
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.spool import replay_spool_dir
from tests.support import SandboxTestCase

CORRUPT_NAME_RE = re.compile(r"^active-sessions\.json\.corrupt\.\d{10}$")


class CacheSalvageTests(SandboxTestCase):
    def _panes_dir(self):
        panes = self.state_dir / "panes"
        panes.mkdir(parents=True, exist_ok=True)
        return panes

    def _write_corrupt(self, *panes, host=None):
        sids = [f"herdr:{host or self.host}:{pane}" for pane in panes]
        text = '{"sessions": {' + ", ".join(f'"{sid}": {{"pane_id": "{sid.split(":", 2)[2]}"' for sid in sids)
        self.cache_mgr.cache_file.write_text(text + ", invalid json...")
        return sids

    def _assert_quiescent(self, record, sid):
        self.assertIs(record["salvaged"], True)
        self.assertEqual((record["desired_state"], record["delivered_state"]), ("Idle", "Idle"))
        self.assertEqual((record["seq"], record["delivered_seq"]), (1, 1))
        self.assertEqual(record["delivery_status"], "salvaged")
        self.assertEqual(record["desired_payload"]["session_id"], sid)
        self.assertGreaterEqual(record["generation"], SALVAGE_EPOCH_FLOOR)

    def test_p12_corrupt_cache_quarantine_and_salvage(self):
        """Plan §10.1 #12 (gap t12-quarantine-signal): quarantined to .corrupt.<ts>; salvaged with seq == delivered_seq,
        not Ended, and never transmitted automatically."""
        (sid,) = self._write_corrupt("w1:pSalvage")
        with self.cache_mgr as data:
            record = data["sessions"][sid]
            self._assert_quiescent(record, sid)
            self.assertNotEqual(record["desired_state"], "Ended")
            self.assertEqual(data["pane_generations"]["w1:pSalvage"], record["generation"])
            self.assertLessEqual(record["generation"], data["next_generation"])
        corrupt = [p.name for p in self.state_dir.glob("active-sessions.json.corrupt.*")]
        self.assertEqual(len(corrupt), 1)
        self.assertRegex(corrupt[0], CORRUPT_NAME_RE)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.events_for(sid), [], "salvaged sessions are excluded from automatic transmission")

    def test_p12_signal_in_critical_section_only_sets_flag(self):
        """Plan §10.1 #12 / §6.1 L727 (gap t12-quarantine-signal): SIGALRM inside the critical section sets the
        pending flag and returns; the cache is completed and saved before the deferred exit."""
        with self.assertRaises(SystemExit):
            with self.cache_mgr as data:
                watchdog._timeout_watchdog(14, None)
                self.assertTrue(runtime.PENDING_WATCHDOG_EXIT)
                data["sessions"]["after-signal"] = {"seq": 1}
                self.cache_mgr.save(data)
        self.assertIn("after-signal", json.loads(self.cache_mgr.cache_file.read_text())["sessions"])

    @staticmethod
    def _pre_crash_envelope(event_name, event_data, arrival_ns):
        return {"event_name": event_name, "event_data": event_data, "context": {}, "arrival_ns": arrival_ns,
                "enqueued_ns": arrival_ns}

    def test_p50_salvage_preserves_close_envelopes(self):
        """Plan §10.1 #50 (gaps t50-salvage-hostname, test-salvage-hollow, salvage-agent-exit-detection):
        close envelopes (incl. agent exit and unparseable ones) stay in spool/, status envelopes go to spool/bad/,
        sessions are staged quiescently, markers are removed and nothing is sent."""
        spool = self.state_dir / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        before_crash = time.time_ns() - 5_000_000_000
        envs = {
            "001_close.json": self._pre_crash_envelope("pane.closed", {"pane_id": "w1:pC"}, before_crash),
            "002_status.json": self._pre_crash_envelope(
                "pane.agent_status_changed", {"pane_id": "w1:pS", "agent_status": "working", "agent": "claude"},
                before_crash + 1),
            "003_exit.json": self._pre_crash_envelope(
                "pane.agent_status_changed", {"pane_id": "w1:pE", "agent_status": "idle", "agent": ""}, before_crash + 2),
            "004_tab.json": self._pre_crash_envelope("tab.closed", {"tab_id": "w1:t1"}, before_crash + 3),
        }
        for name, env in envs.items():
            (spool / name).write_text(json.dumps(env))
        (spool / "005_garbled.json").write_text("{not json")
        panes = self._panes_dir()
        hex_salvaged = get_hex_pane_id("w1:pSalvaged")
        (panes / hex_salvaged).write_text("1")
        (panes / f"{hex_salvaged}.failed").write_text("1")
        *_, sid = self._write_corrupt("w1:pC", "w1:pE", "w1:pSalvaged")

        with self.cache_mgr as data:
            self._assert_quiescent(data["sessions"][sid], sid)
        kept = sorted(p.name for p in spool.glob("*.json"))
        self.assertEqual(kept, ["001_close.json", "003_exit.json", "004_tab.json", "005_garbled.json"])
        self.assertEqual(sorted(p.name for p in (spool / "bad").glob("*.json")), ["002_status.json"])
        self.assertFalse((panes / hex_salvaged).exists(), "salvaged pane marker removed")
        self.assertFalse((panes / f"{hex_salvaged}.failed").exists(), "remove_pane_marker clears .failed too")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.events_for(sid), [])

    def test_p50_preserved_closes_replay_against_salvaged_sessions(self):
        """Plan §6.3 step 2 / §10.1 #50 (finding: salvaged admitted_at_ns hides pre-crash closes): close and
        agent-exit envelopes kept in spool/ end the salvaged sessions they target when replayed (the salvage time
        is not a real admission), so no phantom survives on Top Shelf; untouched salvaged records stay quiescent."""
        spool = self.state_dir / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        before_crash = time.time_ns() - 5_000_000_000
        (spool / "001_close.json").write_text(json.dumps(
            self._pre_crash_envelope("pane.closed", {"pane_id": "w1:pC"}, before_crash)))
        (spool / "002_exit.json").write_text(json.dumps(self._pre_crash_envelope(
            "pane.agent_status_changed", {"pane_id": "w1:pE", "agent_status": "idle", "agent": ""}, before_crash + 1)))
        sid_closed, sid_exited, sid_quiet = self._write_corrupt("w1:pC", "w1:pE", "w1:pQuiet")

        batch = replay_spool_dir(self.state_dir)

        self.assertEqual(set(batch.staged_sessions), {sid_closed, sid_exited})
        self.assertEqual(list(spool.glob("*.json")), [])
        with self.cache_mgr as data:
            closed, exited = data["sessions"][sid_closed], data["sessions"][sid_exited]
            self.assertEqual((closed["desired_state"], closed["seq"], closed["close_kind"], closed["salvaged"]),
                             ("Ended", 2, "container", False))
            self.assertEqual(data["tombstones"]["w1:pC"]["closed_at_ns"], before_crash)
            self.assertEqual((exited["desired_state"], exited["seq"], exited["close_kind"], exited["salvaged"]),
                             ("Ended", 2, "agent_exit", False))
            self.assertIn("w1:pE", data["agent_exits"])
            self._assert_quiescent(data["sessions"][sid_quiet], sid_quiet)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid_closed)], ["Ended"])
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid_exited)], ["Ended"])
        self.assertEqual(self.bridge.events_for(sid_quiet), [])

    def test_salvage_marker_removal_is_per_candidate(self):
        """Plan §6.3 step 4 (gap salvage-marker-removal-scope): only salvaged panes lose markers; unrelated files stay."""
        panes = self._panes_dir()
        other = get_hex_pane_id("w2:pOther")
        keep = [panes / other, panes / f"{other}.vendor_active", panes / f"{other}.va.tmp"]
        for path in keep:
            path.write_text("x")
        self._write_corrupt("w1:pOnly")
        with self.cache_mgr:
            pass
        for path in keep:
            self.assertTrue(path.exists(), path.name)

    def test_salvage_is_persisted_by_read_only_callers(self):
        """Plan §6.3 step 6 (gap salvage-not-persisted): a read-only with-block persists the salvaged cache."""
        (sid,) = self._write_corrupt("w1:pPersist")
        with self.cache_mgr as data:
            self.assertIn(sid, data["sessions"])  # no save()
        on_disk = json.loads(self.cache_mgr.cache_file.read_text())
        self.assertIn(sid, on_disk["sessions"])
        self.assertEqual(on_disk["version"], 4)
        with self.cache_mgr as data:
            self.assertIn(sid, data["sessions"])
        self.assertEqual(len(list(self.state_dir.glob("active-sessions.json.corrupt.*"))), 1)

    def test_salvage_pins_host_from_salvaged_ids(self):
        """Plan L273 (gaps salvage-host-pin, t50-salvage-hostname): the root host is the salvaged ids' host."""
        self._write_corrupt("w1:pA", "w1:pB", host="oldmac")
        with self.cache_mgr as data:
            self.assertEqual(data["host"], "oldmac")
            self.assertIn("herdr:oldmac:w1:pA", data["sessions"])

    def test_pane_generation_parse_handles_colon_ids(self):
        """Plan §6.3 step 4 (gap salvage-pane-gen-parse): colon-bearing pane ids parse correctly."""
        raw = '{"pane_generations": {"w1:p1": 5, "w2:p9": 12, "bad id": 3}, "sessions": {'
        self.assertEqual(parse_pane_generations(raw), {"w1:p1": 5, "w2:p9": 12})

    def test_salvage_without_candidates_still_dominates(self):
        """Gap load-nameerror-salvage: a corrupt cache with no candidate ids yields an epoch-dominating empty cache."""
        self.cache_mgr.cache_file.write_text("\x00\x01 garbage")
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"], {})
            self.assertGreaterEqual(data["next_generation"], SALVAGE_EPOCH_FLOOR)

    def test_quarantine_files_pruned_after_7_days(self):
        """Plan §6.3 step 1 (gap quarantine-prune-7d): .corrupt.<ts>[.<n>] files older than exactly 7 days are
        removed; younger ones stay."""
        self.assertEqual(QUARANTINE_RETENTION_SECONDS, 7 * 86400)
        now = time.time()
        old = self.state_dir / f"active-sessions.json.corrupt.{int(now - 7 * 86400 - 60)}"
        old_suffixed = self.state_dir / f"active-sessions.json.corrupt.{int(now - 7 * 86400 - 60)}.1"
        fresh = self.state_dir / f"active-sessions.json.corrupt.{int(now - 7 * 86400 + 60)}"
        fresh_suffixed = self.state_dir / f"active-sessions.json.corrupt.{int(now - 7 * 86400 + 60)}.2"
        for path in (old, old_suffixed, fresh, fresh_suffixed):
            path.write_text("{")
        self.assertEqual(sorted(prune_corrupt_quarantine(self.state_dir, now)), sorted([old, old_suffixed]))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(fresh_suffixed.exists())
        old.write_text("{")
        self._write_corrupt("w1:pPrune")
        with self.cache_mgr:
            pass
        self.assertFalse(old.exists(), "salvage prunes stale quarantine files")

    def test_salvage_write_failure_keeps_corrupt_file(self):
        """Gap save-errors-swallowed: if the salvaged cache cannot be written the corrupt file stays in place."""
        from herdr_bartender import cache as cache_mod
        self._write_corrupt("w1:pKeep")
        raw = self.cache_mgr.cache_file.read_bytes()
        with mock.patch.object(cache_mod.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(cache_mod.CacheWriteError):
                with self.cache_mgr:
                    pass
        self.assertEqual(self.cache_mgr.cache_file.read_bytes(), raw)
        self.assertFalse(runtime.IN_CRITICAL_SECTION)

    def test_salvage_install_failure_leaves_corrupt_cache_in_place(self):
        """Plan §6.3 non-destructive salvage / step 6 (findings: salvage install window): if installing the salvaged
        cache fails, active-sessions.json still holds the corrupt original (never a window with no cache), no
        quarantine or salvage tmp is left, and the next locked load salvages again with epoch-dominating
        generations instead of starting from an empty cache at generation 1."""
        (sid,) = self._write_corrupt("w1:pInstall")
        raw = self.cache_mgr.cache_file.read_bytes()
        real_replace = os.replace

        def fail_install(src, dst, *args, **kwargs):
            if str(dst) == str(self.cache_mgr.cache_file) and ".salvage." in str(src):
                raise OSError(5, "EIO while installing the salvaged cache")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(cache_mod.os, "replace", side_effect=fail_install):
            with self.assertRaises(cache_mod.CacheWriteError):
                with self.cache_mgr:
                    self.fail("the body must not run on an unsaved salvage")
        self.assertEqual(self.cache_mgr.cache_file.read_bytes(), raw)
        self.assertEqual(list(self.state_dir.glob("active-sessions.json.salvage.*")), [])
        self.assertEqual(list(self.state_dir.glob("active-sessions.json.corrupt.*")), [])
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        with self.cache_mgr as data:
            self._assert_quiescent(data["sessions"][sid], sid)
            self.assertGreaterEqual(data["next_generation"], SALVAGE_EPOCH_FLOOR)
        self.assertEqual(len(list(self.state_dir.glob("active-sessions.json.corrupt.*"))), 1)

    def test_quarantine_names_never_collide_within_one_second(self):
        """Plan §6.3 step 1 (finding: quarantine name resolution): two salvages in the same second keep both
        forensic copies (the second gets a numeric suffix the 7-day prune still parses)."""
        self.use_fake_clock()
        self._write_corrupt("w1:pFirst")
        with self.cache_mgr:
            pass
        self._write_corrupt("w1:pSecond")
        with self.cache_mgr:
            pass
        copies = sorted(self.state_dir.glob("active-sessions.json.corrupt.*"))
        self.assertEqual(len(copies), 2)
        self.assertRegex(copies[0].name, CORRUPT_NAME_RE)
        self.assertEqual(copies[1].name, f"{copies[0].name}.1")
        self.assertEqual(sorted("pFirst" in p.read_text() for p in copies), [False, True])

    def test_quarantine_falls_back_to_a_private_copy_without_hard_links(self):
        """Plan §6.3 step 1: where hard links are unsupported the quarantine is an exclusive 0600 copy of the
        corrupt bytes, and the salvage still completes."""
        from herdr_bartender import salvage
        (sid,) = self._write_corrupt("w1:pCopy")
        raw = self.cache_mgr.cache_file.read_bytes()
        with mock.patch.object(salvage.os, "link", side_effect=PermissionError(1, "EPERM")):
            with self.cache_mgr as data:
                self.assertIn(sid, data["sessions"])
        (copy,) = self.state_dir.glob("active-sessions.json.corrupt.*")
        self.assertEqual(copy.read_bytes(), raw)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)

    def test_salvage_generations_dominate_epoch_and_recovered_values(self):
        """Plan §6.3 step 4: generation = max(recovered pane generation, max(int(now), floor)); next_generation
        is the maximum of the epoch and every recovered generation."""
        clock = self.use_fake_clock(start=float(SALVAGE_EPOCH_FLOOR + 100_000_000))
        epoch = int(clock.time())
        far = epoch * 3
        high, low = self.sid("w1:pHigh"), self.sid("w1:pLow")
        self.cache_mgr.cache_file.write_text(
            '{"pane_generations": {"w1:pHigh": %d, "w1:pLow": 5, "w9:pGone": %d}, '
            '"sessions": {"%s": {"pane_id": "w1:pHigh"}, "%s": {, broken' % (far, far + 7, high, low))
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][high]["generation"], far)
            self.assertEqual(data["pane_generations"]["w1:pHigh"], far)
            self.assertEqual(data["sessions"][low]["generation"], epoch)
            self.assertEqual(data["pane_generations"]["w1:pLow"], epoch)
            self.assertEqual(data["next_generation"], far + 7)

    def test_p58_salvage_with_pending_orphans(self):
        """Plan §10.1 #58: an epoch salvage generation does not stop orphan replay from clearing orphans."""
        orphan_test_file = self.state_dir / "test-salvage-orphans.json"
        sid_58 = self.sid("w1:pSalvageOrphan")
        orphan_test_file.write_text(json.dumps({
            "version": 1,
            "sessions": {
                sid_58: {
                    "session_id": sid_58,
                    "pane_id": "w1:pSalvageOrphan",
                    "desired_state": "Ended",
                    "generation": 3,
                    "admitted_at_ns": 500,
                    "agent": "Claude (Herdr)",
                }
            },
        }), encoding="utf-8")
        (self.state_dir / "active-sessions.json").write_text("{corrupt json...", encoding="utf-8")
        with self.cache_mgr as data:
            self.assertGreaterEqual(data.get("next_generation", 0), SALVAGE_EPOCH_FLOOR)
        self.bridge.sessions[sid_58] = {"state": "Working"}
        replay_ok = run_replay_orphans(str(orphan_test_file), bridge_url=self.mock_url)
        self.assertIs(replay_ok, True, "Orphan replay must succeed despite epoch salvage generation")
        self.assertNotIn(sid_58, self.bridge.sessions, "Orphan must clear Top Shelf state")
        self.assertFalse(orphan_test_file.exists(), "Orphan file must be cleaned up on success")


if __name__ == "__main__":
    unittest.main()
