"""Fail fast (instead of hanging the suite) when a reader blocks on a planted FIFO."""

import os
import threading


def call_without_blocking(test, fifo, fn):
    """Run ``fn`` in a thread; a reader stuck on ``fifo`` fails the test (and is released) instead of hanging it."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:   # re-raised on the test thread
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(2.0)
    if worker.is_alive():
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)   # give the blocked reader an EOF
        os.close(fd)
        worker.join(2.0)
        test.fail(f"reading {fifo.name} blocked")
    if "error" in box:
        raise box["error"]
    return box.get("value")
