"""Keep the radio running when a handler raises, and close cleanly on SIGTERM / Ctrl-C.

PyQt5 aborts the whole process (core dump, no cleanup) when a slot raises and nothing has
replaced ``sys.excepthook``. For a radio that means one bug in a rarely used button stops
the audio and loses a recording in progress. :func:`install_excepthook` logs the traceback
to ``pisdr.log``, tells the user once, and lets the event loop carry on.

:func:`install_signal_handlers` turns SIGTERM / SIGINT / SIGHUP (shutdown, reboot, Ctrl-C,
session logout) into a normal window close, so settings are saved and a recording gets a
valid WAV header.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
import traceback
from typing import Callable

from .qt import QTimer

log = logging.getLogger("pisdr.crash")

LOG_REPEAT_S = 60.0       # the same error is logged in full at most this often
NOTIFY_EVERY_S = 10.0     # and shown on screen at most this often


class ExceptionGuard:
    """``sys.excepthook`` replacement. *notify* gets a one-line text for the screen."""

    def __init__(self, notify: Callable[[str], None] | None = None,
                 on_interrupt: Callable[[], None] | None = None) -> None:
        self.notify = notify
        self.on_interrupt = on_interrupt
        self.count = 0
        self._seen: dict[tuple, list] = {}     # where -> [repeats since last log, last log time]
        self._last_notify = 0.0

    def __call__(self, exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            if self.on_interrupt is not None:
                self.on_interrupt()
            return
        self.count += 1
        now = time.monotonic()
        frames = traceback.extract_tb(tb)
        where = (exc_type.__name__, frames[-1].filename, frames[-1].lineno) if frames else (exc_type.__name__,)
        seen = self._seen.setdefault(where, [0, None])
        if seen[1] is None or now - seen[1] >= LOG_REPEAT_S:
            repeats = f" (and {seen[0]} more like it)" if seen[0] else ""
            log.error("unhandled error%s", repeats, exc_info=(exc_type, exc, tb))
            seen[0], seen[1] = 0, now
        else:
            seen[0] += 1      # e.g. a timer that fails 20 times a second: don't fill the card
        if self.notify is not None and now - self._last_notify >= NOTIFY_EVERY_S:
            self._last_notify = now
            try:
                self.notify(f"Internal error: {exc_type.__name__}: {exc} - details in pisdr.log")
            except Exception:   # the UI itself may be what is broken
                pass


def install_excepthook(notify: Callable[[str], None] | None = None,
                       on_interrupt: Callable[[], None] | None = None) -> ExceptionGuard:
    guard = ExceptionGuard(notify, on_interrupt)
    sys.excepthook = guard
    return guard


def install_signal_handlers(app, close: Callable[[], None]) -> QTimer:
    """Call *close* (on the Qt thread) when the process is asked to stop.

    Returns the wake-up timer; keep a reference to it. Python only runs signal handlers
    between bytecodes, and Qt's event loop can sit in C++ for a long time, so the timer
    gives the interpreter a chance four times a second.
    """
    def handler(signum, _frame) -> None:
        log.info("signal %d received - closing", signum)
        QTimer.singleShot(0, close)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)
    wake = QTimer(app)
    wake.timeout.connect(lambda: None)
    wake.start(250)
    return wake
