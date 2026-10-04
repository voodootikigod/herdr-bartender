"""Python vs Bash parity: canonical pane IDs, hex encoding and STATE_DIR resolution.

The Bash side is the REAL guard (herdr_bartender/hook_guard.sh), executed in
fall-through mode: no fake herdr process, so the guard is unhealthy and records
``panes/<hex>.vendor_active`` for the canonical pane it derived. The file name
it creates is compared with the Python intake path (resolve_identity +
get_hex_pane_id). A pane the guard rejects creates no file at all.
"""

import os
import string
import subprocess
import unittest
from pathlib import Path

from herdr_bartender.intake import resolve_identity
from herdr_bartender.paths import get_state_dir
from herdr_bartender.sanitize import get_hex_pane_id, normalize_pane_id
from tests.support import REPO_ROOT, SandboxTestCase

VARIED = (string.ascii_letters + string.digits) * 2  # no two identical 16-byte rows for od
GUARD_FILE = REPO_ROOT / "herdr_bartender" / "hook_guard.sh"
LOCALE_UTF8 = "en_US.UTF-8"
VENDOR_TAIL = 'echo "VENDOR_RAN"\nexit 0'

# (raw HERDR_PANE_ID, HERDR_WORKSPACE_ID or None, expected canonical or "" when rejected)
FIXTURE_TABLE = [
    ("p1", "w1", "w1:p1"),
    ("pane123", None, ""),
    ("pane123", "", ""),
    ("pane123", "wsA", "wsA:pane123"),
    ("wsB:pane456", None, "wsB:pane456"),
    ("wsB:pane456", "wsC", "wsB:pane456"),
    ("w2:p2", "w1", "w2:p2"),
    ("custom_name-4", "ws_alpha", "ws_alpha:custom_name-4"),
    ("w3:p5:sub", "w4", "w3:p5:sub"),
    ("w1:" + VARIED[:45], None, "w1:" + VARIED[:45]),    # exactly 48 chars
    ("w1:" + VARIED[:46], None, ""),                     # 49 chars rejected
    (VARIED[:45], "w1", "w1:" + VARIED[:45]),            # colon-less, canonical exactly 48
    (VARIED[:46], "w1", ""),                             # raw ok, canonical 49 rejected
    (VARIED[:49], "w1", ""),                             # raw over-length
    ("w1:p 1", None, ""),                                # invalid char (space)
    ("w1:p.1", None, ""),                                # invalid char (dot)
    ("p1", "w 1", ""),                                   # invalid workspace char
    ("p1", "w1;rm", ""),                                 # shell metachar in workspace
    ("", "w1", ""),                                      # empty pane id
]


class ParityTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.assertTrue(GUARD_FILE.is_file(), f"guard resource missing: {GUARD_FILE}")
        self.script = self.sandbox / "vendor-hook.sh"
        self.script.write_text(f"#!/bin/bash\nset -u\n{GUARD_FILE.read_text()}\n{VENDOR_TAIL}\n")
        os.chmod(self.script, 0o755)

    def _run_real_guard(self, raw: str, ws, extra_env=None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID")}
        env["HERDR_PANE_ID"] = raw
        if ws is not None:
            env["HERDR_WORKSPACE_ID"] = ws
        env.update(extra_env or {})
        return subprocess.run([str(self.script)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              env=env, timeout=20)

    def _guard_hexes(self, state_dir: Path) -> list:
        panes = state_dir / "panes"
        if not panes.is_dir():
            return []
        return sorted(p.name[: -len(".vendor_active")] for p in panes.iterdir() if p.name.endswith(".vendor_active"))

    def _python_canonical(self, raw: str, ws) -> str:
        env = {} if ws is None else {"HERDR_WORKSPACE_ID": ws}
        identity, _ = resolve_identity({"pane_id": raw}, {}, env=env)
        return identity.canonical_pane if identity else ""

    def _clear_panes(self):
        panes = self.state_dir / "panes"
        if panes.is_dir():
            for p in panes.iterdir():
                p.unlink()

    def test_p31_hex_encoding_parity(self):
        """Plan §10.1 #31 (gap t31-37-parity-dup): the real guard's panes/<hex> name equals Python's hex of the canonical pane."""
        for raw, ws, expected in FIXTURE_TABLE:
            with self.subTest(raw=raw, ws=ws):
                self._clear_panes()
                res = self._run_real_guard(raw, ws)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("VENDOR_RAN", res.stdout, "guard must fail open to the vendor body")
                py_canon = self._python_canonical(raw, ws)
                self.assertEqual(py_canon, expected)
                expected_hexes = [get_hex_pane_id(py_canon)] if py_canon else []
                self.assertEqual(self._guard_hexes(self.state_dir), expected_hexes,
                                 f"guard vs Python hex mismatch for raw={raw!r} ws={ws!r}")

    def test_p37_canonical_parity_fixture_table(self):
        """Plan §10.1 #37 (gaps t31-37-parity-dup, normalize-env-fallback): guard and Python agree on canonical IDs."""
        for raw, ws, expected in FIXTURE_TABLE:
            with self.subTest(raw=raw, ws=ws):
                self._clear_panes()
                self._run_real_guard(raw, ws)
                guard = [bytes.fromhex(h).decode("utf-8") for h in self._guard_hexes(self.state_dir)]
                self.assertEqual(guard, [expected] if expected else [])
                # R1: event workspace_id and $HERDR_WORKSPACE_ID derive the same canonical ID.
                env_only = normalize_pane_id(raw, None, {} if ws is None else {"HERDR_WORKSPACE_ID": ws})
                event_ws = normalize_pane_id(raw, ws or None, {})
                self.assertEqual(env_only, event_ws)

    def _assert_guard_matches_python(self, raw: str, ws, extra_env=None):
        self._clear_panes()
        self._run_real_guard(raw, ws, extra_env)
        py_canon = self._python_canonical(raw, ws)
        self.assertEqual(self._guard_hexes(self.state_dir), [get_hex_pane_id(py_canon)] if py_canon else [])

    def test_p31_hex_parity_repetitive_pane_id(self):
        """Plan §10.1 #31 (gap t31-37-parity-dup): a canonical ID with repeated 16-byte rows hexes identically."""
        self._assert_guard_matches_python("w1:" + "p" * 45, None)

    def test_p37_non_ascii_pane_rejected_like_python(self):
        """Plan §10.1 #37 (gap t31-37-parity-dup): a non-ASCII pane ID is rejected by both guard and Python."""
        probe = subprocess.run(["bash", "-c", "printf 'w1:p\\303\\2511' | grep -Eq '^[a-zA-Z0-9_:-]{1,48}$'"],
                               env=dict(os.environ, LC_ALL=LOCALE_UTF8), capture_output=True)
        if probe.returncode != 0:
            self.skipTest(f"{LOCALE_UTF8} does not collate non-ASCII letters into [a-z] on this host")
        self._assert_guard_matches_python("w1:p\u00e91", None, {"LC_ALL": LOCALE_UTF8, "LANG": LOCALE_UTF8})

    def test_p66_state_dir_resolution_parity(self):
        """Plan §10.1 #66: get_state_dir() and the real guard resolve ${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-...}} identically."""
        custom_state = self.tmp / "test_herdr_plugin_state"
        custom_xdg = self.tmp / "test_xdg_home"
        cases = [
            ({"HERDR_PLUGIN_STATE_DIR": str(custom_state)}, custom_state),
            ({"XDG_STATE_HOME": str(custom_xdg)}, custom_xdg / "herdr" / "plugins" / "herdr-bartender"),
            ({}, Path(os.environ["HOME"]) / ".local" / "state" / "herdr" / "plugins" / "herdr-bartender"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
                os.environ.pop("XDG_STATE_HOME", None)
                os.environ.update(overrides)
                self.assertEqual(get_state_dir(), expected)
                self._run_real_guard("w1:pState", None)
                self.assertEqual(self._guard_hexes(expected), [get_hex_pane_id("w1:pState")])
                self.assertTrue(str(expected).startswith(str(self.tmp)), "must resolve inside the sandbox")


if __name__ == "__main__":
    unittest.main()
