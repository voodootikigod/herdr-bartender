"""Platform layer: start times, probe timeouts, the 0.5s liveness cache, Herdr liveness, orphan lock mode."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import unittest
from unittest import mock

from herdr_bartender import orphans, process, runtime
from herdr_bartender.orphans import export_orphan_record, flush_pending_orphan_ops, remove_orphan_record
from tests.support import SandboxTestCase
from tests.support.sandbox import SHIM_DIR

NO_SUCH_PID = 999_999_999  # above every pid_max: the real ps always fails for it


def hot_path_budget() -> None:
    """Run as the watchdog-bounded plugin process does (1.5s deadline from now)."""
    runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
    runtime.START_TIME = time.monotonic()
    runtime.set_deadline_mode(runtime.DEADLINE_BOUNDED)


def long_running_process() -> None:
    """Run as a production process an hour into its life with no watchdog armed (reconciler, --cleanup).

    Production default deadline, no explicit mode: the mode is inferred, exactly as in a
    reconciler that nobody told about deadline modes.
    """
    runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
    runtime.START_TIME = time.monotonic() - 3600
    runtime.set_deadline_mode(None)


def stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777


class StartTimeTests(SandboxTestCase):
    start_bridge = False

    def test_parse_lstart_is_locale_free_integer_epoch(self):
        """Plan §3.4 Herdr Process Model (gap pid-reuse-start-time-portability): lstart becomes an integer epoch string."""
        expected = str(int(time.mktime((2026, 10, 4, 8, 0, 0, 0, 0, -1))))
        self.assertEqual(process.parse_lstart("Sat Oct  4 08:00:00 2026"), expected)
        self.assertEqual(process.parse_lstart("  Sat Oct 4 08:00:00 2026\n"), expected)
        for bad in ("", "garbage", "Sam Okt  4 08:00:00 2026", "Sat Oct 4 25:00:00 2026", "Sat Oct 4 08:00 2026"):
            self.assertIsNone(process.parse_lstart(bad), bad)

    def test_unknown_start_time_is_none_not_load_time(self):
        """R14 / Plan L667-668 (gaps start-time-fallback-false-restart, pid-reuse-start-time-portability):
        a failed `ps` lookup is None, never this process's load time."""
        self.assertIsNone(process.get_process_start_time(NO_SUCH_PID))
        self.add_fake_process("herdr", pid=4242, lstart="not-a-date")
        self.assertIsNone(process.get_process_start_time(4242))
        self.assertIsNone(process.get_process_start_time(0))
        self.assertIsNone(process.get_process_start_time(-5))

    def test_ps_runs_in_c_locale_with_timeout(self):
        """Plan §6.1 bounded operations (gaps subprocess-no-timeout, pid-reuse-start-time-portability):
        every ps/pgrep probe has a timeout and runs with LC_ALL=C."""
        self.add_fake_process("Bartender 6", pid=900, lstart="Sat Oct  4 07:00:00 2026")
        self.set_herdr_alive()
        with mock.patch.object(process.subprocess, "run", wraps=subprocess.run) as run:
            process.get_bartender_pid()
            process.get_herdr_pid()
            process.get_process_start_time(900)
        self.assertGreater(run.call_count, 0)
        for call in run.call_args_list:
            self.assertIn(call.args[0][0], ("ps", "pgrep"))
            self.assertIsNotNone(call.kwargs.get("timeout"), call)
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertEqual(call.kwargs["env"]["LC_ALL"], "C")

    def test_instance_alive_unknown_start_time_is_no_information(self):
        """R14 (gap start-time-fallback-false-restart): unknown start times never reject a live holder;
        two known, different start times do (PID-reuse defense)."""
        pid = self.add_fake_process("worker", lstart="Sat Oct  4 09:00:00 2026", live=True)
        known = process.get_process_start_time(pid)
        self.assertIsNotNone(known)
        self.assertTrue(process.is_process_instance_alive(pid, known))
        self.assertFalse(process.is_process_instance_alive(pid, str(int(known) - 3600)))
        for unknown in (None, "", "None"):
            self.assertTrue(process.is_process_instance_alive(pid, unknown), unknown)
        self.assertFalse(process.is_process_instance_alive(pid, "Sat-Oct-4_legacy"), "a known mismatch")
        self.assertFalse(process.is_process_instance_alive(NO_SUCH_PID, known))

    def test_start_times_differ_requires_both_known(self):
        """R14 (gap start-time-fallback-false-restart): a restart needs two known, different start times."""
        self.assertTrue(process.start_times_differ("100", "200"))
        self.assertFalse(process.start_times_differ("100", "100"))
        self.assertTrue(process.start_times_differ("x-y", "100"), "legacy non-epoch values are known")
        for a, b in ((None, "100"), ("100", None), ("None", "100"), ("", "100"), ("100", "None")):
            self.assertFalse(process.start_times_differ(a, b), (a, b))


