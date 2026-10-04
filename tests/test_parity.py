"""Python vs Bash parity: canonical pane IDs, hex encoding and STATE_DIR resolution."""

import os
import subprocess
import unittest
from pathlib import Path

from herdr_bartender.paths import get_state_dir
from herdr_bartender.sanitize import get_hex_pane_id, normalize_pane_id
from tests.support import SandboxTestCase

CANON_BASH = '''
HERDR_PANE_ID="{raw}"
HERDR_WORKSPACE_ID="{ws}"
CANONICAL_PANE=""
if printf '%s' "$HERDR_PANE_ID" | grep -q ':'; then
  CANONICAL_PANE="$HERDR_PANE_ID"
elif [ -n "${{HERDR_WORKSPACE_ID:-}}" ]; then
  CANONICAL_PANE="${{HERDR_WORKSPACE_ID}}:${{HERDR_PANE_ID}}"
else
  CANONICAL_PANE=""
fi
'''


class ParityTests(SandboxTestCase):
    start_bridge = False

    # WEAK: t31-37-parity-dup
    def test_p31_hex_encoding_parity(self):
        """Plan §10.1 #31: Python hex of the canonical pane equals bash `od -An -tx1 | tr -d ' \\t\\n'`."""
        test_cases = [
            ("pane123", None, ""),
            ("pane123", "wsA", "wsA:pane123"),
            ("wsB:pane456", None, "wsB:pane456"),
            ("wsB:pane456", "wsC", "wsB:pane456"),
        ]
        for raw_p, ws_env, expected_canon in test_cases:
            with self.subTest(raw=raw_p, ws=ws_env):
                py_canon = normalize_pane_id(raw_p, ws_env)
                self.assertEqual(py_canon, expected_canon, f"Python canonical mismatch: {py_canon} != {expected_canon}")
                py_hex = get_hex_pane_id(py_canon)
                bash_cmd = CANON_BASH.format(raw=raw_p, ws=ws_env or "") + \
                    "printf '%s' \"$CANONICAL_PANE\" | od -An -tx1 | tr -d ' \\t\\n'\n"
                res_bash = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
                self.assertEqual(res_bash.stdout.strip(), py_hex, f"Bash vs Python hex mismatch for {raw_p}: {res_bash.stdout.strip()} != {py_hex}")

    # WEAK: t31-37-parity-dup
    def test_p37_canonical_parity_fixture_table(self):
        """Plan §10.1 #37: Python and Bash derive identical canonical pane IDs across the fixture table."""
        fixture_cases = [
            ("p1", "w1", "w1:p1"),
            ("w2:p2", "w1", "w2:p2"),
            ("p3", "", ""),
            ("custom_name-4", "ws_alpha", "ws_alpha:custom_name-4"),
            ("w3:p5:sub", "w4", "w3:p5:sub"),
        ]
        for raw_p, ws_p, expected_canonical in fixture_cases:
            with self.subTest(raw=raw_p, ws=ws_p):
                py_canon = normalize_pane_id(raw_p, ws_p or None)
                self.assertEqual(py_canon, expected_canonical, f"Python canonical mismatch: {py_canon} != {expected_canonical}")
                bash_cmd = CANON_BASH.format(raw=raw_p, ws=ws_p) + "printf '%s' \"$CANONICAL_PANE\"\n"
                res_b = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
                self.assertEqual(res_b.stdout.strip(), expected_canonical, f"Bash canonical mismatch: {res_b.stdout.strip()} != {expected_canonical}")
                self.assertEqual(py_canon, res_b.stdout.strip(), "Python and Bash must derive identical canonical pane ID")

    def test_p66_state_dir_resolution_parity(self):
        """Plan §10.1 #66: get_state_dir() matches the bash ${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-...}} rule."""
        norm_bash_cmd = 'printf "%s" "${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/herdr-bartender}"'
        # Sandbox: temp paths instead of the original /tmp literals.
        custom_state = str(self.tmp / "test_herdr_plugin_state")
        custom_xdg = str(self.tmp / "test_xdg_home")

        os.environ["HERDR_PLUGIN_STATE_DIR"] = custom_state
        os.environ.pop("XDG_STATE_HOME", None)
        py_dir_1 = str(get_state_dir())
        bash_dir_1 = subprocess.check_output(["bash", "-c", norm_bash_cmd], text=True)
        self.assertTrue(py_dir_1 == bash_dir_1 == custom_state, f"Case 1 mismatch: py={py_dir_1}, bash={bash_dir_1}")

        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ["XDG_STATE_HOME"] = custom_xdg
        py_dir_2 = str(get_state_dir())
        bash_dir_2 = subprocess.check_output(["bash", "-c", norm_bash_cmd], text=True)
        expected_2 = f"{custom_xdg}/herdr/plugins/herdr-bartender"
        self.assertTrue(py_dir_2 == bash_dir_2 == expected_2, f"Case 2 mismatch: py={py_dir_2}, bash={bash_dir_2}")

        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ.pop("XDG_STATE_HOME", None)
        py_dir_3 = str(get_state_dir())
        bash_dir_3 = subprocess.check_output(["bash", "-c", norm_bash_cmd], text=True)
        expected_3 = str(Path.home() / ".local" / "state" / "herdr" / "plugins" / "herdr-bartender")
        self.assertTrue(py_dir_3 == bash_dir_3 == expected_3, f"Case 3 mismatch: py={py_dir_3}, bash={bash_dir_3}")
        self.assertTrue(expected_3.startswith(str(self.home)), "Case 3 must resolve inside the sandboxed HOME")


if __name__ == "__main__":
    unittest.main()
