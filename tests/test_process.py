"""Process discovery and liveness (pgrep/ps are PATH shims inside the sandbox)."""

import time
import unittest
from pathlib import Path

from herdr_bartender import process
from herdr_bartender.process import get_bartender_pid, get_herdr_pid
from tests.support import SandboxTestCase

LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def epoch_of(lstart: str) -> str:
    return str(int(time.mktime(time.strptime(" ".join(lstart.split()), LSTART_FMT))))


class HerdrPidSelectionTests(SandboxTestCase):
    """Plan §3.4 Herdr Process Model / §1 L133: GUI bundle first, then earliest start time."""

    start_bridge = False
    default_liveness = False  # this class drives the shim process table itself

    def test_p53_earliest_start_herdr_pid(self):
        """Plan §10.1 #53 (gap t53-herdr-pid): with several CLI-only `herdr` PIDs the earliest start wins.

        PID 200 started first even though it is neither the lowest nor the highest PID.
        """
        self.add_fake_process("herdr", pid=300, lstart="Sat Oct  4 09:00:00 2026", comm="herdr")
        self.add_fake_process("herdr", pid=200, lstart="Sat Oct  4 08:00:00 2026", comm="herdr")
        self.add_fake_process("herdr", pid=100, lstart="Sat Oct  4 10:00:00 2026", comm="herdr")
        self.assertEqual(get_herdr_pid(), 200)

    def test_p53_gui_bundle_preferred_over_earlier_cli(self):
        """Plan §10.1 #53 / §3.4 (gap t53-herdr-pid): a `.app` bundle PID beats an earlier CLI helper PID."""
        self.add_fake_process("herdr", pid=200, lstart="Sat Oct  4 08:00:00 2026", comm="herdr")
        self.add_fake_process("herdr", pid=300, lstart="Sat Oct  4 09:00:00 2026",
                              comm="/Applications/Herdr.app/Contents/MacOS/herdr")
        self.assertEqual(get_herdr_pid(), 300)

    def test_p53_no_herdr_or_pgrep_failure_is_none(self):
        """Plan §10.1 #53 (gap t53-herdr-pid): no match, or a failing pgrep (exit 2), yields None."""
        self.assertIsNone(get_herdr_pid())
        self.add_fake_process("herdr", pid=200, lstart="Sat Oct  4 08:00:00 2026", comm="herdr")
        (self.sandbox / "pgrep.fail").write_text("")
        self.assertIsNone(get_herdr_pid())

    def test_p53_unparseable_start_time_ranks_last(self):
        """Plan §10.1 #53 (gaps t53-herdr-pid, start-time-fallback-false-restart): unknown start times
        never win over a known one, and never stand in for this process's own load time."""
        self.add_fake_process("herdr", pid=100, lstart="not-a-date", comm="herdr")
        self.add_fake_process("herdr", pid=300, lstart="Sat Oct  4 09:00:00 2026", comm="herdr")
        self.assertEqual(get_herdr_pid(), 300)

    def test_p53_all_unknown_start_times_pick_lowest_pid(self):
        """Plan §10.1 #53 (gap t53-herdr-pid): when no start time is known the choice is still deterministic."""
        self.add_fake_process("herdr", pid=310, lstart="garbage", comm="herdr")
        self.add_fake_process("herdr", pid=110, lstart="garbage", comm="herdr")
        self.assertEqual(get_herdr_pid(), 110)

    def test_r15_app_bundle_path_matches_without_process_name(self):
        """R15 (gap portability-guard-herdr-liveness): `pgrep -f '/Herdr.app/Contents/MacOS/'` finds the GUI app."""
        app = "/Applications/Herdr.app/Contents/MacOS/Herdr Main"
        pid = self.add_fake_process("Herdr Main", pid=4321, comm=app, cmdline=app)
        self.assertEqual(get_herdr_pid(), pid)

    def test_r15_loose_herdr_app_substring_does_not_match(self):
        """R15 (gap portability-guard-herdr-liveness): a cmdline merely mentioning `Herdr.app` is not Herdr."""
        self.add_fake_process("vim", pid=4322, cmdline="vim notes/Herdr.app.txt")
        self.assertIsNone(get_herdr_pid())

    def test_r15_bundle_path_as_argument_is_not_herdr(self):
        """R15 (gap portability-guard-herdr-liveness): the bundle path must be the executable (argv[0]),
        as in the guard's anchored `^[^[:space:]]*/Herdr\\.app/Contents/MacOS/`; a process that merely
        takes the bundle path as an argument is not Herdr (and must not feed the instance id)."""
        self.add_fake_process("vim", pid=4322, cmdline="vim /Applications/Herdr.app/Contents/MacOS/notes")
        self.assertIsNone(get_herdr_pid())
        self.assertIs(process.herdr_liveness(), False)
        self.assertEqual(process.get_herdr_instance_id(), "")

    def test_r15_python_pattern_matches_guard_pattern(self):
        """R15 (gap portability-guard-herdr-liveness): Python and hook_guard.sh share one bundle rule."""
        guard = (Path(process.__file__).parent / "hook_guard.sh").read_text(encoding="utf-8")
        needle = f"pgrep -f '{process.HERDR_APP_PATTERN}'"
        self.assertTrue(needle in guard, f"hook_guard.sh does not use {needle!r}")


class BartenderPidSelectionTests(SandboxTestCase):
    """Plan §1 L134 / §3.1 Restart Detection: Bartender PID is the earliest-started process."""

    start_bridge = False
    default_liveness = False  # this class drives the shim process table itself

    def test_bartender_pid_is_earliest_start_not_lowest_pid(self):
        """Plan §3.1 L213 (gaps bartender-pid-earliest, bartender-earliest-start-broken):
        a higher PID with an earlier start time wins."""
        self.add_fake_process("Bartender 6", pid=500, lstart="Sat Oct  4 09:00:00 2026")
        self.add_fake_process("Bartender 6", pid=900, lstart="Sat Oct  4 07:00:00 2026")
        self.assertEqual(get_bartender_pid(), 900)

    def test_bartender_falls_back_to_legacy_name(self):
        """Plan §1 L134 (gap bartender-pid-earliest): `pgrep -x Bartender` is tried when `Bartender 6` is absent."""
        self.add_fake_process("Bartender", pid=777, lstart="Sat Oct  4 07:00:00 2026")
        self.assertEqual(get_bartender_pid(), 777)

    def test_bartender_absent_is_none(self):
        """Plan §8 liveness gate (gap bartender-pid-earliest): no Bartender process gives None."""
        self.assertIsNone(get_bartender_pid())
        self.assertEqual(epoch_of("Sat Oct  4 07:00:00 2026"), epoch_of("Sat Oct 4 07:00:00 2026"))


if __name__ == "__main__":
    unittest.main()
