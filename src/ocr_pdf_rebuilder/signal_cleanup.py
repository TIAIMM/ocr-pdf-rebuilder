"""Signal handling that lets process controllers reclaim OCR descendants."""

from __future__ import annotations

from contextlib import contextmanager
import signal


@contextmanager
def termination_raises_keyboard_interrupt():
    previous = {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
