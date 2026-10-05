"""Pure intake functions: invocation parsing (R20), identity (R1/R2/R3), admission
(Cases A/B/C), §2.3 field resolution, sanitization and container matching."""

import io
import json
import os
import unittest
from unittest import mock

from herdr_bartender import intake, sanitize
from tests.support import SandboxTestCase

FIXTURE_ENVELOPE = {
    "event": "pane.agent_status_changed",
    "data": {"pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude", "agent_status": "working",
             "timestamp": 1728000000.123},
    "context": {"focused_pane_id": "w1:p1", "focused_pane_agent": "claude", "focused_pane_cwd": "/workspace",
                "workspace_id": "w1", "workspace_label": "Dev", "tab_id": "w1:t1"},
}


def _ident(pane="w1:p1", focused=None, data_extra=None, env=None):
    data = {"pane_id": pane}
    data.update(data_extra or {})
    ident, _ = intake.resolve_identity(data, {"focused_pane_id": focused} if focused else {}, env=env or {})
    return ident


class ParseInvocationTests(unittest.TestCase):
    def test_stdin_envelope_is_primary(self):
        """Plan §3.4 / R20 (gap event-input-contract, main-stdin-envelope): the stdin envelope wins over argv and env."""
        env = {"HERDR_PLUGIN_EVENT": "tab.closed", "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"tab_id": "x"}})}
        name, data, ctx = intake.parse_invocation(["pane.closed"], json.dumps(FIXTURE_ENVELOPE).encode(), env)
        self.assertEqual(name, "pane.agent_status_changed")
        self.assertEqual(data["pane_id"], "w1:p1")
        self.assertEqual(ctx["tab_id"], "w1:t1")

    def test_argv_event_name_when_envelope_lacks_event(self):
        """Plan §3.4 / R20 (gap event-input-contract): argv[1] names the event when the envelope has no `event`."""
        raw = json.dumps({"data": {"pane_id": "w1:p1"}}).encode()
        name, data, ctx = intake.parse_invocation(["pane.closed"], raw, {})
        self.assertEqual((name, data, ctx), ("pane.closed", {"pane_id": "w1:p1"}, {}))

    def test_flags_are_never_event_names(self):
        """R20 (gap event-input-contract): argv[1] is used only when it is not a flag."""
        name, data, _ = intake.parse_invocation(["--verbose"], b"", {})
        self.assertEqual(name, "")
        self.assertIsNone(data)

    def test_empty_stdin_known_argv_event_has_no_payload(self):
        """R20 (gap event-input-contract): empty stdin with a known argv event yields data=None (safe no-op)."""
        name, data, ctx = intake.parse_invocation(["pane.closed"], b"", {})
        self.assertEqual(name, "pane.closed")
        self.assertIsNone(data)
        self.assertEqual(ctx, {})

    def test_malformed_stdin_falls_back_to_argv(self):
        """R20 (gap event-input-contract): malformed stdin JSON or a non-object envelope never raises."""
        for raw in (b"{not json", b"[1,2]", b"\xff\xfe", b"null", b"   \n"):
            with self.subTest(raw=raw):
                name, data, _ = intake.parse_invocation(["tab.closed"], raw, {})
                self.assertEqual(name, "tab.closed")
                self.assertIsNone(data)

    def test_non_dict_data_and_context_become_empty(self):
        """Gap main-stdin-envelope: non-dict envelope data/context are treated as {}."""
        raw = json.dumps({"event": "pane.closed", "data": [1], "context": "x"}).encode()
        name, data, ctx = intake.parse_invocation([], raw, {})
        self.assertEqual((name, data, ctx), ("pane.closed", {}, {}))

    def test_legacy_env_fallback(self):
        """R20 (gap event-input-contract): legacy HERDR_PLUGIN_EVENT* env vars are the last fallback."""
        env = {
            "HERDR_PLUGIN_EVENT": "workspace.closed",
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"workspace_id": "w9"}}),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_id": "w1"}),
        }
        self.assertEqual(intake.parse_invocation([], b"", env),
                         ("workspace.closed", {"workspace_id": "w9"}, {"workspace_id": "w1"}))
        bad = dict(env, HERDR_PLUGIN_EVENT_JSON="{oops", HERDR_PLUGIN_CONTEXT_JSON="[]")
        self.assertEqual(intake.parse_invocation([], b"", bad), ("workspace.closed", None, {}))

    def test_event_name_is_validated_not_repaired(self):
        """Plan §2.3 `event` row: a malformed envelope event is ignored (argv wins), never repaired into a known name."""
        raw = json.dumps({"event": "pane.closed\x00", "data": {}}).encode()
        self.assertEqual(intake.parse_invocation(["tab.closed"], raw, {})[0], "tab.closed")
        self.assertEqual(intake.parse_invocation([], raw, {})[0], "")
        self.assertEqual(sanitize.sanitize_event_name("pane.agent_status_changed\x1b[1m"), "pane.agent_status_changed1m")
        self.assertEqual(len(sanitize.sanitize_event_name("a" * 200)), 64)
        self.assertEqual(sanitize.sanitize_event_name(None), "")


