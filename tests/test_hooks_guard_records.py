"""The guard's ``.vendor_active`` records agree with Python's reading of them (round-4 low findings).

* The vendor ``session_id`` was validated with a line-based ``grep``, so a JSON string carrying a raw newline
  (``"<16+ valid chars>\\n<anything>"``) passed on its first line and the guard wrote
  ``{"vendor_session_id":"...\\n..."}``: corrupt JSON that Python reads as a bare touch while the guard treats
  it as a UUID record.
* The post-pass-through refresh was a plain ``touch`` after a ``[ -f ]`` test: when Python retired the file in
  between (claim + unlink under the cache lock) the touch re-created it as a bare touch. ``touch -c`` only
  refreshes.
"""

import json
import os
import unittest

from tests.support import run_guard
from tests.support.guard_harness import make_shim, path_with
from tests.test_hooks_guard import _GuardCase

VALID_PREFIX = "abcdefghijklmnopqrstuv"   # 22 chars: a valid UUID on its own
MULTI_LINE_SID = VALID_PREFIX + "\nEVIL"
SID = "0123456789abcdef0123"


class MultiLineSessionIdTests(_GuardCase):
    def _payload(self, sid, event="UserPromptSubmit"):
        # A raw newline inside the string (not the escaped \\n json.dumps would write).
        return '{"session_id":"' + sid + '", "hook_event_name":"' + event + '"}'

    def _run(self, pane, payload, mode):
        script = self.script("guard-sid.sh", 'echo "PASSTHROUGH"')
        env = {"HERDR_PANE_ID": pane}
        if mode == "argv-json":
            return run_guard(script, payload, env_extra=env)
        return run_guard(script, input=payload, env_extra=env)

    def test_multi_line_session_id_is_never_recorded(self):
        """Herdr down, no .vendor_active yet: the fall-through records a bare touch, never the corrupt record."""
        self.set_herdr_dead()
        for mode in ("stdin-json", "argv-json"):
            with self.subTest(mode=mode):
                pane = f"w1:pNl{mode.split('-')[0]}"
                _, va = self.paths(pane)
                res = self._run(pane, self._payload(MULTI_LINE_SID), mode)
                self.assertIn("PASSTHROUGH", res.stdout, res.stderr)
                self.assertTrue(va.exists(), "the fall-through is still tracked")
                self.assertEqual(va.read_text(), "", "an invalid session id leaves a bare touch")

    def test_multi_line_session_id_never_upgrades_a_bare_touch(self):
        self.set_herdr_dead()
        for mode in ("stdin-json", "argv-json"):
            with self.subTest(mode=mode):
                pane = f"w1:pNlUp{mode.split('-')[0]}"
                _, va = self.paths(pane)
                va.write_text("")
                self._run(pane, self._payload(MULTI_LINE_SID), mode)
                self.assertEqual(va.read_text(), "")

    def test_single_line_session_id_is_still_recorded(self):
        """Control: the same payload with a valid id records it (so the rejection above is the newline alone)."""
        self.set_herdr_dead()
        for mode in ("stdin-json", "argv-json"):
            with self.subTest(mode=mode):
                pane = f"w1:pOk{mode.split('-')[0]}"
                _, va = self.paths(pane)
                self._run(pane, self._payload(VALID_PREFIX), mode)
                self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": VALID_PREFIX})


class RefreshNeverRecreatesTests(_GuardCase):
    def test_refresh_does_not_recreate_a_vendor_active_retired_meanwhile(self):
        """The refresh's own process first unlinks the file (Python retiring it right after the guard's checks), then
        runs the real refresh: the file must stay retired (R79: a no-follow fd open never creates)."""
        bin_dir = self.tmp / "retire-bin"
        retire = 'for target in "$@"; do :; done\nrm -f "$target"\n'
        make_shim(bin_dir, "touch", retire + 'PATH="$HB_REAL_PATH" exec touch "$@"')
        make_shim(bin_dir, "perl", 'case "$*" in *utime*) ' + retire.replace("\n", "; ") + ';; esac\n'
                  'PATH="$HB_REAL_PATH" exec perl "$@"')
        script = self.script("guard-refresh.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pRetired"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}, separators=(",", ":")))
        self.set_herdr_dead()   # unhealthy: the UUID record is kept and the event passes through
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(bin_dir)})
        self.assertIn("PASSTHROUGH", res.stdout, res.stderr)
        self.assertFalse(va.exists(), "the refresh must not re-create a retired .vendor_active")

    def test_refresh_still_updates_an_existing_record(self):
        """Control (Plan §10.1 #45 guard side): without the race the pass-through still refreshes the mtime."""
        script = self.script("guard-refresh-ok.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pRefreshed"
        _, va = self.paths(pane)
        va.write_text("")
        aged = os.stat(va).st_mtime - 120
        os.utime(va, (aged, aged))
        self.set_herdr_dead()
        run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane})
        self.assertGreater(os.stat(va).st_mtime, aged + 60)


if __name__ == "__main__":
    unittest.main()
