"""Byte-exact guard-block text operations shared by install, uninstall, repair and rollback.

Contract (Plan §7.3, gap strip-not-inverse-of-insert): the inserted text is
exactly ``template + b"\\n"`` placed at the start of the line after the anchor,
and ``strip_guard`` removes exactly that span, so
``strip_guard(insert_guard(x, t)) == x`` for every accepted ``x``. All
functions work on ``bytes`` so CRLF and non-UTF-8 vendor content survive.

A guard is well formed only with exactly one BEGIN and one END marker, BEGIN
first, each at the start of its own line, END followed by a newline or EOF
(Plan §9.1, gap marker-count-validation). Anything else raises MarkerError and
callers must leave the file untouched.

R37 legacy layout: the pre-package monolith inserted ``b"\n" + block + b"\n"``, i.e. a
blank line before BEGIN. ``insert_guard`` always places BEGIN right after a non-empty
anchor line, so a blank line directly before BEGIN only ever comes from that layout;
``strip_guard`` removes it with the block, restoring the original hook byte-exact.
"""

from __future__ import annotations

import os
from typing import List, NamedTuple, Optional, Tuple

BEGIN_MARKER = b"# BEGIN HERDR-BARTENDER DEDUP GUARD"
END_MARKER = b"# END HERDR-BARTENDER DEDUP GUARD"
SUPPORTED_INTERPRETERS = frozenset({"bash", "sh"})
_PREAMBLE_COMMANDS = frozenset({b"set", b"shopt"})


class GuardTextError(ValueError):
    """Base class: the hook text cannot be patched or unpatched safely."""


class MarkerError(GuardTextError):
    """Missing, duplicated, misordered or misplaced BEGIN/END markers."""


class AnchorError(GuardTextError):
    """No safe insertion anchor (shebang on line 1 or a top-level nounset line)."""


class GuardSpan(NamedTuple):
    start: int  # offset of BEGIN marker
    block_end: int  # offset just past END marker (exclusive, no newline)
    end: int  # offset past END marker's trailing newline (or EOF)


def _at_line_start(content: bytes, offset: int) -> bool:
    return offset == 0 or content[offset - 1:offset] == b"\n"


def find_guard(content: bytes) -> Optional[GuardSpan]:
    """Return the guard span, None when no marker exists; MarkerError when malformed."""
    n_begin, n_end = content.count(BEGIN_MARKER), content.count(END_MARKER)
    if n_begin == 0 and n_end == 0:
        return None
    if n_begin != 1 or n_end != 1:
        raise MarkerError(f"mismatched dedup markers (BEGIN={n_begin}, END={n_end})")
    start, end_at = content.index(BEGIN_MARKER), content.index(END_MARKER)
    if end_at < start:
        raise MarkerError("mismatched dedup markers (END precedes BEGIN)")
    if not (_at_line_start(content, start) and _at_line_start(content, end_at)):
        raise MarkerError("mismatched dedup markers (marker not at start of line)")
    block_end = end_at + len(END_MARKER)
    tail = content[block_end:block_end + 1]
    if tail not in (b"", b"\n"):
        raise MarkerError("mismatched dedup markers (text after END marker)")
    return GuardSpan(start, block_end, block_end + len(tail))


def guard_state(content: bytes) -> str:
    """'present' or 'absent'; raises MarkerError when malformed."""
    return "absent" if find_guard(content) is None else "present"


def guard_block(content: bytes) -> Optional[bytes]:
    """The installed guard block text (BEGIN..END, no trailing newline), or None."""
    span = find_guard(content)
    return None if span is None else content[span.start:span.block_end]


def is_legacy_layout(content: bytes, span: Optional[GuardSpan] = None) -> bool:
    """R37: the guard sits after a blank line (the monolith's ``"\n" + block + "\n"`` insertion)."""
    span = find_guard(content) if span is None else span
    return span is not None and span.start >= 2 and content[span.start - 2:span.start] == b"\n\n"