class ReadStdinTests(unittest.TestCase):
    def test_reads_pipe_until_eof(self):
        """R20 (gap event-input-contract): a closed pipe is read to EOF within budget."""
        r, w = os.pipe()
        os.write(w, b'{"event":"pane.closed"}')
        os.close(w)
        with os.fdopen(r, "rb") as f:
            self.assertEqual(intake.read_stdin_bounded(f, budget=1.0), b'{"event":"pane.closed"}')

    def test_open_pipe_without_eof_is_bounded(self):
        """R20 (gap event-input-contract): a stalled writer cannot block past the budget."""
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        os.write(w, b"partial")
        clock = iter([0.0, 0.0, 0.05, 0.2, 0.3, 0.4, 0.5, 9.0, 9.0, 9.0])
        with os.fdopen(r, "rb") as f:
            got = intake.read_stdin_bounded(f, budget=0.1, now=lambda: next(clock))
        self.assertEqual(got, b"partial")

    def test_size_cap(self):
        """R20 (gap event-input-contract): input beyond max_bytes is cut off (and later parses as malformed)."""
        r, w = os.pipe()
        os.write(w, b"x" * 5000)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            self.assertEqual(len(intake.read_stdin_bounded(f, budget=1.0, max_bytes=1024)), 1024)

    def test_missing_or_unusable_stream(self):
        """R20 (gap event-input-contract): no stdin, or one without a file descriptor, reads as empty."""
        self.assertEqual(intake.read_stdin_bounded(None, budget=1.0), b"")
        self.assertEqual(intake.read_stdin_bounded(io.StringIO("x"), budget=1.0), b"")


class IdentityTests(unittest.TestCase):
    def test_pane_id_required_no_focused_fallback(self):
        """Gap pane-closed-context-leak: an event without pane_id never resolves to the focused pane."""
        for data in ({}, {"pane_id": None}, {"pane_id": ""}, {"pane_id": 7}):
            with self.subTest(data=data):
                ident, notes = intake.resolve_identity(data, {"focused_pane_id": "w1:pF"}, env={})
                self.assertIsNone(ident)
                self.assertTrue(notes)

    def test_colon_less_uses_event_workspace_then_env(self):
        """R1 (gap normalize-env-fallback): ws = event workspace_id or HERDR_WORKSPACE_ID; empty -> rejected."""
        self.assertEqual(_ident("p1", data_extra={"workspace_id": "w2"}).canonical_pane, "w2:p1")
        self.assertEqual(_ident("p1", env={"HERDR_WORKSPACE_ID": "wE"}).canonical_pane, "wE:p1")
        ident, _ = intake.resolve_identity({"pane_id": "p1"}, {"workspace_id": "wCtx", "focused_pane_id": "p1"}, env={})
        self.assertIsNone(ident, "context.workspace_id must never qualify a colon-less pane")

    def test_invalid_event_workspace_falls_through(self):
        """Plan §2.3 workspace_id row (gap tab-workspace-validation): an invalid event workspace_id falls through."""
        ident = _ident("p1", data_extra={"workspace_id": "bad ws!"}, env={"HERDR_WORKSPACE_ID": "wE"})
        self.assertEqual(ident.canonical_pane, "wE:p1")

    def test_prefix_is_authoritative(self):
        """R2 (gap workspace-id-precedence): a colon-qualified pane's prefix overrides a mismatched event workspace_id."""
        ident, notes = intake.resolve_identity({"pane_id": "w1:p1", "workspace_id": "w2"}, {}, env={})
        self.assertEqual((ident.canonical_pane, ident.workspace_id), ("w1:p1", "w1"))
        self.assertTrue(any("mismatch" in n for n in notes))
        self.assertEqual(_ident("default:p1").workspace_id, "default")

    def test_invalid_canonical_rejected(self):
        """Plan §2.2 admission (gap tests-isolation-weak): invalid/over-length canonical IDs are rejected, never truncated."""
        for pane in ("w1:" + "p" * 46, "w1:p 1", "w1:p\x1b1"):
            with self.subTest(pane=pane):
                self.assertIsNone(_ident(pane))

    def test_is_focused_matches_raw_or_canonical(self):
        """R3 (gap focus-match-contradiction): is_focused = focused_id in (canonical_pane, raw_pane)."""
        self.assertTrue(_ident("p1", focused="p1", data_extra={"workspace_id": "w1"}).is_focused)
        self.assertTrue(_ident("p1", focused="w1:p1", data_extra={"workspace_id": "w1"}).is_focused)
        self.assertFalse(_ident("w1:p1", focused="w1:p2").is_focused)
        self.assertFalse(_ident("w1:p1").is_focused)


