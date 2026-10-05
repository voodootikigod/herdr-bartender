"""Vendor-hook guard: the R36 top-level classifier's cost and portability, its deadline, and the perl capture (R41).

Round-2 findings:

* security/bash: the awk classifier re-sliced the rest of a segment once per bracket, so string-free nested
  arrays (numeric matrices, coordinates in a tool_response) took quadratic time - a few hundred KB stalled the
  agent's hook for seconds, ~1.5MB for over a minute. Nothing bounded the awk step.
* bash: the primary (perl) stdin capture exited from a SIGALRM handler, which perl runs between ops, so a chunk
  already taken from the pipe by sysread but not yet printed was lost, and the vendor got a stream with a hole.

Found while fixing them: macOS's awk (BWK, 20200816) splits a string on every newline as well as on a
one-character separator, and runs gsub/match in O(matches x length). The old single-record classifier therefore
misread pretty-printed payloads on macOS (a SessionEnd classified as N). The classifier now streams line by line
with single-character splits only. These program-level tests run under every awk found on the host
(``awk``, ``mawk``, ``nawk``, ``original-awk``, ``gawk``, ``busybox awk``) plus any listed in
``HB_TEST_EXTRA_AWKS`` (os.pathsep-separated paths, e.g. a BWK awk built from Apple's sources).
"""

import json
import os
import re
import shutil
import subprocess
import time
import unittest

from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE
from tests.support import run_guard
from tests.support.guard_harness import leftovers, make_shim, path_with, start_fifo_writer
from tests.test_hooks_guard import _GuardCase

SID = "top_level_session_0001"
SID_RE = re.compile(r"^[a-zA-Z0-9_-]{16,64}$")
UUID_RECORD = '{"vendor_session_id":"vendor_uuid_record_0001"}'
PERL_SEAM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "perl")


def classifier_program() -> str:
    match = re.search(r"_HB_AWK='(.*?)'\n", HOOK_GUARD_TEMPLATE, re.S)
    assert match, "the guard must define _HB_AWK"
    return match.group(1)


def available_awks() -> list:
    """Every distinct awk implementation on this host (argv prefixes), deduplicated by resolved binary."""
    found, seen = [], set()
    candidates = [[name] for name in ("awk", "mawk", "nawk", "original-awk", "gawk")]
    if shutil.which("busybox"):
        candidates.append(["busybox", "awk"])
    extra = [p for p in os.environ.get("HB_TEST_EXTRA_AWKS", "").split(os.pathsep) if p]
    candidates.extend([p] for p in extra)
    for argv in candidates:
        path = shutil.which(argv[0])
        if not path:
            continue
        key = (os.path.realpath(path), tuple(argv[1:]))
        if key in seen:
            continue
        seen.add(key)
        found.append([path, *argv[1:]])
    return found


def classify(awk: list, payload: bytes, timeout: float = 10.0):
    """(class letter, validated session id) exactly as the guard reads the awk output."""
    env = {"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run([*awk, classifier_program()], input=payload, capture_output=True, env=env,
                         timeout=timeout, check=True).stdout.decode("latin-1").rstrip("\n")
    cls, _, sid = out.partition(" ")
    return cls, sid if SID_RE.match(sid) else ""


def doc(event=None, indent=None, **members):
    body = {"session_id": SID, **({"hook_event_name": event} if event else {}), **members}
    return json.dumps(body, indent=indent, ensure_ascii=False).encode("utf-8")


