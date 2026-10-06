"""npx adversarial-review gate round 39: a deadline passed during an exception still hands off (R87); with no FIFO
possible the hook guard still delivers every captured byte, under bash and dash (R87)."""

import os
import shutil
import time
import unittest
from unittest import mock

from herdr_bartender import runtime, watchdog
from tests.support import SandboxTestCase, run_guard
from tests.support.guard_harness import leftovers, make_shim, path_with, start_fifo_writer
from tests.test_hooks_guard import _GuardCase


class DeferredExitDuringExceptionTests(SandboxTestCase):
    def test_hand_off_happens_and_the_exception_propagates(self):
        self.addCleanup(setattr, runtime, "PENDING_WATCHDOG_EXIT", False)
        with mock.patch.object(watchdog, "hand_off_to_reconciler") as hand_off:
            with self.assertRaises(ValueError):
                with watchdog.deferred_exit():
                    runtime.PENDING_WATCHDOG_EXIT = True
                    raise ValueError("unexpected Step A failure")
        hand_off.assert_called_once()
        self.assertFalse(runtime.IN_DEFER_SECTION)


class NoFifoSpliceTests(_GuardCase):
    def shells(self):
        shells = [("bash", "#!/bin/bash\nset -u")]
        dash = shutil.which("dash")
        if dash:
            shells.append(("dash", f"#!{dash}\nset -u"))
        return shells

    def test_prefix_and_remainder_survive_without_mkfifo(self):
        line1 = '{"session_id":"1234567890123456","hook_event_name":"UserPromptSubmit"}\n'
        line2 = "remainder-line-after-deadline\n"
        make_shim(self.shim_bin, "mkfifo", "exit 1")   # inode/disk exhaustion: no FIFO can be created
        for shell, shebang in self.shells():
            with self.subTest(shell=shell):
                pane = f"w1:pNoFifo{shell}"
                self.fresh_marker(pane)
                script = self.script(f"guard-nofifo-{shell}.sh", 'printf "BODY:"; cat', shebang=shebang)
                fifo = self.tmp / f"nofifo_{shell}"
                os.mkfifo(str(fifo))
                start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; printf '%s' '{line1}' >&3; sleep 1.5; "
                                              f"printf '%s' '{line2}' >&3; exec 3>&-")
                with open(str(fifo), "rb") as reader:
                    res = run_guard(script, stdin=reader,
                                    env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, "BODY:" + line1 + line2, "every captured byte reaches the vendor once")
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])


if __name__ == "__main__":
    unittest.main()