class OwnIdentityTests(SandboxTestCase):
    start_bridge = False

    def test_own_start_time_never_forks_inside_critical_section(self):
        """Plan §6.1 <10ms critical section (W1 verifier note): own_start_time() does not spawn ps under the lock."""
        runtime.IN_CRITICAL_SECTION = True
        with mock.patch.object(process.subprocess, "run", side_effect=AssertionError("forked")):
            self.assertIsNone(process.own_start_time())
        runtime.IN_CRITICAL_SECTION = False
        self.assertEqual(process.own_start_time(), process.get_process_start_time(os.getpid()))

    def test_warm_process_identity_resolves_before_lock(self):
        """Plan §4.1 lease_token (W1 verifier note): warm_process_identity() caches the start time for later lock holders."""
        warmed = process.warm_process_identity()
        self.assertIsNotNone(warmed)
        runtime.IN_CRITICAL_SECTION = True
        with mock.patch.object(process.subprocess, "run", side_effect=AssertionError("forked")):
            self.assertEqual(process.own_start_time(), warmed)
        runtime.IN_CRITICAL_SECTION = False


class LivenessCacheTests(SandboxTestCase):
    start_bridge = False
    default_liveness = False  # this class drives the shim process table itself

    def test_liveness_lookups_cached_for_half_a_second(self):
        """Plan §6.1 L706 / budget table L712 (gap liveness-cache-0p5s): repeated lookups within 0.5s do not re-fork."""
        fake = self.use_fake_clock()
        self.add_fake_process("Bartender 6", pid=900, lstart="Sat Oct  4 07:00:00 2026")
        self.set_herdr_alive()
        with mock.patch.object(process.subprocess, "run", wraps=subprocess.run) as run:
            first = (process.get_bartender_pid(), process.get_herdr_pid(), process.is_herdr_alive(),
                     process.get_process_start_time(900))
            forks = run.call_count
            self.assertGreater(forks, 0)
            fake.advance(0.4)
            again = (process.get_bartender_pid(), process.get_herdr_pid(), process.is_herdr_alive(),
                     process.get_process_start_time(900))
            self.assertEqual(run.call_count, forks, "cached within 0.5s")
            self.assertEqual(first, again)
            fake.advance(0.2)
            process.get_bartender_pid()
            self.assertGreater(run.call_count, forks, "expired after 0.5s")

    def test_reset_caches_forces_fresh_probe(self):
        """Gap liveness-cache-0p5s: reset_caches() is the test hook that drops every memoised lookup."""
        self.use_fake_clock()
        self.assertIsNone(process.get_bartender_pid())
        self.add_fake_process("Bartender 6", pid=901, lstart="Sat Oct  4 07:00:00 2026")
        self.assertIsNone(process.get_bartender_pid(), "still cached")
        process.reset_caches()
        self.assertEqual(process.get_bartender_pid(), 901)


class ProbeTimeoutTests(SandboxTestCase):
    start_bridge = False

    def _install_slow_shim(self, name: str) -> None:
        shim_dir = self.tmp / "slow-shims"
        shim_dir.mkdir(exist_ok=True)
        shim = shim_dir / name
        shim.write_text("#!/bin/sh\nexec sleep 5\n")
        shim.chmod(0o755)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"

    def test_hung_pgrep_is_bounded_and_unknown(self):
        """Plan §6.1 / budget table L712 (gap subprocess-no-timeout): a hung pgrep cannot stall the hot path."""
        hot_path_budget()
        self._install_slow_shim("pgrep")
        t0 = time.monotonic()
        self.assertIsNone(process.get_bartender_pid())
        self.assertIsNone(process.get_herdr_pid())
        self.assertIsNone(process.herdr_liveness(), "a timed-out probe is unknown, not dead")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_hung_ps_gives_unknown_start_time(self):
        """Plan §6.1 (gaps subprocess-no-timeout, start-time-fallback-false-restart): a hung ps is None, quickly."""
        hot_path_budget()
        self._install_slow_shim("ps")
        t0 = time.monotonic()
        self.assertIsNone(process.get_process_start_time(os.getpid()))
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_cli_with_hung_pgrep_exits_within_budget(self):
        """Plan §6.1 1.5s guarantee (gap subprocess-no-timeout): `--health` still exits within 1.5s."""
        self._install_slow_shim("pgrep")
        t0 = time.monotonic()
        res = self.run_cli("--health")
        self.assertLess(time.monotonic() - t0, 1.5 + 0.5, res.stderr)  # + interpreter start-up slack
        self.assertIn("unreachable", res.stdout)