class StatusClassificationTests(unittest.TestCase):
    def test_classify_status(self):
        """Plan §2.1 (gap unrecognized-status-log): known, unknown and unrecognized statuses are distinguished."""
        for s in ("working", "blocked", "done", "idle"):
            self.assertEqual(intake.classify_status(s), "valid")
        self.assertEqual(intake.classify_status("unknown"), "unknown")
        for s in ("sleeping", None, 3, ""):
            self.assertEqual(intake.classify_status(s), "unrecognized")


class AdmissionTests(unittest.TestCase):
    LIVE = {"desired_state": "Working", "raw_agent": "claude"}
    ENDED = {"desired_state": "Ended", "raw_agent": "claude"}

    def _admit(self, status, data, cached=None, focused=None, ctx_agent=None):
        data = dict(data, pane_id="w1:p1")
        ctx = {"focused_pane_id": focused, "focused_pane_agent": ctx_agent}
        ident, _ = intake.resolve_identity(data, ctx, env={})
        return intake.admit_status(status, data, ctx, ident, cached or {})

    def test_case_a_idle_with_agent(self):
        """Plan §2.2 Case A: idle with a non-empty agent -> Idle."""
        self.assertEqual(self._admit("idle", {"agent": "claude"}), ("Idle", "claude"))

    def test_case_b_null_or_empty_agent_ends_live_session(self):
        """Plan §2.2 Case B (gap idle-case-c-omitted-key): agent null/"" -> Ended, even on a focused pane with focused agent."""
        for agent in (None, "", "   "):
            with self.subTest(agent=agent):
                self.assertEqual(self._admit("idle", {"agent": agent}, self.LIVE, focused="w1:p1", ctx_agent="codex"),
                                 ("Ended", "claude"))
                self.assertIsNone(self._admit("idle", {"agent": agent}), "no session -> unassisted shell, discarded")

    def test_case_c_omitted_key(self):
        """Plan §2.2 Case C (gap idle-case-c-omitted-key): key omitted -> Idle with retained agent, else discard."""
        self.assertEqual(self._admit("idle", {}, self.LIVE), ("Idle", "claude"))
        self.assertIsNone(self._admit("idle", {}))
        self.assertIsNone(self._admit("idle", {}, self.ENDED))
        self.assertIsNone(self._admit("idle", {}, {"desired_state": "Working"}), "no cached raw_agent -> discard")

    def test_no_resurrection_of_ended(self):
        """Plan §2.2 (gap ended-resurrection-cached-agent): working without agent cannot revive an Ended session."""
        self.assertIsNone(self._admit("working", {}, self.ENDED))
        self.assertEqual(self._admit("working", {"agent": "codex"}, self.ENDED), ("Working", "codex"))

    def test_whitespace_agent_is_not_positive(self):
        """Plan §2.2 L160 (gap admission-whitespace-agent): whitespace-only agent is not a positive signal."""
        self.assertIsNone(self._admit("working", {"agent": "  \t "}))
        self.assertEqual(self._admit("working", {"agent": "  claude "}), ("Working", "claude"))

    def test_focused_agent_only_when_focused(self):
        """R3 / Plan §2.3 agent row (gap tests-isolation-weak): focused_pane_agent admits only the focused pane."""
        self.assertIsNone(self._admit("working", {}, focused="w1:p2", ctx_agent="codex"))
        self.assertEqual(self._admit("working", {}, focused="w1:p1", ctx_agent="codex"), ("Working", "codex"))
        self.assertEqual(self._admit("blocked", {}, self.LIVE, focused="w1:p2", ctx_agent="codex"), ("Waiting", "claude"))

    def test_agent_is_sanitized(self):
        """Plan §2.3 agent row (gap agent-sanitization): admission uses the control-stripped, 64-char agent."""
        state, agent = self._admit("working", {"agent": "\x1b]0;evil\x07cla\x1b[31mude" + "x" * 200})
        self.assertEqual(state, "Working")
        self.assertTrue(agent.startswith("claude"))
        self.assertEqual(len(agent), 64)


