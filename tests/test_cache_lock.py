"""Cache lock, atomic save, schema v4 and critical-section semantics (Plan §4.2, §4.3 Step A/C, §6.1, §6.2)."""

import fcntl
import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import cache, runtime
from herdr_bartender.cache import (
    BoundedSessionCache,
    CacheWriteError,
    IntegrationDisabled,
    LockTimeout,
    lock_timeout_seconds,
)
from herdr_bartender.cache_schema import SCHEMA_VERSION, normalize_cache
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock

V4_ROOT_FIELDS = (
    "version", "host", "cache_seq", "herdr_instance_id", "last_herdr_pid", "last_bartender_pid",
    "last_updated", "consecutive_failures", "last_successful_delivery", "next_generation",
    "pane_generations", "tombstones", "agent_exits", "dismissed_vendor_uuids",
    "pending_compensations", "pending_vendor_cleanups", "sessions",
)


def lock_is_free(lock_file) -> bool:
    """True when a fresh open file description can take LOCK_EX|LOCK_NB (flock is per description)."""
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class LockAcquisitionTests(SandboxTestCase):
    start_bridge = False

    def test_contended_lock_raises_lock_timeout_without_reading(self):
        """Plan §4.3 Step A contention rule (gaps lock-best-effort, lock-timeout-proceeds-unlocked):
        a lock held by another process is never 'proceeded past'; LockTimeout is raised, nothing is read."""
        hold_lock(self, self.cache_mgr.lock_file)
        t0 = time.monotonic()
        with mock.patch.object(BoundedSessionCache, "_load", side_effect=AssertionError("read unlocked")):
            with self.assertRaises(LockTimeout):
                with self.cache_mgr:
                    self.fail("body must not run without the lock")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        self.assertFalse(self.cache_mgr.held)

    def test_bounded_lock_deadline_formula(self):
        """Plan §4.3 L390 / R9: bounded path waits min(0.2, max(0.02, time_remaining() - 0.3))."""
        runtime.set_deadline_mode(runtime.DEADLINE_BOUNDED)
        runtime.PROCESS_DEADLINE_SECONDS = 1.5
        for elapsed, expected in ((0.0, 0.2), (1.0, 0.2), (1.1, 0.1), (1.25, 0.02), (1.45, 0.02)):
            with self.subTest(elapsed=elapsed):
                runtime.START_TIME = time.monotonic() - elapsed
                self.assertAlmostEqual(lock_timeout_seconds(), expected, delta=0.01)

    def test_unbounded_callers_wait_longer(self):
        """R10 / gap lock-best-effort: reconciler/--cleanup (unbounded) get their own longer lock budget."""
        runtime.set_deadline_mode(runtime.DEADLINE_UNBOUNDED)
        self.assertGreaterEqual(lock_timeout_seconds(), 1.0)
        self.assertEqual(BoundedSessionCache(self.state_dir, lock_timeout=0.05).effective_lock_timeout(), 0.05)

    def test_in_critical_section_true_exactly_while_locked(self):
        """Plan §6.1 (gap in-critical-section-flag): the flag is set only after flock and cleared only after unlock."""
        seen = {}
        real_flock = fcntl.flock

        def spy(fd, op):
            seen.setdefault("before_lock", runtime.IN_CRITICAL_SECTION) if op & fcntl.LOCK_EX else None
            if op == fcntl.LOCK_UN:
                seen["at_unlock"] = runtime.IN_CRITICAL_SECTION
            return real_flock(fd, op)

        with mock.patch.object(cache.fcntl, "flock", side_effect=spy):
            with self.cache_mgr as data:
                seen["inside"] = runtime.IN_CRITICAL_SECTION
                self.cache_mgr.save(data)
                seen["after_save"] = runtime.IN_CRITICAL_SECTION
        self.assertEqual(seen, {"before_lock": False, "inside": True, "after_save": True, "at_unlock": True})
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        self.assertTrue(lock_is_free(self.cache_mgr.lock_file))

    def test_load_failure_releases_lock_and_flag(self):
        """Gap load-nameerror-salvage: an exception while loading releases the flock and resets the flag."""
        with mock.patch.object(BoundedSessionCache, "_load", side_effect=OSError("EACCES")):
            with self.assertRaises(OSError):
                with self.cache_mgr:
                    pass
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        self.assertTrue(lock_is_free(self.cache_mgr.lock_file))

    def test_disabled_rechecked_under_lock(self):
        """Plan §4.3 L392/L484 (gap is-disabled-under-lock): check_disabled re-reads DISABLED after flock."""
        guarded = BoundedSessionCache(self.state_dir, check_disabled=True)
        (self.state_dir / "DISABLED").touch()
        with self.assertRaises(IntegrationDisabled):
            with guarded:
                self.fail("body must not run while disabled")
        self.assertTrue(lock_is_free(guarded.lock_file))
        with self.cache_mgr as data:  # --cleanup style callers bypass DISABLED
            self.assertIn("sessions", data)


