"""A bug in one handler must not take the radio down; SIGTERM must close cleanly."""
from __future__ import annotations

import logging
import os
import signal
import sys

import pytest

from conftest import pump
from pisdr import crashguard
from pisdr.qt import QTimer


def raise_and_catch(guard, exc: BaseException) -> None:
    try:
        raise exc
    except BaseException:  # noqa: BLE001 - feeding the hook exactly what Python would
        guard(*sys.exc_info())


def test_error_is_logged_with_traceback_and_shown_once(caplog, clock, monkeypatch):
    monkeypatch.setattr(crashguard, "time", clock)
    shown: list[str] = []
    guard = crashguard.ExceptionGuard(shown.append)
    with caplog.at_level(logging.ERROR, logger="pisdr.crash"):
        raise_and_catch(guard, ZeroDivisionError("division by zero"))
    assert guard.count == 1
    assert shown == ["Internal error: ZeroDivisionError: division by zero - details in pisdr.log"]
    assert "Traceback" in caplog.text and "raise_and_catch" in caplog.text


def test_repeating_error_does_not_flood_the_log_or_the_screen(caplog, clock, monkeypatch):
    """A timer that fails 20 times a second must not fill the SD card."""
    monkeypatch.setattr(crashguard, "time", clock)
    shown: list[str] = []
    guard = crashguard.ExceptionGuard(shown.append)
    with caplog.at_level(logging.ERROR, logger="pisdr.crash"):
        for _ in range(20 * 65):                       # 65 seconds at 20 Hz
            raise_and_catch(guard, KeyError("x"))
            clock.advance(0.05)
    assert guard.count == 1300
    assert len(caplog.records) == 2                    # once, then once a minute
    assert "more like it" in caplog.records[1].getMessage()
    assert len(shown) == 7                             # every 10 s


def test_different_errors_are_each_logged(caplog, clock, monkeypatch):
    monkeypatch.setattr(crashguard, "time", clock)
    guard = crashguard.ExceptionGuard()
    with caplog.at_level(logging.ERROR, logger="pisdr.crash"):
        raise_and_catch(guard, KeyError("a"))
        try:
            {}["b"]
        except KeyError:
            guard(*sys.exc_info())
    assert len(caplog.records) == 2


def test_broken_notifier_is_tolerated():
    def notify(text: str) -> None:
        raise RuntimeError("the window is gone")
    raise_and_catch(crashguard.ExceptionGuard(notify), ValueError("x"))


def test_ctrl_c_closes_instead_of_logging(caplog):
    closed: list[bool] = []
    guard = crashguard.ExceptionGuard(on_interrupt=lambda: closed.append(True))
    raise_and_catch(guard, KeyboardInterrupt())
    assert closed == [True] and guard.count == 0 and not caplog.records


def test_event_loop_survives_an_exception_in_a_slot(qapp, slot_errors):
    """Without the guard PyQt5 calls abort() here and the whole test run dies."""
    shown: list[str] = []
    guard = crashguard.install_excepthook(shown.append)
    after: list[int] = []
    QTimer.singleShot(0, lambda: 1 / 0)
    QTimer.singleShot(10, lambda: after.append(1))
    pump(qapp, 0.1)
    assert guard.count == 1 and after == [1]
    assert shown and shown[0].startswith("Internal error: ZeroDivisionError")
    assert slot_errors == []                           # handled by the guard, not leaked


@pytest.mark.parametrize("name", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_stop_signals_close_the_window(qapp, name):
    if not hasattr(signal, name) or sys.platform == "win32":
        pytest.skip("POSIX signals only")
    previous = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGTERM", "SIGINT", "SIGHUP")}
    closed: list[bool] = []
    wake = crashguard.install_signal_handlers(qapp, lambda: closed.append(True))
    try:
        os.kill(os.getpid(), getattr(signal, name))
        pump(qapp, 0.6)
        assert closed == [True]
    finally:
        wake.stop()
        for n, handler in previous.items():
            signal.signal(getattr(signal, n), handler)