class FieldResolutionTests(unittest.TestCase):
    def _fields(self, data, ctx, cached=None):
        data = dict(data, pane_id=data.get("pane_id", "w1:p1"))
        ident, _ = intake.resolve_identity(data, ctx, env={})
        return intake.resolve_fields(data, ctx, ident, cached or {})

    def test_cwd_hierarchy_cached_beats_focused(self):
        """Plan §2.3 cwd row (gap cwd-order-and-sanitization): event -> cached -> focused cwd -> workspace cwd -> ''."""
        ctx = {"focused_pane_id": "w1:p1", "focused_pane_cwd": "/focused", "workspace_cwd": "/ws"}
        self.assertEqual(self._fields({"cwd": "/ev"}, ctx, {"cwd": "/cached"}).cwd, "/ev")
        self.assertEqual(self._fields({}, ctx, {"cwd": "/cached"}).cwd, "/cached")
        self.assertEqual(self._fields({}, ctx).cwd, "/focused")
        self.assertEqual(self._fields({}, dict(ctx, focused_pane_cwd=None)).cwd, "/ws")
        self.assertEqual(self._fields({}, dict(ctx, focused_pane_id="w1:p2")).cwd, "")

    def test_cwd_sanitized_and_bounded(self):
        """Plan §2.3 cwd row (gap cwd-order-and-sanitization): control chars stripped; 256 chars max."""
        f = self._fields({"cwd": "/a\x00b\x1b[2Jc/" + "d" * 400}, {})
        self.assertTrue(f.cwd.startswith("/abc/"))
        self.assertEqual(len(f.cwd), 256)

    def test_tab_id_validated_and_focus_gated(self):
        """Plan §2.3 tab_id row (gap tab-workspace-validation): invalid tab_id falls through; context only if focused."""
        ctx = {"focused_pane_id": "w1:p1", "tab_id": "w1:tCtx"}
        self.assertEqual(self._fields({"tab_id": "bad tab"}, ctx, {"tab_id": "w1:tCached"}).tab_id, "w1:tCached")
        self.assertEqual(self._fields({"tab_id": "bad tab"}, ctx).tab_id, "w1:tCtx")
        self.assertIsNone(self._fields({"tab_id": "bad tab"}, dict(ctx, focused_pane_id="w1:p9")).tab_id)
        self.assertIsNone(self._fields({"tab_id": 5}, {}).tab_id)

    def test_title_hierarchy(self):
        """Plan §2.3 title row: event -> cached -> workspace_label (focused only) -> 'Pane <id>'."""
        ctx = {"focused_pane_id": "w1:p1", "workspace_label": "Dev"}
        self.assertEqual(self._fields({"title": "\x1b[1mT\x1b[0m"}, ctx).title, "T")
        self.assertEqual(self._fields({}, ctx, {"title": "Cached"}).title, "Cached")
        self.assertEqual(self._fields({}, ctx).title, "Dev")
        self.assertEqual(self._fields({}, dict(ctx, focused_pane_id=None)).title, "Pane w1:p1")

    def test_workspace_from_canonical_prefix(self):
        """R2 (gap workspace-id-precedence): stored workspace_id is the canonical prefix; never 'default' fallback."""
        self.assertEqual(self._fields({"workspace_id": "w2"}, {}).workspace_id, "w1")
        self.assertEqual(self._fields({"pane_id": "p3", "workspace_id": "wX"}, {"workspace_id": "w1"}).workspace_id, "wX")