class DeferredExitTests(SandboxTestCase):
    start_bridge = False

    def _assert_deferred_exit(self, body):
        with self.assertRaises(SystemExit) as ctx:
            body()
        self.assertEqual(ctx.exception.code, 0)
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        self.assertTrue(lock_is_free(self.cache_mgr.lock_file), "exit must come after the lock is released")
        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "deferred exit hands off to the reconciler")

    def test_deferred_exit_after_save(self):
        """Plan §6.1 L728 (gap deferred-exit-only-in-save): exit happens after __exit__ unlocks, not inside save()."""
        def body():
            with self.cache_mgr as data:
                runtime.PENDING_WATCHDOG_EXIT = True  # SIGALRM arrived inside the critical section
                data["sessions"]["x"] = {"seq": 1}
                self.cache_mgr.save(data)
                self.assertTrue(runtime.IN_CRITICAL_SECTION, "save() must not end the critical section")
        self._assert_deferred_exit(body)
        self.assertIn("x", json.loads(self.cache_mgr.cache_file.read_text())["sessions"])

    def test_deferred_exit_on_early_return_without_save(self):
        """Gap deferred-exit-only-in-save: a with-block that returns early still honors the pending exit."""
        def locked_lookup():
            with self.cache_mgr as data:
                runtime.PENDING_WATCHDOG_EXIT = True
                return data.get("sessions")
        self._assert_deferred_exit(locked_lookup)

    def test_deferred_exit_on_exception(self):
        """Gap in-critical-section-flag: an exception inside the critical section still exits 0 after unlock."""
        def failing():
            with self.cache_mgr:
                runtime.PENDING_WATCHDOG_EXIT = True
                raise ValueError("boom")
        self._assert_deferred_exit(failing)


