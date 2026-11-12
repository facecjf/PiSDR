"""Shared test setup: offscreen Qt, a throw-away config folder, no network, no hardware.

Run from ``software/``::

    python -m pip install -r requirements-dev.txt
    python -m pytest

Nothing here needs a radio, a Pi or GNU Radio. On a machine without GNU Radio a stand-in
(``tests/fakegr.py``) is installed so the flowgraph wiring can be tested too.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import time as _time     # the Clock class below has a method called time()
import types
from pathlib import Path

# Must be set before pisdr (and Qt) are imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SCRATCH = Path(tempfile.mkdtemp(prefix="pisdr-tests-"))
os.environ["PISDR_CONFIG_DIR"] = str(_SCRATCH / "config")
os.environ["PISDR_RECORDINGS_DIR"] = str(_SCRATCH / "recordings")
os.environ.pop("PISDR_AUDIO_DEVICE", None)

SOFTWARE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOFTWARE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakegr  # noqa: E402

if importlib.util.find_spec("gnuradio") is None:
    fakegr.install()

import pytest  # noqa: E402

from pisdr import config  # noqa: E402
from pisdr.qt import QtWidgets  # noqa: E402

needs_fake_gr = pytest.mark.skipif(not fakegr.is_fake(), reason="uses the GNU Radio stand-in")


@pytest.fixture(scope="session")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["pisdr-tests"])
    yield app


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Every test gets its own settings / memories / recordings folders."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "USER_CONFIG_DIR", cfg)
    for name, filename in (("SETTINGS_FILE", "settings.json"), ("MEMORIES_FILE", "memories.json"),
                           ("SCAN_LOG_FILE", "scan_log.json"), ("LOG_FILE", "pisdr.log"),
                           ("SPACEWX_FILE", "spacewx.json"), ("USER_TAGS_FILE", "tags.json")):
        monkeypatch.setattr(config, name, cfg / filename)
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(config, "BACKLIGHT_DIR", tmp_path / "no-backlight")
    return cfg


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The HF panel must never reach the internet from a test."""
    from pisdr import spacewx

    def refuse(url: str) -> str:
        raise OSError("network disabled in tests")
    monkeypatch.setattr(spacewx, "fetch_text", refuse)


@pytest.fixture(autouse=True)
def slot_errors(monkeypatch):
    """Fail the test if a Qt slot raised (PyQt5 would otherwise abort the whole run)."""
    errors: list[BaseException] = []
    monkeypatch.setattr(sys, "excepthook", lambda etype, exc, tb: errors.append(exc))
    yield errors
    assert not errors, f"exception inside a Qt slot: {errors[0]!r}"


class Clock:
    """Controllable replacement for the ``time`` module inside one pisdr module."""

    def __init__(self, wall: float = 1_700_000_000.0) -> None:
        self.mono = 1000.0
        self.wall = wall

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    # Pass-throughs so patched modules can still format times.
    gmtime = staticmethod(_time.gmtime)
    localtime = staticmethod(_time.localtime)
    strftime = staticmethod(_time.strftime)
    sleep = staticmethod(lambda seconds: None)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def state(qapp):
    from pisdr.state import RadioState
    return RadioState()


def pump(app, seconds: float = 0.05) -> None:
    """Run the Qt event loop for *seconds*."""
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.002)


_CLOSED_WINDOWS: list = []


@pytest.fixture
def window(qapp, monkeypatch):
    """The real main window on the simulated backend, 800x480, offscreen."""
    from pisdr.radio.backend_sim import SimBackend
    from pisdr.state import RadioState
    from pisdr.ui import settings_page
    from pisdr.ui.main_window import MainWindow

    launched: list = []
    # "Shut down Pi" must never power off the machine running the tests.
    monkeypatch.setattr(settings_page, "subprocess",
                        types.SimpleNamespace(Popen=lambda cmd, *a, **k: launched.append(cmd)))
    st = RadioState()
    st.load()
    backend = SimBackend(st, reason="--sim requested")
    win = MainWindow(st, backend, windowed=True)
    win.spacewx.stop()
    win.launched = launched
    backend.start()
    win.show()
    pump(qapp, 0.2)
    yield win
    win.close()
    pump(qapp, 0.05)
    _CLOSED_WINDOWS.append(win)     # late single-shot timers must still find their widgets