class DeadlineModeTests(SandboxTestCase):
    """Plan §6.1: the 1.5s budget formula binds only the watchdog-bounded event path."""

    start_bridge = False

    def _install_real_speed_shims(self) -> None:
        """pgrep/ps shims that take ~50ms per call, like the real tools on a busy machine."""
        shim_dir = self.tmp / "real-speed-shims"
        shim_dir.mkdir(exist_ok=True)
        for name in ("pgrep", "ps"):
            shim = shim_dir / name
            shim.write_text(f'#!/bin/sh\nsleep 0.05\nexec "{SHIM_DIR / name}" "$@"\n')
            shim.chmod(0o755)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"

    def _arm_test_itimer(self) -> None:
        """Arm ITIMER_REAL far in the future with a no-op handler, as arm_watchdog() does."""
        previous = signal.signal(signal.SIGALRM, lambda *_: None)
        signal.setitimer(signal.ITIMER_REAL, 30.0)

        def disarm():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.addCleanup(disarm)

    def test_long_running_process_resolves_bartender_with_real_speed_pgrep(self):
        """Plan §6.1 / §6 items 8-9 (gap subprocess-no-timeout): an hour into an unbounded process,
        a 50ms pgrep still resolves the Bartender PID and its start time (no 20ms probe cap)."""
        long_running_process()
        self._install_real_speed_shims()
        self.add_fake_process("Bartender 6", pid=900, lstart="Sat Oct  4 07:00:00 2026")
        self.assertFalse(runtime.deadline_bounded())
        self.assertEqual(process.probe_timeout(), process.RELAXED_PROBE_TIMEOUT)
        self.assertEqual(process.get_bartender_pid(), 900)
        self.assertIsNotNone(process.get_process_start_time(900))

    def test_armed_watchdog_keeps_the_hot_path_formula(self):
        """Plan §6.1 (gap subprocess-no-timeout): with the SIGALRM watchdog armed the probe budget
        is still bounded by time_remaining() - 0.3, even when the clock says the budget is spent."""
        long_running_process()
        self._arm_test_itimer()
        self.assertTrue(runtime.deadline_bounded())
        self.assertEqual(process.probe_timeout(), process.MIN_PROBE_TIMEOUT)

    def test_hot_path_probe_cap_scales_with_remaining_budget(self):
        """Plan §6.1 budget table (gap subprocess-no-timeout): each hot-path probe gets
        min(time_remaining() - 0.3, HOT_PATH_PROBE_TIMEOUT), not a fixed 0.1s."""
        hot_path_budget()
        self.assertGreaterEqual(process.HOT_PATH_PROBE_TIMEOUT, 0.2)
        self.assertAlmostEqual(process.probe_timeout(), process.HOT_PATH_PROBE_TIMEOUT, delta=0.01)
        runtime.START_TIME = time.monotonic() - (runtime.DEFAULT_DEADLINE_SECONDS - 0.45)  # ~0.45s left
        self.assertLess(process.probe_timeout(), process.HOT_PATH_PROBE_TIMEOUT)
        self.assertGreater(process.probe_timeout(), process.MIN_PROBE_TIMEOUT)

    def test_startup_window_before_the_watchdog_is_bounded(self):
        """Plan §6.1 (gap subprocess-no-timeout): before arm_watchdog() (identity warm-up) an
        inferred-mode process still honours the 1.5s budget."""
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = time.monotonic()
        runtime.set_deadline_mode(None)
        self.assertTrue(runtime.deadline_bounded())
        self.assertLessEqual(process.probe_timeout(), process.HOT_PATH_PROBE_TIMEOUT)

    def test_explicit_modes_override_inference(self):
        """Plan §6.1 / L619, L686 (gap subprocess-no-timeout): unbounded modes can declare themselves."""
        hot_path_budget()
        runtime.set_deadline_mode(runtime.DEADLINE_UNBOUNDED)
        self.assertFalse(runtime.deadline_bounded())
        self.assertTrue(runtime.budget_allows(0.3))
        long_running_process()
        runtime.set_deadline_mode(runtime.DEADLINE_BOUNDED)
        self.assertTrue(runtime.deadline_bounded())
        self.assertFalse(runtime.budget_allows(0.3))
        with self.assertRaises(ValueError):
            runtime.set_deadline_mode("sometimes")