class SanitizeTests(unittest.TestCase):
    def test_csi_private_and_intermediate_bytes(self):
        """Plan §2.3 title row (gap title-csi-coverage): private-mode/intermediate CSI and 8-bit CSI are removed."""
        self.assertEqual(sanitize.sanitize_title("\x1b[?25hHi\x1b[>4;2m!"), "Hi!")
        self.assertEqual(sanitize.sanitize_title("a\x1b[1 qb\x9b31mc"), "abc")

    def test_osc_dcs_variants(self):
        """Plan §2.3 title row (gap title-csi-coverage): OSC-8 hyperlinks, 8-bit OSC/DCS and unterminated tails."""
        link = "\x1b]8;;http://x\x1b\\click\x1b]8;;\x1b\\"
        self.assertEqual(sanitize.sanitize_title(link), "click")
        self.assertEqual(sanitize.sanitize_title("a\x9d0;t\x07b\x90q\x1b\\c"), "abc")
        self.assertEqual(sanitize.sanitize_title("ok\x1b]0;never-terminated"), "ok")
        self.assertEqual(sanitize.sanitize_title("ok\x1bPdcs-tail"), "ok")

    def test_controls_and_title_length(self):
        """Plan §2.3 title row: C0/C1 controls stripped and the title truncated to 120."""
        self.assertEqual(sanitize.sanitize_title("a\x00\x07\x85\x7fb"), "ab")
        self.assertEqual(len(sanitize.sanitize_title("t" * 500)), 120)
        self.assertEqual(sanitize.sanitize_title(12), "12")

    def test_bidi_and_invisible_format_characters_are_stripped(self):
        """R47 (low finding): a terminal-set title, agent or cwd cannot spoof Top Shelf with Unicode bidi overrides
        (Trojan-Source style reordering), zero-width/invisible format characters, tag characters or line/paragraph
        separators. ZWJ/ZWNJ stay (emoji sequences and Indic/Persian shaping need them)."""
        self.assertEqual(sanitize.sanitize_title("build \u202eexe.txt\u202c ok"), "build exe.txt ok")
        self.assertEqual(sanitize.sanitize_title("a\u2066b\u2067c\u2068d\u2069e\u200ef\u200fg\u061ch"), "abcdefgh")
        self.assertEqual(sanitize.sanitize_title("\ufeffz\u200bw\u2060s\u00adp\u2028l\u2029x"), "zwsplx")
        self.assertEqual(sanitize.sanitize_title("hi\U000e0041\U000e0042\U000e007f"), "hi")
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        self.assertEqual(sanitize.sanitize_title(f"team {family}"), f"team {family}")
        self.assertEqual(sanitize.sanitize_title("ab\u200cc"), "ab\u200cc")
        self.assertEqual(sanitize.format_agent_name("\u202eedoc\u202c"), "Edoc (Herdr)")
        self.assertEqual(sanitize.sanitize_cwd("/tmp/\u202egpj.sh"), "/tmp/gpj.sh")

    def test_every_format_or_separator_character_is_stripped(self):
        """R47: every Unicode Cf/Zl/Zp code point this Python knows (except ZWJ/ZWNJ) is removed by the shared helper.
        A failure on a newer Python names a format character a later Unicode version added to INVISIBLE_RE's list."""
        import sys
        import unicodedata
        kept = {"\u200c", "\u200d"}
        missed = [f"U+{cp:04X}" for cp in range(sys.maxunicode + 1)
                  if unicodedata.category(chr(cp)) in ("Cf", "Zl", "Zp") and chr(cp) not in kept
                  and sanitize.strip_terminal_controls(f"a{chr(cp)}b") != "ab"]
        self.assertEqual(missed, [])

    def test_format_agent_name_sanitized(self):
        """Plan §2.3 agent row (gap agent-sanitization): OSC-injected and 200-char agents are cleaned, <= 64 chars."""
        self.assertEqual(sanitize.format_agent_name("\x1b]0;pwn\x07claude"), "Claude (Herdr)")
        self.assertEqual(sanitize.format_agent_name("  CODEX "), "Codex (Herdr)")
        long_name = sanitize.format_agent_name("a" * 200)
        self.assertLessEqual(len(long_name), 64)
        self.assertTrue(long_name.endswith(" (Herdr)"))
        for empty in (None, "", "  ", "\x1b[0m"):
            self.assertEqual(sanitize.format_agent_name(empty), "Herdr")

    def test_normalize_pane_id_env_fallback(self):
        """R1 (gap normalize-env-fallback): normalize_pane_id(raw, ws, env) consults HERDR_WORKSPACE_ID."""
        self.assertEqual(sanitize.normalize_pane_id("p1", None, {"HERDR_WORKSPACE_ID": "wE"}), "wE:p1")
        self.assertEqual(sanitize.normalize_pane_id("p1", "w1", {"HERDR_WORKSPACE_ID": "wE"}), "w1:p1")
        self.assertEqual(sanitize.normalize_pane_id("p1", None, {}), "")
        self.assertEqual(sanitize.normalize_pane_id("w2:p1", "w1", {}), "w2:p1")
        self.assertEqual(sanitize.normalize_pane_id(None, "w1", {}), "")