class AtomicSaveTests(SandboxTestCase):
    start_bridge = False

    def test_save_failure_raises_and_removes_tmp(self):
        """Plan §6.2 (gap save-errors-swallowed): a failed write raises CacheWriteError, keeps the old cache, leaves no tmp."""
        with self.cache_mgr as data:
            data["sessions"]["keep"] = {"seq": 1}
            self.cache_mgr.save(data)
        before = self.cache_mgr.cache_file.read_bytes()
        with self.cache_mgr as data:
            data["sessions"]["lost"] = {"seq": 2}
            with mock.patch.object(cache.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaises(CacheWriteError):
                    self.cache_mgr.save(data)
        self.assertEqual(self.cache_mgr.cache_file.read_bytes(), before)
        self.assertEqual(list(self.state_dir.glob("active-sessions.json.tmp.*")), [])

    def test_save_requires_held_lock(self):
        """Plan §6.2: every mutation is persisted under the lock; save() outside the with-block refuses."""
        with self.cache_mgr as data:
            pass
        with self.assertRaises(CacheWriteError):
            self.cache_mgr.save(data)
        self.assertFalse(self.cache_mgr.cache_file.exists())

    def test_cache_seq_advances_on_every_save(self):
        """Plan §4.1 L272: cache_seq strictly increments on each save()."""
        seqs = []
        for _ in range(3):
            with self.cache_mgr as data:
                self.cache_mgr.save(data)
            seqs.append(json.loads(self.cache_mgr.cache_file.read_text())["cache_seq"])
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(len(seqs), 3)

    def test_save_never_reads_orphan_file(self):
        """Plan §1 L108 (gap save-reads-orphans-under-lock): the 512-cap prune uses caller-supplied orphan panes."""
        orphan = self.home / ".herdr-bartender-orphans.json"
        orphan.write_text(json.dumps({"version": 1, "sessions": {"o": {"pane_id": "w9:pOrphan"}}}))
        opened = []
        real_open = open

        def spy_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with self.cache_mgr as data:
            data["pane_generations"] = {f"w9:p{i}": i for i in range(600)}
            data["pane_generations"]["w9:pOrphan"] = 0
            with mock.patch("builtins.open", side_effect=spy_open):
                self.cache_mgr.save(data)
            self.assertEqual(len(data["pane_generations"]), 601, "without orphan info the prune is deferred")
            self.cache_mgr.save(data, orphan_panes=frozenset({"w9:pOrphan"}))
        self.assertNotIn(str(orphan), opened)
        saved = json.loads(self.cache_mgr.cache_file.read_text())["pane_generations"]
        self.assertEqual(len(saved), 512)
        self.assertIn("w9:pOrphan", saved, "orphan-referenced panes are protected even with the lowest generation")


class OrphanPaneProtectionTests(SandboxTestCase):
    def test_orphan_pane_ids_read_outside_cache_lock(self):
        """Gap save-reads-orphans-under-lock: orphan pane ids come from the orphan file (and its journal) under the
        orphan lock, never while the cache lock is held; None on orphan-lock contention."""
        from herdr_bartender import orphans
        orphans.export_orphan_record("s1", {"pane_id": "w1:pO1"}, blocking=True)
        orphans._journal_op(orphans.get_orphan_path(), {"op": "export", "sid": "s2", "session": {"pane_id": "w1:pO2"}})
        seen = []
        real = orphans._read_orphan_sessions

        def spy(path):
            seen.append(runtime.IN_CRITICAL_SECTION)
            return real(path)

        with mock.patch.object(orphans, "_read_orphan_sessions", side_effect=spy):
            self.assertEqual(orphans.orphan_pane_ids(), frozenset({"w1:pO1", "w1:pO2"}))
        self.assertEqual(seen, [False])
        hold_lock(self, orphans._lock_path(orphans.get_orphan_path()))
        self.assertIsNone(orphans.orphan_pane_ids())

    def test_reconciler_sweep_prunes_pane_generations_protecting_orphans(self):
        """Plan §5.1 item 12: the reconciler's save prunes pane_generations to 512, keeping orphan panes."""
        from herdr_bartender.orphans import export_orphan_record
        from herdr_bartender.reconciler import reconcile_active_sessions
        export_orphan_record("orphan-sid", {"pane_id": "w9:pOrphan"}, blocking=True)
        with self.cache_mgr as data:
            data["pane_generations"] = {f"w9:p{i}": i + 10 for i in range(600)}
            data["pane_generations"]["w9:pOrphan"] = 1
            data["sessions"][self.sid("w9:pLive")] = {"pane_id": "w9:pLive", "desired_state": "Working", "seq": 1,
                                                      "delivered_seq": 1, "delivery_status": "delivered",
                                                      "last_event_at": time.time()}
            self.cache_mgr.save(data)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(len(data["pane_generations"]), 512)
            self.assertIn("w9:pOrphan", data["pane_generations"])


class SchemaTests(SandboxTestCase):
    start_bridge = False

    def test_new_cache_is_schema_v4(self):
        """Plan §4.2 (gap cache-schema-version-fields): a fresh cache carries version 4 and every root field."""
        with self.cache_mgr as data:
            for field in V4_ROOT_FIELDS:
                self.assertIn(field, data, field)
            self.assertEqual(data["version"], SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 4)
            self.assertEqual(data["host"], self.host)

    def test_v1_cache_is_migrated(self):
        """Gap cache-schema-version-fields: a v1 cache is migrated in place; next_generation dominates every generation."""
        v1 = {"version": 1, "host": "oldhost", "sessions": {"s": {"generation": 9, "pane_id": "w1:p1"}},
              "pane_generations": {"w1:p1": 7}}
        self.cache_mgr.cache_file.write_text(json.dumps(v1))
        with self.cache_mgr as data:
            self.assertEqual(data["version"], 4)
            self.assertEqual(data["host"], "oldhost", "the pinned host survives migration")
            self.assertGreaterEqual(data["next_generation"], 9)
            for field in V4_ROOT_FIELDS:
                self.assertIn(field, data, field)

    def test_reconciler_persists_herdr_instance_id(self):
        """Plan §4.2 L313 (gap cache-schema-version-fields): herdr_instance_id = "<pid>:<start time>" is persisted
        alongside last_herdr_pid when the reconciler records the Herdr instance."""
        from herdr_bartender.process import get_herdr_pid, get_process_start_time
        from herdr_bartender.reconciler import reconcile_active_sessions
        with self.cache_mgr as data:
            data["sessions"][self.sid("w1:pInst")] = {"pane_id": "w1:pInst", "desired_state": "Working", "seq": 1,
                                                      "delivered_seq": 1, "delivery_status": "delivered",
                                                      "last_event_at": time.time()}
            self.cache_mgr.save(data)
        reconcile_active_sessions(self.state_dir)
        pid = get_herdr_pid()
        with self.cache_mgr as data:
            self.assertEqual(data["last_herdr_pid"], pid)
            self.assertEqual(data["herdr_instance_id"], f"{pid}:{get_process_start_time(pid)}")

    def test_normalize_drops_malformed_session_records(self):
        """Gap cache-schema-version-fields: non-dict session records and wrong-typed root fields are repaired."""
        data = normalize_cache({"sessions": {"ok": {"seq": 1}, "bad": "x"}, "tombstones": [], "pending_compensations": {}},
                               lambda: "h")
        self.assertEqual(list(data["sessions"]), ["ok"])
        self.assertEqual(data["tombstones"], {})
        self.assertEqual(data["pending_compensations"], [])


if __name__ == "__main__":
    unittest.main()
