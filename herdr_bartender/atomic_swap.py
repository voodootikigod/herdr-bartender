"""R71: atomically exchange two paths (Linux ``renameat2(RENAME_EXCHANGE)``, macOS ``renamex_np(RENAME_SWAP)``).

Lets the hook writer verify what it displaced *after* the swap and swap the vendor's version back, instead of a
compare-then-replace that a concurrent vendor update could slip between.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import Callable, Optional

_AT_FDCWD = -100
_RENAME_EXCHANGE = 2      # linux/fs.h
_RENAME_SWAP = 0x00000002  # darwin sys/stdio.h
_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", errno.EINVAL), errno.EXDEV}


def _libc_function() -> Optional[Callable[[bytes, bytes], int]]:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        fn = libc.renamex_np
        fn.argtypes, fn.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
        return lambda a, b: fn(a, b, _RENAME_SWAP)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return lambda a, b: fn(_AT_FDCWD, a, _AT_FDCWD, b, _RENAME_EXCHANGE)
    return None


_EXCHANGE = _libc_function()


def exchange(a: Path, b: Path) -> bool:
    """Swap ``a`` and ``b`` atomically: True when swapped, False when this platform/filesystem cannot.

    Raises OSError for any other failure (both paths are then unchanged).
    """
    if _EXCHANGE is None:
        return False
    if _EXCHANGE(os.fsencode(str(a)), os.fsencode(str(b))) == 0:
        return True
    err = ctypes.get_errno()
    if err in _UNSUPPORTED:
        return False
    raise OSError(err, os.strerror(err), str(b))
