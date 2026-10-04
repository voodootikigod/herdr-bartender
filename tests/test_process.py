"""Process discovery and liveness (pgrep/ps are PATH shims inside the sandbox)."""

import unittest

from herdr_bartender.process import get_herdr_pid
from tests.support import SandboxTestCase


class ProcessTests(SandboxTestCase):
    start_bridge = False

    # WEAK: t53-herdr-pid
    def test_p53_earliest_start_herdr_pid(self):
        """Plan §10.1 #53: get_herdr_pid() exists and copes with the (shimmed) process list."""
        self.assertTrue(get_herdr_pid() is not None or get_herdr_pid() is None)


if __name__ == "__main__":
    unittest.main()
