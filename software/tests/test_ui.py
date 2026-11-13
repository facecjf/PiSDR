"""The whole window on the simulator, offscreen: every page, theme and settings row, plus a
long random session. Any exception in any handler fails the test (see ``slot_errors``)."""
from __future__ import annotations

import json
import random
import time

import numpy as np
import pytest

from conftest import pump
from pisdr import config, crashguard, storage
from pisdr.memories import Memory
from pyqtgraph.Qt import QtTest

from pisdr.qt import Qt, QtCore, QtGui
from pisdr.ui import style
from test_radio import write_wav
from test_state import assert_sane


def press(win, key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    QtTest.QTest.keyClick(win, key, modifiers)


def image_stats(win) -> tuple[int, float]:
    """(number of distinct colours, mean brightness) of a screenshot: catches blank pages."""
    image = win.grab().toImage().convertToFormat(QtGui.QImage.Format.Format_RGB32)
    ptr = image.constBits()
    ptr.setsize(image.width() * image.height() * 4)
    pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(-1, 4)[::7, :3]
    return len(np.unique(pixels, axis=0)), float(pixels.mean())


# --------------------------------------------------------------------------- pages
def test_window_is_the_size_of_the_panel(window):
    assert (window.width(), window.height()) == (config.SCREEN_W, config.SCREEN_H)
    assert window.stack.currentIndex() == window.PAGE_MAIN
    colours, brightness = image_stats(window)
    assert colours > 50 and brightness > 5


@pytest.mark.parametrize("page", [*range(8), 10, 11])   # 8 and 9 hold the dongle: tested on their own
def test_every_page_opens_draws_and_returns(window, qapp, page):
    window.show_page(page)
    pump(qapp, 0.05)
    assert window.stack.currentIndex() == page
    labels = window.current_page().softkey_labels()
    assert len(labels) == 4 and all(isinstance(text, str) for text in labels)
    assert [b.text() for b in window.softkeys.buttons] == labels
    colours, _ = image_stats(window)
    assert colours > 8, "page is blank"
    # Every page reacts to every control without raising, then K4 / Esc gets back out.
    for index in range(4):
        window.key_long_pressed(index)
        window.show_page(page)
    window.knob_turned(1)
    window.knob_turned(-3)
    window.encoder_pushed()
    window.show_page(page)
    press(window, Qt.Key.Key_Escape)
    assert window.stack.currentIndex() == window.PAGE_MAIN


def test_spectrum_only_runs_while_it_is_visible(window):
    """The waterfall costs CPU the audio needs; it is paused on every other page."""
    assert window.backend._spectrum_on
    window.show_page(window.PAGE_SETTINGS)
    assert not window.backend._spectrum_on
    window.show_main()
    assert window.backend._spectrum_on


@pytest.mark.parametrize("theme", config.THEME_NAMES)
def test_every_theme_draws_every_page(window, qapp, theme):
    window.state.set_theme(theme)
    assert style.ACCENT == style.THEMES[theme]["ACCENT"]
    for page in (window.PAGE_MAIN, window.PAGE_SETTINGS, window.PAGE_BAND, window.PAGE_MEMORIES):
        window.show_page(page)
        pump(qapp, 0.03)
        colours, brightness = image_stats(window)
        assert colours > 8
    window.state.set_hf_panel("ON")
    window.show_main()
    pump(qapp, 0.05)
    assert window.main_page.hf_panel.isVisible()


def test_theme_list_matches_the_stylesheet_themes():
    assert tuple(style.THEMES) == config.THEME_NAMES
    assert set(style.PALETTES) >= set(config.WATERFALL_PALETTES) - {"THEME"}


# --------------------------------------------------------------------------- settings
def rows(window):
    return list(enumerate(window.settings_page.rows))


def test_every_settings_row_shows_a_value_and_survives_every_control(window, qapp):
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.quit_requested.disconnect()
    assert len(page.rows) > 40
    for index, row in rows(window):
        if row.label.startswith(("Network mode", "ADS-B")):
            continue                                  # covered on their own (below, test_adsb.py)
        page.selected = index
        page.refresh()
        assert isinstance(row.get(), str), row.label
        for detents in (1, 1, -1, -5, 12, 0, 0):      # knob both ways, then CHANGE twice
            page.on_knob(detents) if detents else page.on_softkey(2)
            page.selected = index
            assert isinstance(row.get(), str), row.label
        pump(qapp, 0.005)
        assert_sane(window.state)
        if window.stack.currentIndex() != window.PAGE_SETTINGS:
            window.show_page(window.PAGE_SETTINGS)     # "Recordings" / "Scan log" open a page
    assert window.launched == [["systemctl", "poweroff"]]


def test_settings_tabs_follow_the_selection(window):
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    seen = []
    for _ in range(len(page.rows)):
        seen.append(page.stack.currentIndex())
        window.key_pressed(1)                          # DOWN
    assert sorted(set(seen)) == list(range(len(page.TABS)))
    assert seen == sorted(seen)                        # tabs are visited in order
    assert page.selected == 0                          # and it wraps
    for tab in range(len(page.TABS)):
        page.tab_buttons[tab].click()
        assert page.stack.currentIndex() == tab and page.tab_buttons[tab].isChecked()
    for widget in page.widgets:
        assert widget.label.text()


def test_settings_rows_fit_on_the_screen(window, qapp):
    """A row pushed below the bottom edge cannot be reached on a touch screen."""
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    for tab in range(len(page.TABS)):
        page._select_tab(tab)
        pump(qapp, 0.03)
        for index, widget in enumerate(page.widgets):
            if page.row_tab[index] != tab:
                continue
            bottom = widget.mapTo(window, QtCore.QPoint(0, widget.height())).y()
            assert bottom <= window.softkeys.y(), f"{page.rows[index].label!r} is hidden behind the soft keys"
            assert widget.height() >= 30, f"{page.rows[index].label!r} is too small to tap"


def test_shutdown_needs_two_presses_and_stops_a_recording(window, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda d: 10_000_000_000)
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.selected = next(i for i, r in rows(window) if r.label == "Shut down Pi")
    window.state.set_recording(True)
    window.state.set_volume(23)
    window.key_pressed(2)
    assert window.launched == [] and window.toast.text() == "Press again to confirm"
    window.key_pressed(2)
    assert window.launched == [["systemctl", "poweroff"]]
    assert not window.state.recording
    assert json.loads(config.SETTINGS_FILE.read_text())["volume"] == 23


def test_network_mode_locks_the_ui_until_stopped(window, qapp):
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.selected = next(i for i, r in rows(window) if r.label.startswith("Network mode"))
    window.key_pressed(2)
    assert window.state.network_mode and window.stack.currentIndex() == window.PAGE_NETWORK
    assert str(config.RTL_TCP_PORT) in window.network_page.address.text()
    for action in (window.show_main, lambda: window.key_pressed(0), lambda: window.key_long_pressed(2),
                   lambda: window.knob_turned(5), lambda: press(window, Qt.Key.Key_Escape)):
        action()
        assert window.stack.currentIndex() == window.PAGE_NETWORK
    window.key_pressed(3)                              # STOP
    assert not window.state.network_mode and window.stack.currentIndex() == window.PAGE_MAIN


# --------------------------------------------------------------------------- everyday use
def test_keypad_entry(window):
    window.encoder_held()
    assert window.stack.currentIndex() == window.PAGE_KEYPAD
    for key in "146.52":
        window.keypad_page.key(key)
    window.key_pressed(0)                              # MHz
    assert window.state.vfo_hz == 146_520_000 and window.stack.currentIndex() == window.PAGE_MAIN

    for key in (Qt.Key.Key_7, Qt.Key.Key_0, Qt.Key.Key_7, Qt.Key.Key_4):   # typing opens the keypad
        press(window, key)
    window.key_pressed(1)                              # kHz
    assert window.state.vfo_hz == 7_074_000

    window.encoder_held()
    for key in "99999":
        window.keypad_page.key(key)
    window.key_pressed(0)
    assert window.state.vfo_hz == 7_074_000 and "Out of range" in window.toast.text()
    window.key_pressed(0)                              # nothing typed after CLR
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN


def test_band_and_mode_pickers(window):
    state = window.state
    window.key_pressed(0)
    assert window.stack.currentIndex() == window.PAGE_BAND
    window.band_page.buttons[3].click()
    assert state.band_index == 3 and window.stack.currentIndex() == window.PAGE_MAIN
    assert state.vfo_hz == state.bands[3].default_hz
    window.key_pressed(1)
    window.mode_page.buttons[config.MODES.index("LSB")].click()
    assert state.mode == "LSB" and window.stack.currentIndex() == window.PAGE_MAIN


def test_keyboard_stands_in_for_the_knob_and_keys(window):
    state = window.state
    state.set_knob_accel(False)
    state.set_step_hz(1000)
    start = state.vfo_hz
    press(window, Qt.Key.Key_Up)
    press(window, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert state.vfo_hz == start + 11_000
    step = state.step_hz
    press(window, Qt.Key.Key_Return)                   # knob push: next step size
    assert state.step_hz != step
    press(window, Qt.Key.Key_F3)                       # K3: knob function
    assert state.knob_function == "VOLUME"
    volume = state.volume
    press(window, Qt.Key.Key_Down)
    assert state.volume == volume - config.VOLUME_STEP
    press(window, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)        # hold K3: settings
    assert window.stack.currentIndex() == window.PAGE_SETTINGS


def test_scanning_from_the_front_panel(window, qapp):
    state = window.state
    state.select_band(next(i for i, b in enumerate(state.bands) if b.mode == "NFM"))
    window.key_pressed(3)                              # FNC: second toolbar layer
    window.key_long_pressed(0)                         # hold MEM: scan
    assert state.scanning
    assert [b.text() for b in window.softkeys.buttons] == ["HOLD", "SKIP", "MEM", "STOP"]
    assert "SCAN:BAND" in window.main_page.vfo_bar.text()
    window.key_pressed(0)
    assert state.scan_held and window.softkeys.buttons[0].text() == "RESUME"
    window.key_pressed(0)
    assert not state.scan_held
    window.knob_turned(1)                              # touching the knob takes over
    assert not state.scanning
    assert [b.text() for b in window.softkeys.buttons][:2] == ["MEM", "SCAN:BAND"]
    window.key_pressed(1)                              # SCAN
    assert state.scanning
    window.key_long_pressed(3)                         # hold STOP stops it as well
    assert not state.scanning
    window.key_pressed(1)
    window.show_page(window.PAGE_SETTINGS)             # leaving the main page stops it too
    assert not state.scanning


def tap(widget, point) -> None:
    """Click at *point* (in *widget* coordinates) on whatever child is under it, like a finger."""
    target = widget.childAt(point) or widget
    QtTest.QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            target.mapFrom(widget, point))


def test_tapping_the_spectrum_tunes_there(window, qapp):
    state = window.state
    state.cycle_snap_mode(config.SNAP_MODES.index("OFF") - config.SNAP_MODES.index(state.snap_mode))
    state.tune_to(146_000_000, "NFM", 5000)
    pump(qapp, 0.15)
    spectrum = window.main_page.spectrum
    centre, span = window.backend.view_center_hz, window.backend.view_span_hz
    for fraction in (0.2, 0.5, 0.85):
        for y in (spectrum.height() // 4, spectrum.height() * 3 // 4):       # trace, then waterfall
            state.set_vfo(centre)
            tap(spectrum, QtCore.QPoint(int(spectrum.width() * fraction), y))
            expected = centre - span / 2 + fraction * span
            assert abs(state.vfo_hz - expected) < 0.06 * span, f"tap at {fraction:.0%} tuned {state.vfo_hz}"
            assert state.vfo_hz % 5000 == 0, "tap did not land on the tuning step grid"


def test_tap_snaps_to_the_signal_under_the_finger(window, qapp):
    state = window.state
    state.tune_to(146_000_000, "NFM", 5000)
    assert state.snap_mode == "TUNE"
    spectrum = window.main_page.spectrum
    vb = spectrum.spec_plot.getViewBox()
    for _ in range(3):
        pump(qapp, 0.6)                                # let detection run on a few frames
        others = [s for s in spectrum._signals if not s.lo_hz - 20_000 <= state.vfo_hz <= s.hi_hz + 20_000]
        assert others, "the simulator's stations were not detected"
        sig = others[0]
        scene = vb.mapViewToScene(QtCore.QPointF(sig.centroid_hz / 1e6 + 0.004, 0.0))   # 4 kHz off
        point = spectrum.glw.mapFromScene(QtCore.QPointF(scene.x(), vb.sceneBoundingRect().center().y()))
        tap(spectrum, spectrum.glw.mapTo(spectrum, point))
        assert sig.lo_hz <= state.vfo_hz <= sig.hi_hz
        assert abs(state.vfo_hz - sig.centroid_hz) < 2500, "snap did not centre on the signal"


def test_zoom_and_seek_buttons(window, qapp):
    state = window.state
    state.tune_to(146_000_000, "NFM", 5000)
    pump(qapp, 0.6)
    spectrum = window.main_page.spectrum
    buttons = {b.text(): b for b in spectrum.controls.findChildren(type(window.softkeys.buttons[0]))}
    assert len(buttons) == 4
    plus = next(b for text, b in buttons.items() if text.strip() == "+")
    minus = next(b for text, b in buttons.items() if text.strip() in ("-", "\u2212"))
    for expected in (2, 4, 8):
        plus.click()
        pump(qapp, 0.05)
        assert state.spectrum_zoom == expected
        assert window.backend.view_span_hz == pytest.approx(config.SAMPLE_RATE / expected)
    minus.click()
    assert state.spectrum_zoom == 4
    state.step_spectrum_zoom(-5)
    pump(qapp, 0.6)
    before = state.vfo_hz
    spectrum.seek(+1)
    assert state.vfo_hz > before, "seek up found nothing although stations are on screen"
    up = state.vfo_hz
    spectrum.seek(-1)
    assert state.vfo_hz < up


def test_memories_page_saves_and_recalls(window, qapp):
    state = window.state
    state.tune_to(162_550_000, "NFM", 25_000)
    window.key_pressed(3)                              # FNC, then MEM
    window.key_pressed(0)
    assert window.stack.currentIndex() == window.PAGE_MEMORIES
    labels = window.memories_page.softkey_labels()
    save = next(i for i, text in enumerate(labels) if "SAVE" in text.upper())
    window.key_pressed(save)
    assert [(m.vfo_hz, m.mode) for m in state.memories] == [(162_550_000, "NFM")]
    state.memories += [Memory(f"m{i}", 100_000_000 + i * 200_000, "WFM", 100_000, group="FM") for i in range(30)]
    state.changed.emit("memories")
    window.show_page(window.PAGE_MEMORIES)
    for _ in range(40):                                # walk past the end of several pages
        window.knob_turned(1)
    pump(qapp, 0.02)
    window.encoder_pushed()
    assert state.mode in ("WFM", "NFM")


def test_recordings_page_lists_plays_and_deletes(window, qapp, monkeypatch):
    first = write_wav(config.RECORDINGS_DIR / "pisdr_20250115_201500_6840000_USB.wav", seconds=1.0)
    write_wav(config.RECORDINGS_DIR / "other.wav")
    (config.RECORDINGS_DIR / "broken.wav").write_bytes(b"RIFF")
    window.show_page(window.PAGE_RECORDINGS)
    page = window.recordings_page
    pump(qapp, 0.05)
    assert len(page.items) == 3
    assert "free" in page.title.text()
    index = next(i for i, p in enumerate(page.items) if str(p) == first)
    assert page.row_text(page.items[index]) == ("01-15 20:15   6.8400 MHz USB", "0:01  0.1 MB")
    page.selected = index
    window.key_pressed(0)                              # PLAY
    assert window.state.playing == first and window.softkeys.buttons[0].text() == "STOP"
    assert "PLAY" in window.topbar_left.text()
    window.key_pressed(1)
    assert window.softkeys.buttons[1].text() == "CONFIRM"
    window.key_pressed(1)
    assert not (config.RECORDINGS_DIR / "pisdr_20250115_201500_6840000_USB.wav").exists()
    assert window.state.playing == "" and len(page.items) == 2
    page.selected = next(i for i, p in enumerate(page.items) if p.name == "broken.wav")
    window.key_pressed(0)
    assert window.state.playing == "" and "Cannot open" in window.toast.text()
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_SETTINGS or window.stack.currentIndex() == window.PAGE_MAIN


def test_scan_log_page_tunes_and_locks(window):
    state = window.state
    state.add_scan_hit({"t": 1_700_000_000, "vfo_hz": 155_340_000, "mode": "NFM", "name": "EMS",
                        "peak_db": -48.2, "src": "band", "dur_s": 12.5})
    window.show_page(window.PAGE_SCAN_LOG)
    page = window.scan_log_page
    assert len(page.items) == 1 and "155.3400 NFM   EMS" in page.row_text(page.items[0])[0]
    window.key_pressed(1)                              # LOCK
    assert state.is_locked_out(155_340_000)
    window.key_pressed(0)                              # TUNE
    assert state.vfo_hz == 155_340_000 and window.stack.currentIndex() == window.PAGE_MAIN


def test_physical_key_press_flashes_its_label(window, qapp):
    window.key_pressed(2)
    assert window.softkeys.buttons[2].isDown()
    pump(qapp, 0.25)
    assert not any(button.isDown() for button in window.softkeys.buttons)


def test_long_messages_stay_on_screen(window):
    window.show_message("x" * 300)
    assert window.toast.isVisible()
    assert window.toast.x() >= 0 and window.toast.x() + window.toast.width() <= window.width()
    window.show_message("short")
    assert window.toast.width() < 200


def test_screen_dims_when_idle_and_wakes_on_touch(window, tmp_path, monkeypatch):
    from pisdr.ui import main_window
    panel = tmp_path / "backlight" / "10-0045"
    panel.mkdir(parents=True)
    (panel / "max_brightness").write_text("255\n")
    (panel / "brightness").write_text("255\n")
    monkeypatch.setattr(config, "BACKLIGHT_DIR", tmp_path / "backlight")
    window.backlight = main_window.Backlight()
    state = window.state
    state.set_brightness(80)
    assert int((panel / "brightness").read_text()) == 204
    state.cycle_dim_after(1)                           # 30 s
    window._last_activity = time.monotonic() - 31
    window._check_dim()
    assert int((panel / "brightness").read_text()) == round(255 * config.DIM_LEVEL_PCT / 100)
    window.knob_turned(0)
    assert int((panel / "brightness").read_text()) == 204


# --------------------------------------------------------------------------- shutdown and failures
def test_closing_saves_settings_and_stops_everything(window, qapp, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda d: 10_000_000_000)
    state = window.state
    state.tune_to(14_074_000, "USB", 100)
    state.set_recording(True)
    window.scanner.start()
    stopped: list[bool] = []
    monkeypatch.setattr(window.backend, "_hw_set_recording", lambda on: stopped.append(on))
    press(window, Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    assert not window.isVisible()
    assert stopped == [False], "recording was not finalised on exit"
    assert not state.scanning and not window.backend.running
    saved = json.loads(config.SETTINGS_FILE.read_text())
    assert (saved["vfo_hz"], saved["mode"]) == (14_074_000, "USB")
    window.close()                                     # a second request is harmless
    assert stopped == [False]


def test_a_broken_handler_shows_a_message_and_the_radio_keeps_running(window, qapp, monkeypatch, slot_errors):
    guard = crashguard.install_excepthook(window.show_message, window.close)

    def boom(detents: int) -> bool:
        raise RuntimeError("simulated bug in a page")
    window.show_page(window.PAGE_BAND)
    monkeypatch.setattr(window.band_page, "on_softkey", boom)
    QtTest.QTest.mouseClick(window.softkeys.buttons[1], Qt.MouseButton.LeftButton)
    pump(qapp, 0.05)
    assert guard.count == 1
    assert window.toast.text().startswith("Internal error: RuntimeError: simulated bug in a page")
    frames: list = []
    window.backend.spectrum_ready.connect(frames.append)
    window.show_main()
    pump(qapp, 0.3)
    assert frames, "spectrum stopped after the error"
    assert slot_errors == []


# --------------------------------------------------------------------------- random session
@pytest.mark.parametrize("seed", range(4))
def test_random_session(window, qapp, seed, monkeypatch):
    """Hammer the controls like an impatient user for a few thousand actions.

    Not a check of any particular result: the pass condition is that nothing raises, the
    state stays legal throughout, and the window still draws at the end.
    """
    monkeypatch.setattr(storage, "free_bytes", lambda d: 10_000_000_000)
    rng = random.Random(seed)
    state = window.state
    window.settings_page.quit_requested.disconnect()   # "Exit PiSDR" would end the session
    state.import_preset_memories()
    write_wav(config.RECORDINGS_DIR / "pisdr_20250115_201500_6840000_USB.wav")
    spectrum = window.main_page.spectrum
    keys = [Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Escape, Qt.Key.Key_5,
            Qt.Key.Key_Period, Qt.Key.Key_F1, Qt.Key.Key_F2, Qt.Key.Key_F3, Qt.Key.Key_F4]

    def tap_spectrum() -> None:
        tap(spectrum, QtCore.QPoint(rng.randrange(spectrum.width()), rng.randrange(spectrum.height())))

    def tap_anywhere() -> None:
        tap(window, QtCore.QPoint(rng.randrange(window.width()), rng.randrange(window.height())))

    actions = [
        (30, lambda: window.key_pressed(rng.randrange(4))),
        (8, lambda: window.key_long_pressed(rng.randrange(4))),
        (25, lambda: window.knob_turned(rng.choice([-20, -3, -1, 1, 1, 2, 15]))),
        (10, window.encoder_pushed),
        (2, window.encoder_held),
        (10, lambda: press(window, rng.choice(keys),
                           rng.choice([Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier]))),
        (6, tap_spectrum),
        (12, tap_anywhere),
        (4, lambda: window.show_page(rng.choice([*range(8), window.PAGE_LOCATION, window.PAGE_MENU]))),
        (1, window._enter_adsb),
        (3, lambda: spectrum.seek(rng.choice([-1, 1]))),
        (2, lambda: state.set_signal_db(rng.uniform(-120, -20))),
        (2, lambda: window.scanner._tick()),
        (1, lambda: window.rec_timer.check(time.time() + rng.randrange(86_400))),
        (1, lambda: state.set_vfo(rng.choice([config.FREQ_MIN, config.FREQ_MAX, rng.randrange(10 ** 9)]))),
    ]
    weights = [w for w, _ in actions]
    for step in range(2500):
        rng.choices(actions, weights)[0][1]()
        if step % 25 == 0:
            pump(qapp, 0.004)
            assert_sane(state)
            assert window.softkeys.buttons[3].text() != "" or state.network_mode
            assert state.adsb_mode == (window.stack.currentIndex() == window.PAGE_ADSB)
            if state.adsb_mode and step % 100 == 0:
                window.backend.poll_adsb()             # the map gets traffic to draw and select
    if state.dongle_lent:
        window.key_pressed(3)
    window.show_main()
    pump(qapp, 0.1)
    assert_sane(state)
    assert not window.launched or window.launched == [["systemctl", "poweroff"]] * len(window.launched)
    colours, _ = image_stats(window)
    assert colours > 50
    state.save()
    reloaded = type(state)()
    reloaded.load()
    assert_sane(reloaded)


def test_screenshot_tool_writes_every_image(tmp_path):
    """The documentation images come from this script; a renamed method must not break it silently."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    tool = Path(__file__).resolve().parent.parent / "tools" / "screenshots.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PISDR_SHOT_SETTLE="0.05")
    result = subprocess.run([sys.executable, "-B", str(tool), str(tmp_path / "shots")],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr[-2000:]
    names = sorted(path.name for path in (tmp_path / "shots").iterdir())
    assert names == ["ui_adsb_map.png", "ui_band_picker.png", "ui_hf_conditions.png", "ui_main_sim.png",
                     "ui_memories.png", "ui_menu.png", "ui_settings.png", "ui_theme_green.png",
                     "ui_theme_inferno.png", "ui_zoom.png"]
    assert all((tmp_path / "shots" / name).stat().st_size > 5000 for name in names)