CORPUS = (
    ("compact-sessionend", doc("SessionEnd"), ("S", SID)),
    ("pretty-sessionend", doc("SessionEnd", indent=2), ("S", SID)),
    ("pretty-stop", doc("Stop", indent=4), ("T", SID)),
    ("crlf-pretty", doc("SessionEnd", indent=2).replace(b"\n", b"\r\n"), ("S", SID)),
    ("pretty-nested-terminal", doc("PreToolUse", indent=2, tool_input={"state": "Ended", "event": "SessionEnd"}),
     ("N", SID)),
    ("pretty-terminal-after-matrix", json.dumps({"tool_response": {"rows": [[1, 2], [3, [4]]]}, "session_id": SID,
                                                 "hook_event_name": "Stop"}, indent=2).encode(), ("T", SID)),
    ("nested-session-id-only", json.dumps({"hook_event_name": "PreToolUse",
                                           "tool_input": {"session_id": "nested_session_id_0001"}}).encode(),
     ("N", "")),
    ("escaped-fake-members", doc("UserPromptSubmit", prompt='say {"state":"Ended"} and "type":"Stop" \\ }'),
     ("N", SID)),
    ("escaped-backslash-before-quote", json.dumps({"prompt": "x\\", "state": "Ended", "session_id": SID}).encode(),
     ("S", SID)),
    ("escaped-quote-runs", json.dumps({"prompt": 'a\\"b\\\\"c', "type": "Done", "session_id": SID}).encode(),
     ("T", SID)),
    ("utf8-prompt", doc("UserPromptSubmit", prompt="café ☃ 日本"), ("N", SID)),
    ("top-level-array", b'[{"hook_event_name":"SessionEnd"}]', ("N", "")),
    ("escaped-session-id", json.dumps({"session_id": 'abc"defghijklmnopq', "event": "Ended"}).encode(), ("S", "")),
    ("first-member-wins", b'{"hook_event_name":"Working","hook_event_name":"SessionEnd"}', ("N", "")),
    ("empty", b"", ("N", "")),
    ("argv-codex-turn", json.dumps({"type": "agent-turn-complete", "thread-id": "t",
                                    "payload": {"type": "Ended"}}).encode(), ("T", "")),
)


def bracket_dense(rows: int, last_event: str, indent=None) -> bytes:
    """A string-free nested numeric matrix (MCP structuredContent shape), the top-level event AFTER it."""
    body = {"session_id": SID, "tool_response": {"structuredContent": {"rows": [[i, [i, 2 * i]] for i in range(rows)]}},
            "hook_event_name": last_event}
    return json.dumps(body, indent=indent, separators=None if indent else (",", ":")).encode()


class ClassifierProgramTests(unittest.TestCase):
    """The _HB_AWK program itself, under every awk implementation on the host."""

    def setUp(self):
        self.awks = available_awks()
        self.assertTrue(self.awks, "no awk on PATH")

    def test_corpus_under_every_available_awk(self):
        for awk in self.awks:
            for label, payload, expected in CORPUS:
                with self.subTest(awk=" ".join(awk), case=label):
                    self.assertEqual(classify(awk, payload), expected)

    def test_linear_time_on_bracket_dense_and_escape_heavy_payloads(self):
        """~1MB payloads: the old program needed ~40s (mawk) for the compact matrix; now well under the bound."""
        content = "".join(f'line {i} with "quotes"\tand tabs\n' for i in range(30000))
        payloads = (
            ("compact-matrix", bracket_dense(70000, "SessionEnd"), ("S", SID)),
            ("pretty-matrix", bracket_dense(12000, "Stop", indent=2), ("T", SID)),
            ("escape-heavy-string", doc("PostToolUse", tool_response={"file": {"content": content}}), ("N", SID)),
        )
        for awk in self.awks:
            for label, payload, expected in payloads:
                with self.subTest(awk=" ".join(awk), case=label, size=len(payload)):
                    self.assertGreater(len(payload), 900_000)
                    started = time.monotonic()
                    self.assertEqual(classify(awk, payload, timeout=20.0), expected)
                    self.assertLess(time.monotonic() - started, 5.0)


