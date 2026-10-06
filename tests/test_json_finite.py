"""Non-finite JSON numbers at every trust boundary (gate finding, adversarial review round 4).

Python's ``json.loads`` accepts ``NaN``, ``Infinity`` and ``-Infinity`` and turns ``1e999`` into ``inf``. A NaN
source timestamp compares false against everything, so it slipped past the staleness and tombstone checks and was
persisted into the cache (which then is not standard JSON). ``jsonsafe.loads`` now rejects them as ``ValueError``
(R49: unusable input), and the staging coercion refuses a non-finite numeric string such as ``"NaN"``.
"""

import json
import math
import time
import unittest

from herdr_bartender import jsonsafe
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.intake import parse_invocation
from herdr_bartender.staging import _num
from tests.support import SandboxTestCase

PANE = "w1:pFinite"
WORKING = {"agent_status": "working", "pane_id": PANE, "workspace_id": "w1", "agent": "claude"}


def strict_loads(text: str) -> object:
    """Standard JSON only: raises on NaN / Infinity."""
    def refuse(name):
        raise ValueError(f"non-standard constant {name}")
    return json.loads(text, parse_constant=refuse)


class LoadsTests(unittest.TestCase):
    def test_non_finite_numbers_are_a_value_error(self):
        for text in ('{"t": NaN}', '{"t": Infinity}', '{"t": -Infinity}', '[1e999]', '[-1e999]',
                     '[' + "9" * 400 + ']'):
            with self.subTest(text=text[:40]):
                with self.assertRaises(ValueError):
                    jsonsafe.loads(text)

    def test_finite_numbers_still_decode(self):
        self.assertEqual(jsonsafe.loads('{"t": 1759650000.25, "n": -3, "e": 1e300, "z": 0}'),
                         {"t": 1759650000.25, "n": -3, "e": 1e300, "z": 0})


class StagingCoercionTests(unittest.TestCase):
    def test_non_finite_numeric_strings_and_overflowing_ints_are_no_timestamp(self):
        for value in ("NaN", "nan", "inf", "-Infinity", 10 ** 400, float("nan"), float("inf")):
            with self.subTest(value=str(value)[:20]):
                self.assertIsNone(_num(value))
        self.assertEqual(_num("12.5"), 12.5)
        self.assertEqual(_num(7), 7.0)


class BoundaryTests(SandboxTestCase):
    start_bridge = True

    def test_stdin_envelope_with_nan_is_unusable(self):
        """R20: malformed stdin is a no-op; a NaN timestamp makes the envelope malformed."""
        envelope = '{"event": "pane.agent_status_changed", "data": {"agent_status": "working", "pane_id": "w1:p1", ' \
                   '"agent": "claude", "timestamp": NaN}}'
        name, data, _ = parse_invocation(["pane.agent_status_changed"], envelope.encode(), {})
        self.assertEqual((name, data), ("pane.agent_status_changed", None))

    def test_nan_string_timestamp_never_reaches_the_cache_or_reopens_a_closed_pane(self):
        """The reported bypass, through a string the decoder cannot refuse: a ``"NaN"`` timestamp is treated as
        absent, so the cache stays standard JSON and the close's tombstone still rejects an older working event."""
        now = time.time()
        handle_agent_status_changed({**WORKING, "timestamp": "NaN"}, {}, bridge_url=self.mock_url)
        handle_pane_closed({"pane_id": PANE, "timestamp": now}, {}, bridge_url=self.mock_url)
        raw = (self.state_dir / "active-sessions.json").read_text()
        data = strict_loads(raw)   # raises if NaN was persisted anywhere
        tombstone = data["tombstones"][PANE]
        self.assertTrue(all(math.isfinite(v) for v in tombstone.values() if isinstance(v, float)))
        handle_agent_status_changed({**WORKING, "timestamp": now - 30}, {}, bridge_url=self.mock_url)
        data = strict_loads((self.state_dir / "active-sessions.json").read_text())
        record = data["sessions"].get(self.sid(PANE))
        self.assertTrue(record is None or record["desired_state"] == "Ended",
                        "an event older than the close does not re-admit the pane")


if __name__ == "__main__":
    unittest.main()
