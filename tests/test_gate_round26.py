"""npx adversarial-review gate round 26: the shell guard applies R63/R70's .vendor_active rules too (R74)."""

import json
import os
import unittest

from tests.support import run_guard
from tests.support.guard_harness import make_shim, path_with
from tests.test_hooks_guard import _GuardCase

SID = "c91f0443-1e47-54f7-9c13-2c064cbee24f"


class GuardVendorActiveHardeningTests(_GuardCase):
    def setUp(self):
        super().setUp()
        self.set_herdr_dead()   # the guard falls through and records .vendor_active
        self.script_path = self.script("guard-va.sh", 'echo "PASSTHROUGH"')

    def run_event(self, pane, *args):
        res = run_guard(self.script_path, *args, env_extra={"HERDR_PANE_ID": pane})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH", res.stdout)
        return res

    def test_symlinked_vendor_active_is_never_followed(self):
        pane = "w1:pVaLink"
        _, va = self.paths(pane)
        target = self.tmp / "crafted.json"
        target.write_text(json.dumps({"vendor_session_id": SID}))
        os.utime(target, (1_000_000, 1_000_000))
        va.symlink_to(target)
        self.run_event(pane, "Working")
        self.assertEqual(int(target.stat().st_mtime), 1_000_000, "the target is never touched")
        self.assertEqual(json.loads(target.read_text()), {"vendor_session_id": SID})
        self.assertFalse(va.is_symlink(), "the symlink is replaced by the guard's own regular record")

    def test_oversized_vendor_active_is_replaced_not_scanned(self):
        pane = "w1:pVaHuge"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}) + " " * 8192)
        self.run_event(pane, "Working")
        self.assertLessEqual(va.stat().st_size, 4096, "the oversized file was dropped and a fresh record written")

    def test_directory_at_vendor_active_blocks_writes(self):
        pane = "w1:pVaDir"
        _, va = self.paths(pane)
        va.mkdir()
        self.run_event(pane, json.dumps({"session_id": SID, "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(list(va.iterdir()), [], "nothing is moved into a directory planted at the path")


class GuardSingleBoundedReadTests(_GuardCase):
    def test_vendor_active_is_read_once_bounded_without_wc(self):
        """R75: the size check and the UUID check use ONE bounded no-follow read (perl/python3), never a whole-file
        `wc -c` scan followed by a second, separately raced `grep` of the path."""
        self.set_herdr_dead()
        script = self.script("guard-va-read.sh", 'echo "PASSTHROUGH"')
        log = self.tmp / "wc.log"
        make_shim(self.shim_bin, "wc", f'printf "%s\\n" "$*" >> "{log}"\nexec /usr/bin/env -i PATH=/usr/bin:/bin wc "$@"')
        pane = "w1:pVaOnce"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}))
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertFalse(log.exists() and log.read_text().strip(), "no wc scan of .vendor_active")
        self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": SID}, "the UUID record is still honoured")


class GuardRetireRaceTests(_GuardCase):
    def test_record_created_after_the_snapshot_is_never_deleted(self):
        """R76: retiring a bare touch claims it first and puts back a record another hook wrote after our read."""
        import shutil
        pane = "w1:pVaRace"
        self.fresh_marker(pane)                       # Herdr healthy: a non-terminal event retires the bare touch
        _, va = self.paths(pane)
        va.write_text("")                             # the bare touch our snapshot sees
        real_mktemp = shutil.which("mktemp")
        record = json.dumps({"vendor_session_id": SID})
        make_shim(self.shim_bin, "mktemp",
                  'case "$*" in *.va.claim.*) printf %s \'' + record + '\' > "' + str(va) + '" ;; esac\n'
                  f'exec "{real_mktemp}" "$@"')
        script = self.script("guard-va-race.sh", 'echo "PASSTHROUGH"')
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(va.exists(), "the concurrently written UUID record survives")
        self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": SID})
        self.assertEqual(list(va.parent.glob(".va.claim.*")), [], "no claim file is left behind")


class GuardTerminalAndRefreshRaceTests(_GuardCase):
    def test_session_end_never_deletes_a_newer_record(self):
        """R79: SessionEnd retires (by claim) only the record the guard read; a newer one written meanwhile stays."""
        import shutil
        self.set_herdr_dead()
        pane = "w1:pVaEnd"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": "a" * 36}))
        newer = json.dumps({"vendor_session_id": SID})
        real_mktemp = shutil.which("mktemp")
        make_shim(self.shim_bin, "mktemp",
                  'case "$*" in *.va.claim.*) printf %s \'' + newer + '\' > "' + str(va) + '" ;; esac\n'
                  f'exec "{real_mktemp}" "$@"')
        script = self.script("guard-va-end.sh", 'echo "PASSTHROUGH"')
        res = run_guard(script, "SessionEnd", env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": SID}, "the newer record survives")

    def test_session_end_retires_the_record_it_read(self):
        self.set_herdr_dead()
        pane = "w1:pVaEnd2"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}))
        script = self.script("guard-va-end2.sh", 'echo "PASSTHROUGH"')
        run_guard(script, "SessionEnd", env_extra={"HERDR_PANE_ID": pane})
        self.assertFalse(os.path.lexists(va))
        self.assertEqual(list(va.parent.glob(".va.claim.*")), [])

    def test_refresh_never_follows_a_symlink_swapped_in_late(self):
        """R79: the closing refresh goes through a no-follow fd, so a symlink swapped in after every check cannot
        redirect it to another file."""
        import shutil
        self.set_herdr_dead()
        pane = "w1:pVaTouch"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}))
        victim = self.tmp / "victim.txt"
        victim.write_text("x")
        os.utime(victim, (1_000_000, 1_000_000))
        real_perl = shutil.which("perl")
        if real_perl is None:
            self.skipTest("perl not installed")
        swap = f'rm -f "{va}"; ln -s "{victim}" "{va}"'
        make_shim(self.shim_bin, "perl", f'case "$*" in *utime*) {swap} ;; esac\nexec "{real_perl}" "$@"')
        real_touch = shutil.which("touch")
        make_shim(self.shim_bin, "touch", f'case "$*" in *vendor_active*) {swap} ;; esac\nexec "{real_touch}" "$@"')
        script = self.script("guard-va-touch.sh", 'echo "PASSTHROUGH"')
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertEqual(int(victim.stat().st_mtime), 1_000_000, "the swapped-in symlink's target is untouched")


if __name__ == "__main__":
    unittest.main()