class GuardClassifierBoundsTests(_GuardCase):
    """The real guard: classification cost, the classifier deadline and the perl capture's deadline."""

    def owned_pane(self, name):
        """Fresh marker, Herdr alive and a UUID .vendor_active: a classified non-terminal event is suppressed."""
        pane = f"w1:p{name}"
        self.fresh_marker(pane)
        _, va = self.paths(pane)
        va.write_text(UUID_RECORD)
        return pane, va

    def test_bracket_dense_stdin_is_classified_within_bounds(self):
        """Plan §10.1 #56 (bounded guard latency). Finding (security/bash): ~1MB of nested arrays used to stall the
        hook for a minute. Now a PostToolUse is classified (N, suppressed) and a SessionEnd placed after the matrix
        is still found (S, passes through and unlinks .vendor_active), each well inside the bound."""
        script = self.script("guard-dense.sh", 'printf "V:"; wc -c | tr -d " "')
        for event, suppressed in (("PostToolUse", True), ("SessionEnd", False)):
            with self.subTest(event=event):
                pane, va = self.owned_pane(f"Dense{event}")
                payload = bracket_dense(45000, event)
                self.assertGreater(len(payload), 600_000)
                started = time.monotonic()
                res = run_guard(script, input=payload, env_extra={"HERDR_PANE_ID": pane}, text=False, timeout=30)
                elapsed = time.monotonic() - started
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, b"" if suppressed else f"V:{len(payload)}\n".encode())
                self.assertEqual(va.exists(), suppressed)
                self.assertLess(elapsed, 4.0)
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])

    @unittest.skipUnless(shutil.which("perl"), "the classifier deadline uses perl")
    def test_hung_classifier_is_cut_at_the_deadline_and_fails_open(self):
        """Plan §10.1 #56. Finding (security): the awk step had no deadline. A classifier that never finishes is now
        killed after 1s, and the unclassified event is never suppressed: the vendor runs with its stdin / argv
        intact."""
        make_shim(self.shim_bin, "awk", "exec sleep 10")
        script = self.script("guard-hung-awk.sh", 'printf "ARGV:%s|STDIN:" "${1:-}"; cat')
        body = json.dumps({"session_id": SID, "hook_event_name": "PreToolUse"})
        for mode in ("stdin", "argv"):
            with self.subTest(mode=mode):
                pane, va = self.owned_pane(f"Hung{mode}")
                args, stdin = ((), body) if mode == "stdin" else ((body,), "")
                started = time.monotonic()
                res = run_guard(script, *args, input=stdin,
                                env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
                elapsed = time.monotonic() - started
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, f"ARGV:{args[0] if args else ''}|STDIN:{stdin}")
                self.assertLess(elapsed, 3.0, "the classifier must be cut at its 1s deadline")
                self.assertTrue(va.exists())

    @unittest.skipUnless(shutil.which("perl"), "the primary capture path needs perl")
    def test_perl_capture_never_drops_a_chunk_read_at_the_deadline(self):
        """Plan §10.1 #56 (zero data loss on timed-out pipes). Finding (bash): the SIGALRM handler could exit after
        sysread took a chunk and before it was printed.
        HbSlowRead makes that window deterministic (the first read returns data, then stalls past the deadline):
        the vendor must still receive the prefix, the stalled chunk and the late remainder byte for byte."""
        script = self.script("guard-perl-capture.sh", "cat")
        fifo = self.tmp / "perl_fifo"
        os.mkfifo(str(fifo))
        start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; printf 'PART1-' >&3; sleep 0.2; printf 'PART2-' >&3; "
                                      f"sleep 1.5; printf 'PART3\\n' >&3; exec 3>&-")
        seam = {"PERL5LIB": PERL_SEAM_DIR, "PERL5OPT": "-MHbSlowRead", "HERDR_PANE_ID": "w1:pPerlSeam"}
        with open(str(fifo), "rb") as reader:
            res = run_guard(script, stdin=reader, env_extra=seam)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "PART1-PART2-PART3\n", "a chunk read before the deadline was dropped")
        self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])


if __name__ == "__main__":
    unittest.main()