class HostAndSessionIdTests(unittest.TestCase):
    def test_hostname_truncated_to_32(self):
        """Plan §2.3 session_id row (gap hostname-truncation): sanitized host is [:32], fallback 'local'."""
        with mock.patch("herdr_bartender.sanitize.socket.gethostname", return_value="H" * 60 + ".example.com"):
            self.assertEqual(sanitize.sanitized_hostname(), "h" * 32)
        with mock.patch("herdr_bartender.sanitize.socket.gethostname", return_value="!!!.lan"):
            self.assertEqual(sanitize.sanitized_hostname(), "local")

    def test_host_is_the_first_dns_label(self):
        """R46 (low finding, documented deviation from the §2.3 formula): the domain suffix is dropped, as in the
        pre-package monolith, so session ids neither change across the upgrade nor with the network-dependent
        suffix macOS appends (.local, .lan, .attlocal.net). config and intake agree."""
        from herdr_bartender import config
        for raw, host in (("Chris-MacBook-Pro.local", "chris-macbook-pro"), ("Chris-MacBook-Pro.lan", "chris-macbook-pro"),
                          ("dev_box", "dev_box"), (".hidden", "local")):
            with self.subTest(raw=raw):
                with mock.patch("socket.gethostname", return_value=raw):
                    self.assertEqual(sanitize.sanitized_hostname(), host)
                    self.assertEqual(config.get_sanitized_hostname(), host)

    def test_pinned_host_revalidated(self):
        """Plan §2.3 session_id row (gap hostname-truncation): an invalid pinned host falls back to the current host."""
        with mock.patch("herdr_bartender.sanitize.socket.gethostname", return_value="mac"):
            self.assertEqual(intake.resolve_host("pinned-host"), "pinned-host")
            for bad in ("x" * 33, "Bad Host", "", None, 5):
                self.assertEqual(intake.resolve_host(bad), "mac")

    def test_session_id_bounds(self):
        """Plan §2.3 session_id row (gap hostname-truncation): session_id must match ^[a-zA-Z0-9_:-]{1,96}$."""
        self.assertEqual(intake.build_session_id("mac", "w1:p1"), "herdr:mac:w1:p1")
        self.assertEqual(intake.build_session_id("h" * 33, "w1:p1"), "")
        self.assertEqual(intake.build_session_id("mac", "w1:p 1"), "")

    def test_salvage_host_pick(self):
        """Plan L273 (gap salvage-host-pin): salvage re-pins the most common valid host among salvaged ids."""
        sids = ["herdr:old:w1:p1", "herdr:old:w1:p2", "herdr:new:w1:p3", "garbage"]
        self.assertEqual(intake.pick_salvage_host(sids, "cur"), "old")
        self.assertEqual(intake.pick_salvage_host([], "cur"), "cur")