class HerdrLivenessTests(SandboxTestCase):
    start_bridge = False
    default_liveness = False  # this class drives the shim process table itself

    def test_herdr_dead_is_observable_through_shims(self):
        """§10.1 #53/#60 (gap portability-guard-herdr-liveness): with the shim table empty Herdr is dead."""
        self.assertIs(process.herdr_liveness(), False)
        self.assertFalse(process.is_herdr_alive())

    def test_herdr_alive_via_fake_process(self):
        """R15 (gap portability-guard-herdr-liveness): a fake `herdr` process makes is_herdr_alive() true."""
        self.set_herdr_alive()
        self.assertIs(process.herdr_liveness(), True)
        self.assertTrue(process.is_herdr_alive())

    def test_unknown_herdr_liveness_is_treated_as_alive(self):
        """R14 spirit (gap subprocess-no-timeout): a failing pgrep must not look like a Herdr death."""
        (self.sandbox / "pgrep.fail").write_text("")
        self.assertIsNone(process.herdr_liveness())
        self.assertTrue(process.is_herdr_alive())


class OrphanLockModeTests(SandboxTestCase):
    start_bridge = False

    def _hold_lock(self, orphan_path):
        fd = os.open(str(orphan_path.with_name(orphan_path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _release(self, fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def test_event_path_lock_is_nonblocking_50ms(self):
        """R10 / Plan §6.1 L703 (gap orphan-lock-blocking): a contended orphan lock gives up after 50ms."""
        orphan_path = self.home / "orphans.json"
        existing = json.dumps({"version": 1, "sessions": {self.sid("w1:pOld"): {"agent": "Old"}}})
        orphan_path.write_text(existing)
        fd = self._hold_lock(orphan_path)
        self.addCleanup(self._release, fd)
        t0 = time.monotonic()
        self.assertIs(export_orphan_record(self.sid("w1:pA"), {"agent": "A"}, orphan_file=orphan_path), False)
        self.assertIs(remove_orphan_record(self.sid("w1:pOld"), orphan_file=orphan_path), False)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(orphan_path.read_text(), existing, "a contended lock leaves the file untouched")

    def test_nonblocking_deadline_uses_injected_clock(self):
        """R10 (gap orphan-lock-blocking): the 50ms retry loop is driven by the injectable clock."""
        fake = self.use_fake_clock()
        orphan_path = self.home / "orphans.json"
        orphan_path.write_text(json.dumps({"version": 1, "sessions": {}}))
        fd = self._hold_lock(orphan_path)
        self.addCleanup(self._release, fd)
        self.assertIs(export_orphan_record(self.sid("w1:pA"), {"agent": "A"}, orphan_file=orphan_path), False)
        self.assertTrue(fake.sleeps, "the retry loop sleeps through clock.sleep")
        self.assertLessEqual(sum(fake.sleeps), 0.06)

    def test_blocking_mode_waits_for_the_lock(self):
        """R10 (gap orphan-lock-blocking): --replay-orphans/--cleanup may block until the lock is free."""
        orphan_path = self.home / "orphans.json"
        fd = self._hold_lock(orphan_path)
        releaser = threading.Timer(0.2, self._release, args=(fd,))
        releaser.start()
        self.addCleanup(releaser.join)
        sid = self.sid("w1:pB")
        self.assertIs(export_orphan_record(sid, {"agent": "B"}, orphan_file=orphan_path, blocking=True), True)
        self.assertIn(sid, json.loads(orphan_path.read_text())["sessions"])
        self.assertEqual(orphan_path.stat().st_mode & 0o777, 0o600)

    def _pending_entries(self, orphan_path):
        pending = orphans.pending_dir_for(orphan_path)
        return sorted(p.name for p in pending.glob("*.json")) if pending.exists() else []

    def test_contended_export_is_journaled_and_applied_later(self):
        """R10 / Plan §6.1 L703 (gap orphan-lock-blocking): a contended export is not lost; it is
        journaled durably, the reconciler is flagged, and the next lock holder persists it."""
        orphan_path = self.home / "orphans.json"
        fd = self._hold_lock(orphan_path)
        sid = self.sid("w1:pA")
        self.assertIs(export_orphan_record(sid, {"agent": "A"}, orphan_file=orphan_path), False)
        self.assertFalse(orphan_path.exists())
        self.assertEqual(len(self._pending_entries(orphan_path)), 1)
        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "work is left to the reconciler")
        pending_dir = orphans.pending_dir_for(orphan_path)
        self.assertEqual(stat_mode(pending_dir), 0o700)
        for entry in pending_dir.iterdir():
            self.assertEqual(stat_mode(entry), 0o600)
        self._release(fd)
        self.assertIs(flush_pending_orphan_ops(orphan_file=orphan_path), True)
        self.assertEqual(json.loads(orphan_path.read_text())["sessions"], {sid: {"agent": "A"}})
        self.assertEqual(self._pending_entries(orphan_path), [])

    def test_next_uncontended_op_applies_journal_first_in_order(self):
        """R10 (gap orphan-lock-blocking): journaled ops are replayed in order before the caller's op,
        so a later removal is never undone by an earlier contended export."""
        orphan_path = self.home / "orphans.json"
        a, b = self.sid("w1:pA"), self.sid("w1:pB")
        fd = self._hold_lock(orphan_path)
        self.assertIs(export_orphan_record(a, {"agent": "A"}, orphan_file=orphan_path), False)
        self.assertIs(export_orphan_record(b, {"agent": "B"}, orphan_file=orphan_path), False)
        self._release(fd)
        self.assertIs(remove_orphan_record(a, orphan_file=orphan_path), True)
        self.assertEqual(json.loads(orphan_path.read_text())["sessions"], {b: {"agent": "B"}})
        self.assertEqual(self._pending_entries(orphan_path), [])

    def test_contended_remove_is_journaled(self):
        """R10 (gap orphan-lock-blocking): a contended removal is replayed later, not dropped."""
        orphan_path = self.home / "orphans.json"
        a = self.sid("w1:pA")
        self.assertIs(export_orphan_record(a, {"agent": "A"}, orphan_file=orphan_path), True)
        fd = self._hold_lock(orphan_path)
        self.assertIs(remove_orphan_record(a, orphan_file=orphan_path), False)
        self._release(fd)
        self.assertIs(flush_pending_orphan_ops(orphan_file=orphan_path), True)
        self.assertFalse(orphan_path.exists())

    def test_corrupt_journal_entry_is_dropped_and_logged(self):
        """R10 (gap orphan-lock-blocking): an unreadable journal entry cannot wedge the orphan file."""
        orphan_path = self.home / "orphans.json"
        pending = orphans.pending_dir_for(orphan_path)
        pending.mkdir(mode=0o700)
        (pending / "00000000000000000001-1-0.json").write_text("{not json")
        a = self.sid("w1:pA")
        self.assertIs(export_orphan_record(a, {"agent": "A"}, orphan_file=orphan_path), True)
        self.assertEqual(json.loads(orphan_path.read_text())["sessions"], {a: {"agent": "A"}})
        self.assertEqual(self._pending_entries(orphan_path), [])
        self.assertIn("orphan journal", (self.state_dir / "plugin.log").read_text())

    def test_uncontended_export_and_remove(self):
        """Plan §3.3 orphan mirror (mode 0600) (gap orphan-lock-blocking): the fast path still works."""
        orphan_path = self.home / "orphans.json"
        a, b = self.sid("w1:pA"), self.sid("w1:pB")
        self.assertIs(export_orphan_record(a, {"agent": "A"}, orphan_file=orphan_path), True)
        self.assertIs(export_orphan_record(b, {"agent": "B"}, orphan_file=orphan_path), True)
        self.assertEqual(orphan_path.stat().st_mode & 0o777, 0o600)
        lock_path = orphan_path.with_name(orphan_path.name + ".lock")
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        self.assertIs(remove_orphan_record(a, orphan_file=orphan_path), True)
        self.assertEqual(list(json.loads(orphan_path.read_text())["sessions"]), [b])
        self.assertIs(remove_orphan_record(b, orphan_file=orphan_path), True)
        self.assertFalse(orphan_path.exists())


if __name__ == "__main__":
    unittest.main()


class HostnameAndWarningLogTests(SandboxTestCase):
    start_bridge = False

    def test_config_hostname_truncated_to_32(self):
        """Plan §2.3 session_id row (gap hostname-truncation): config.get_sanitized_hostname() caps the host at 32."""
        from unittest import mock
        from herdr_bartender import config
        with mock.patch("socket.gethostname", return_value="H" * 60 + ".example.com"):
            self.assertEqual(config.get_sanitized_hostname(), "h" * 32)
        with mock.patch("socket.gethostname", return_value="!!!.lan"):
            self.assertEqual(config.get_sanitized_hostname(), "local")

    def test_log_warning_is_tagged_and_private(self):
        """Plan §8 L1089 (gap reconciler-allowlist-fails-open handoff): log_warning writes a WARNING line, 0600."""
        from herdr_bartender import log
        log.log_warning("allowlist missing")
        log_file = self.state_dir / log.LOG_FILE_NAME
        self.assertRegex(log_file.read_text(), r"\] WARNING: allowlist missing\n$")
        self.assertEqual(stat_mode(log_file), 0o600)
