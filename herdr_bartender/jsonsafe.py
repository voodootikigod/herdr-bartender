"""Bounded JSON decoding for every trust boundary (stdin envelope, legacy env JSON, spool and result
envelopes, the cache, the orphan file and its journal, stamps, allowlists, bridge bodies).

``json.loads`` raises ``RecursionError`` (a ``RuntimeError``, not a ``ValueError``) on deep nesting:
about 1,000 levels (~2 KB) on macOS's Python 3.9, more on newer interpreters. Every caller here
treats ``ValueError`` as "unusable input", so ``loads`` reports over-deep input the same way. It also
rejects any decoded value nested deeper than ``MAX_DEPTH`` (no file or envelope this package reads
or writes nests beyond a handful of levels), so a payload that decodes on one interpreter cannot
fail later when it is re-encoded (spool envelopes keep ``event_data``/``context`` verbatim) and
the verdict is the same on every supported Python.

Non-finite numbers are unusable input too: ``NaN``/``Infinity``/``-Infinity`` (which ``json.loads``
accepts), a float literal that overflows to infinity (``1e999``) and an integer too long to be a float
are ``ValueError``s, so no NaN timestamp can defeat an ordering comparison or reach the cache.
"""

from __future__ import annotations

import json
import math
from typing import Union

MAX_DEPTH = 128
MAX_INT_DIGITS = 300   # float range ends near 1.8e308: a longer integer literal is no usable number here


class JSONTooDeep(ValueError):
    """The input nests deeper than the decoder or ``MAX_DEPTH`` allows."""


def _within_depth(value: object, limit: int) -> bool:
    """Iterative (never recursive) container depth check: the top-level container is depth 1."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True


class NonFiniteNumber(ValueError):
    """The input holds NaN, an infinity, or a number outside the float range."""


def _refuse_constant(name: str) -> float:
    raise NonFiniteNumber(f"non-finite JSON number {name}")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        raise NonFiniteNumber(f"JSON number {literal[:32]} is outside the float range")
    return value


def _bounded_int(literal: str) -> int:
    if len(literal.lstrip("-")) > MAX_INT_DIGITS:
        raise NonFiniteNumber(f"JSON integer with more than {MAX_INT_DIGITS} digits")
    return int(literal)


def loads(text: Union[str, bytes, bytearray]) -> object:
    """``json.loads`` that raises ``ValueError`` (``JSONTooDeep``), never ``RecursionError``, on deep nesting,
    and ``ValueError`` (``NonFiniteNumber``) on a non-finite number."""
    try:
        value = json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float, parse_int=_bounded_int)
    except RecursionError:
        raise JSONTooDeep("JSON nesting exceeds the interpreter's recursion limit") from None
    if not _within_depth(value, MAX_DEPTH):
        raise JSONTooDeep(f"JSON nesting deeper than {MAX_DEPTH} levels")
    return value