class ContainerMatchingTests(unittest.TestCase):
    def test_pane_match_exact(self):
        """Plan §2.1 L151 (gap pane-closed-suffix-match): closing w1:p1 never matches x:w1:p1."""
        info = {"pane_id": "x:w1:p1"}
        self.assertFalse(intake.session_matches_pane("herdr:mac:x:w1:p1", info, "w1:p1", "mac"))
        self.assertTrue(intake.session_matches_pane("herdr:mac:w1:p1", {"pane_id": "w1:p1"}, "w1:p1", "mac"))
        self.assertTrue(intake.session_matches_pane("herdr:mac:w1:p1", {}, "w1:p1", "mac"))

    def test_tab_match_exact_and_qualified(self):
        """Plan §2.1 L152 (gaps tab-closed-suffix-match, cascade-container-matching): exact qualified tab equality."""
        self.assertFalse(intake.session_matches_tab({"tab_id": "w1:t1", "workspace_id": "w1"}, "w11:t1", None))
        self.assertTrue(intake.session_matches_tab({"tab_id": "w1:t1", "workspace_id": "w1"}, "w1:t1", None))
        self.assertFalse(intake.session_matches_tab({"tab_id": "t1", "workspace_id": "w1"}, "w2:t1", None))
        self.assertTrue(intake.session_matches_tab({"tab_id": "t1", "workspace_id": "w1"}, "w1:t1", None))
        self.assertTrue(intake.session_matches_tab({"tab_id": "w1:t1", "workspace_id": "w1"}, "t1", "w1"))
        self.assertFalse(intake.session_matches_tab({"tab_id": "t1", "workspace_id": "w1"}, "t1", None))
        self.assertFalse(intake.session_matches_tab({"tab_id": None, "workspace_id": "w1"}, "w1:t1", None))

    def test_workspace_match_exact(self):
        """Plan §2.1 L153 (gap tab-closed-suffix-match): workspace equal to the host never matches other workspaces."""
        info = {"workspace_id": "w1", "pane_id": "w1:p1"}
        self.assertFalse(intake.session_matches_workspace(info, "macbook"))
        self.assertTrue(intake.session_matches_workspace(info, "w1"))
        self.assertTrue(intake.session_matches_workspace({"pane_id": "w1:p1"}, "w1"))
        self.assertFalse(intake.session_matches_workspace({"pane_id": "w11:p1", "workspace_id": "w11"}, "w1"))

    def test_container_id_no_context_fallback(self):
        """Gap cascade-container-matching: container ids come from event data only and must be valid."""
        self.assertEqual(intake.container_id({"tab_id": "w1:t1"}, "tab_id"), "w1:t1")
        for data in ({}, {"tab_id": None}, {"tab_id": "bad id"}, {"tab_id": ["w1"]}):
            self.assertIsNone(intake.container_id(data, "tab_id"))

    def test_close_payload_fields(self):
        """Plan §2.3 terminal/event/seq rows (gap close-payload-fields): close payloads carry all fields."""
        info = {"agent": "Claude (Herdr)", "title": "T", "cwd": "/c"}
        self.assertEqual(intake.build_close_payload("herdr:m:w1:p1", info, "tab.closed", 4), {
            "state": "Ended", "agent": "Claude (Herdr)", "session_id": "herdr:m:w1:p1", "title": "T", "cwd": "/c",
            "terminal": "Herdr", "event": "tab.closed", "seq": 4,
        })
        self.assertEqual(intake.build_close_payload("s", {}, "pane.closed", 1)["agent"], "Herdr")


class SandboxedIntakeTests(SandboxTestCase):
    start_bridge = False

    def test_sanitized_hostname_matches_config_for_short_hosts(self):
        """Gap hostname-truncation: for hosts <= 32 chars the intake host equals config.get_sanitized_hostname()."""
        from herdr_bartender.config import get_sanitized_hostname
        if len(get_sanitized_hostname()) <= 32:
            self.assertEqual(sanitize.sanitized_hostname(), get_sanitized_hostname())


if __name__ == "__main__":
    unittest.main()