def strip_guard(content: bytes) -> bytes:
    """Remove the guard exactly as insert_guard (or the legacy monolith, R37) placed it; no-op when absent."""
    span = find_guard(content)
    if span is None:
        return content
    start = span.start - 1 if is_legacy_layout(content, span) else span.start
    return content[:start] + content[span.end:]


def legacy_clean_variants(content: bytes) -> Tuple[bytes, ...]:
    """R37: other guard-free bytes an earlier installer may have approved for a legacy-layout hook.

    ``strip_guard`` gives the monolith's first-install hash (the original hook). A monolith re-install hashed
    the hook with two blank lines at the anchor, and the pre-R37 strip kept one; both are whitespace at our own
    insertion point only. Empty for any other layout.
    """
    span = find_guard(content)
    if span is None or not is_legacy_layout(content, span):
        return ()
    head, tail = content[:span.start - 1], content[span.end:]
    return head + b"\n" + tail, head + b"\n\n" + tail


def replace_guard(content: bytes, template: bytes) -> bytes:
    """Swap an existing guard block for ``template`` in place (stale-guard upgrade)."""
    span = find_guard(content)
    if span is None:
        raise MarkerError("no dedup guard to replace")
    return content[:span.start] + template + content[span.block_end:]


def insert_guard(content: bytes, template: bytes) -> bytes:
    """Insert ``template + b'\\n'`` after the anchor line of guard-free ``content``."""
    if find_guard(content) is not None:
        raise MarkerError("dedup guard already present")
    offset = find_anchor(content)
    return content[:offset] + template + b"\n" + content[offset:]


def find_anchor(content: bytes) -> int:
    """Byte offset of the line after the anchor (nounset line in the preamble, else shebang)."""
    lines = content.splitlines(keepends=True)
    has_shebang = bool(lines) and lines[0].startswith(b"#!")
    if has_shebang:
        _check_interpreter(lines[0])
    anchor = _nounset_anchor(lines, 1 if has_shebang else 0)
    if anchor is None and has_shebang:
        anchor = 0
    if anchor is None:
        raise AnchorError("no valid insertion anchor (shebang on line 1 or top-level `set -u`)")
    if not lines[anchor].endswith(b"\n"):
        raise AnchorError("insertion anchor is the last line and has no trailing newline")
    return sum(len(line) for line in lines[:anchor + 1])


def _check_interpreter(shebang: bytes) -> None:
    words = shebang[2:].decode("latin-1").split()
    if not words:
        raise AnchorError("empty shebang")
    interp = os.path.basename(words[0])
    if interp == "env":
        rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        interp = os.path.basename(rest[0]) if rest else ""
    if interp not in SUPPORTED_INTERPRETERS:
        raise AnchorError(f"unsupported hook interpreter {interp or '?'!r} (bash/sh only)")


def _nounset_anchor(lines: List[bytes], first: int) -> Optional[int]:
    """Index of the first top-level nounset line within the leading preamble, if any."""
    for idx in range(first, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        words = line.split(b"#", 1)[0].split()
        if line[:1] in (b" ", b"\t") or words[0] not in _PREAMBLE_COMMANDS:
            return None  # first real command: past the preamble
        if words[0] == b"set" and enables_nounset(words[1:]):
            return idx
    return None


def enables_nounset(args: List[bytes]) -> bool:
    """True when ``set <args>`` turns on nounset (-u, -eu, -euo pipefail, -o nounset)."""
    enabled = False
    for i, arg in enumerate(args):
        if arg == b"-o" and i + 1 < len(args) and args[i + 1] == b"nounset":
            enabled = True
        elif arg == b"+o" and i + 1 < len(args) and args[i + 1] == b"nounset":
            enabled = False
        elif arg.startswith(b"-") and not arg.startswith(b"--") and b"u" in arg[1:]:
            enabled = True
        elif arg.startswith(b"+") and b"u" in arg[1:]:
            enabled = False
    return enabled
