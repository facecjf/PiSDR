"""The main screen in piHPSDR's layout: VFO bar and its indicators, the meter, MENU / HIDE,
the sliders area, the toolbar layers behind the FNC key, and the menu page."""
from __future__ import annotations

import pytest

from conftest import pump
from pisdr import config, storage
from pisdr.qt import Qt, QtCore, QtGui, QtWidgets
from pisdr.state import RadioState
from pisdr.ui import sliders, vfo_bar
from pisdr.ui.meter import s_units
from pyqtgraph.Qt import QtTest
from test_state import assert_sane
from test_ui import image_stats, tap


def keys(window) -> list[str]:
    return [button.text() for button in window.softkeys.buttons]


def drag_to(widget, point: QtCore.QPoint) -> None:
    """Move a finger that is already down on *widget* to *point*."""
    pos = QtCore.QPointF(point)
    event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, pos, QtCore.QPointF(widget.mapToGlobal(point)),
                              Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(widget, event)


def layer(window, number: int) -> None:
    """Press FNC until toolbar layer *number* (1-based) is showing."""
    for _ in range(len(window.main_page.TOOLBAR)):
        if window.main_page.layer == number - 1:
            return
        window.key_pressed(3)
    raise AssertionError(f"no toolbar layer {number}")


# --------------------------------------------------------------------------- state: VFO B
def test_vfo_b_swaps_copies_and_is_saved(state):
    state.tune_to(146_520_000, "NFM", 5000)
    state.copy_vfo_to_b()
    assert state.vfo_b == {"vfo_hz": 146_520_000, "mode": "NFM", "step_hz": 5000}
    state.tune_to(7_074_000, "USB", 100)
    state.swap_vfo()
    assert (state.vfo_hz, state.mode, state.step_hz) == (146_520_000, "NFM", 5000)
    assert state.vfo_b == {"vfo_hz": 7_074_000, "mode": "USB", "step_hz": 100}
    state.swap_vfo()
    assert (state.vfo_hz, state.mode, state.step_hz) == (7_074_000, "USB", 100)
    state.set_sliders_visible(False)
    state.save()
    again = RadioState()
    again.load()
    assert again.vfo_b == {"vfo_hz": 146_520_000, "mode": "NFM", "step_hz": 5000}
    assert again.sliders_visible is False
    assert_sane(again)


@pytest.mark.parametrize("junk", [None, 7, "x", [], {}, {"vfo_hz": "x"}, {"vfo_hz": -5, "mode": "NFM"},
                                  {"vfo_hz": 7e6, "mode": "nope", "step_hz": 100},
                                  {"vfo_hz": 10 ** 12, "mode": "AM", "step_hz": 0}])
def test_a_damaged_vfo_b_falls_back_to_the_tuned_frequency(state, junk):
    import json
    state.tune_to(14_074_000, "USB", 100)
    state.save()
    saved = json.loads(config.SETTINGS_FILE.read_text())
    saved["vfo_b"] = junk
    saved["sliders_visible"] = "maybe"
    config.SETTINGS_FILE.write_text(json.dumps(saved))
    again = RadioState()
    again.load()
    assert_sane(again)
    again.swap_vfo()                                   # and it can be used straight away
    assert_sane(again)


# --------------------------------------------------------------------------- VFO bar
def test_filter_width_and_s_units_text():
    assert [vfo_bar.filter_text(hz) for hz in (500, 2400, 12_500, 200_000)] == ["500", "2.4k", "12.5k", "200k"]
    assert [s_units(db) for db in (-140, -127, -121, -73, -72.6, -53, -13)] == [
        "S0", "S0", "S1", "S9", "S9", "S9+20", "S9+60"]


def test_indicators_follow_the_receiver(state):
    state.tune_to(146_520_000, "NFM", 5000)
    state.set_agc(False)
    state.set_dsp_nr_level(0)
    by_key = {ind.key: ind for ind in vfo_bar.VfoBar(state).indicators()}
    assert by_key["mode"].text == f"NFM {vfo_bar.filter_text(state.filter_hz)}" and by_key["mode"].kind == "on"
    assert (by_key["agc"].kind, by_key["nr"].kind, by_key["rec"].kind) == ("off", "off", "off")
    assert by_key["step"].text == "Step 5 kHz" and by_key["knob"].text == "Knob TUNE"
    assert by_key["gain"].text == f"RF {state.gain_db:.0f}dB"

    state.set_agc(True)
    state.set_dsp_nr_level(7)
    state.set_squelch(-60)
    state.set_knob_function("SQUELCH")
    by_key = {ind.key: ind for ind in vfo_bar.VfoBar(state).indicators()}
    assert (by_key["agc"].kind, by_key["nr"].text, by_key["nr"].kind) == ("on", "NR7", "on")
    assert (by_key["squelch"].text, by_key["squelch"].kind) == ("SQL-60", "on")
    assert by_key["knob"].text == "Knob SQL" and by_key["gain"].text == "RF AGC"
    state.set_mode("CW")
    assert "Hz" in vfo_bar.top_indicators(state)[0][0].text


def test_every_indicator_that_opens_settings_names_a_real_row(window):
    labels = {row.label for row in window.settings_page.rows}
    state = window.state
    state.arm_timer(True)
    assert state.timer_armed
    targets = [ind.action.partition(":")[2] for ind in window.main_page.vfo_bar.indicators()
               if ind.action.startswith("settings:")]
    assert "Timer" in targets and len(targets) >= 10
    assert set(targets) <= labels


def test_vfo_bar_shows_the_frequency_and_fits_the_screen(window, qapp):
    state = window.state
    bar = window.main_page.vfo_bar
    for hz in (config.FREQ_MIN, 7_074_000, 146_520_000, 1_090_000_000, config.FREQ_MAX):
        state.set_vfo(hz)
        pump(qapp, 0.03)
        cells = sorted(bar._digits, key=lambda cell: cell[0].x())
        assert [weight for _rect, weight in cells] == sorted((w for _r, w in cells), reverse=True)
        assert cells[0][0].x() >= 24, "the digits run into the A label"
        assert cells[-1][0].right() < bar.dial_b_rect().x()
    # Indicator touch bands never overlap, on either line.
    for line in (vfo_bar._TOP_BAND, vfo_bar._BOTTOM_BAND):
        rects = sorted((r for r, _ind in bar._hits if r.y() == line[0]), key=lambda r: r.x())
        assert len(rects) >= 5
        assert all(a.right() <= b.x() for a, b in zip(rects, rects[1:]))
        assert rects[-1].right() <= bar.width()
    assert bar.width() + window.main_page.meter.width() + window.main_page.menu_btn.width() <= window.width()


def test_tapping_a_digit_sets_the_step_and_holding_opens_the_keypad(window, qapp):
    state = window.state
    bar = window.main_page.vfo_bar
    state.tune_to(146_520_000, "NFM", 5000)
    state.set_knob_function("VOLUME")
    pump(qapp, 0.03)
    tap(window, bar.mapTo(window, bar.digit_rect(1000).center()))
    assert state.step_hz == 1000 and state.knob_function == "TUNE"
    tap(window, bar.mapTo(window, bar.digit_rect(100_000_000).center()))     # no such step
    assert state.step_hz == 1000

    point = bar.digit_rect(10_000).center()
    QtTest.QTest.mousePress(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    pump(qapp, vfo_bar._HOLD_MS / 1000 + 0.15)
    assert window.stack.currentIndex() == window.PAGE_KEYPAD
    QtTest.QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert state.step_hz == 1000, "the release after a long press also counted as a tap"


def test_tapping_vfo_b_swaps_and_stops_a_scan(window, qapp):
    state = window.state
    bar = window.main_page.vfo_bar
    state.tune_to(146_520_000, "NFM", 5000)
    state.copy_vfo_to_b()
    state.tune_to(162_550_000, "NFM", 25_000)
    window.scanner.start()
    assert state.scanning
    tap(window, bar.mapTo(window, bar.dial_b_rect().center()))
    assert not state.scanning
    assert state.vfo_hz == 146_520_000 and state.vfo_b["vfo_hz"] == 162_550_000
    assert "A<>B" in window.toast.text()


def test_tapping_an_indicator_goes_to_its_control(window, qapp):
    state = window.state
    bar = window.main_page.vfo_bar
    page = window.settings_page
    pump(qapp, 0.03)

    def tap_indicator(key: str) -> None:
        window.show_main()
        pump(qapp, 0.03)
        rect = bar.indicator_rect(key)
        assert rect is not None, f"{key} is not on screen"
        tap(window, bar.mapTo(window, rect.center()))

    for key, label in (("agc", "Tuner AGC"), ("nr", "Noise reduction"), ("notch", "Notch"),
                       ("bias", "Bias tee (4.5 V)"), ("rec", "Record now")):
        tap_indicator(key)
        assert window.stack.currentIndex() == window.PAGE_SETTINGS, key
        assert page.rows[page.selected].label == label
        assert page.stack.currentIndex() == page.row_tab[page.selected], "the row's tab is not showing"
    tap_indicator("mode")
    assert window.stack.currentIndex() == window.PAGE_MODE
    tap_indicator("band")
    assert window.stack.currentIndex() == window.PAGE_BAND

    step, zoom = state.step_hz, state.spectrum_zoom
    tap_indicator("step")
    assert state.step_hz != step and window.stack.currentIndex() == window.PAGE_MAIN
    tap_indicator("zoom")
    assert state.spectrum_zoom != zoom
    tap_indicator("volume")
    assert state.knob_function == "VOLUME"
    tap_indicator("volume")                            # again: back to tuning
    assert state.knob_function == "TUNE"
    tap_indicator("squelch")
    assert state.knob_function == "SQUELCH"
    tap_indicator("knob")
    assert state.knob_function != "SQUELCH"


# --------------------------------------------------------------------------- meter, MENU, HIDE
def test_meter_shows_s_units_clock_and_the_hf_chip(window, qapp):
    state = window.state
    meter = window.main_page.meter
    state.set_hf_panel("OFF")
    for _ in range(80):
        state.set_signal_db(-73.0)
    assert meter.smeter.reading() == ("S9", "-73 dB")
    for _ in range(80):
        state.set_signal_db(-33.0)
    assert meter.smeter.reading()[0] == "S9+40"
    window._update_topbar()
    assert len(meter.clock.text()) in (5, 6) and ":" in meter.clock.text()
    assert meter.chip.text() == window.hf_chip.text()
    assert not window.main_page.hf_panel.isVisible()
    tap(window, meter.chip.mapTo(window, meter.chip.rect().center()))
    assert state.hf_panel == "ON" and window.main_page.hf_panel.isVisible()


def test_the_status_line_is_only_on_the_other_pages(window, qapp):
    assert not window.topbar.isVisible()
    spectrum_height = window.main_page.spectrum.height()
    window.show_page(window.PAGE_SETTINGS)
    assert window.topbar.isVisible()
    window.show_main()
    pump(qapp, 0.03)
    assert not window.topbar.isVisible()
    assert window.main_page.spectrum.height() == spectrum_height


def test_hide_gives_the_sliders_room_to_the_spectrum_and_pops_up_a_slider(window, qapp):
    state = window.state
    page = window.main_page
    pump(qapp, 0.03)
    assert page.sliders.isVisible() and page.hide_btn.text() == "HIDE"
    height = page.spectrum.height()
    state.set_knob_function("VOLUME")
    window.knob_turned(1)
    assert not window.popup.isVisible(), "no pop-up while the slider itself is on screen"

    page.hide_btn.click()
    pump(qapp, 0.03)
    assert not page.sliders.isVisible() and page.hide_btn.text() == "SHOW"
    assert page.spectrum.height() >= height + sliders.HEIGHT
    volume = state.volume
    window.knob_turned(2)
    assert state.volume == volume + 2 * config.VOLUME_STEP
    assert window.popup.isVisible() and window.popup.level.name == "AF"
    assert window.popup.level.text() == str(state.volume)
    assert window.popup.geometry().intersected(window.rect()) == window.popup.geometry()
    window.popup._timer.timeout.emit()                 # (after a moment)
    assert not window.popup.isVisible()
    state.set_knob_function("TUNE")
    window.knob_turned(1)
    assert not window.popup.isVisible(), "tuning shows on the dial, not in a pop-up"
    state.set_knob_function("GAIN")
    window.knob_turned(1)
    assert window.popup.isVisible()
    window.show_page(window.PAGE_BAND)
    assert not window.popup.isVisible()

    window.show_main()
    page.hide_btn.click()
    assert page.sliders.isVisible() and state.sliders_visible


def test_menu_button_opens_the_menu(window, qapp):
    window.main_page.menu_btn.click()
    assert window.stack.currentIndex() == window.PAGE_MENU
    assert keys(window) == ["UP", "DOWN", "SELECT", "BACK"]
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN


# --------------------------------------------------------------------------- sliders
def test_levels_describe_and_set_the_three_controls(state):
    levels = sliders.levels(state)
    assert [level.name for level in levels.values()] == ["AF", "RF", "SQL"]
    levels["VOLUME"].set_fraction(0.25)
    assert state.volume == 25 and levels["VOLUME"].fraction() == pytest.approx(0.25)
    levels["GAIN"].set_fraction(1.0)
    assert state.gain_db == pytest.approx(config.GAIN_MAX_DB, abs=config.GAIN_STEP_DB)
    state.set_agc(True)
    assert levels["GAIN"].text() == "AGC"
    levels["GAIN"].set_fraction(0.5)                   # moving it takes over from the AGC
    assert not state.agc and levels["GAIN"].text() == f"{state.gain_db:.0f} dB"
    levels["SQUELCH"].set_fraction(0.0)
    assert state.squelch_db == config.SQUELCH_MIN_DB and levels["SQUELCH"].text() == "open"
    levels["SQUELCH"].set_fraction(0.5)
    assert levels["SQUELCH"].text() == f"{state.squelch_db:.0f} dB"
    for level in levels.values():                      # out-of-range drags are clamped by the state
        for fraction in (-3.0, 0.0, 1.0, 9.0):
            level.set_fraction(fraction)
            assert 0.0 <= level.fraction() <= 1.0
    assert_sane(state)


def test_dragging_a_slider_sets_its_value(window, qapp):
    state = window.state
    bar = window.main_page.sliders
    pump(qapp, 0.03)
    slider = bar.sliders["VOLUME"]
    track = slider.track_rect()
    y = int(track.center().y())

    def at(fraction: float) -> QtCore.QPoint:
        return QtCore.QPoint(int(track.left() + fraction * track.width()), y)

    QtTest.QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.3))
    assert state.volume == pytest.approx(30, abs=2)
    QtTest.QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.3))
    drag_to(slider, at(0.8))
    assert state.volume == pytest.approx(80, abs=2)
    drag_to(slider, QtCore.QPoint(slider.width() + 300, y))                   # past the end
    assert state.volume == 100
    drag_to(slider, QtCore.QPoint(-200, y))                                   # and the start
    assert state.volume == 0
    QtTest.QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(1.0))
    assert state.volume == 0, "letting go moved the slider"
    drag_to(slider, at(0.6))                           # not held down any more
    assert state.volume == 0
    assert state.knob_function == "TUNE", "dragging must not take the knob"


def test_tapping_a_slider_name_gives_it_the_knob(window, qapp):
    state = window.state
    bar = window.main_page.sliders
    pump(qapp, 0.03)
    for function in ("VOLUME", "GAIN", "SQUELCH"):
        slider = bar.sliders[function]
        volume = state.volume
        tap(window, slider.mapTo(window, QtCore.QPoint(20, sliders.HEIGHT // 2)))
        assert state.knob_function == function
        assert state.volume == volume, "the tap on the name moved the slider"
    tap(window, bar.sliders["SQUELCH"].mapTo(window, QtCore.QPoint(20, sliders.HEIGHT // 2)))
    assert state.knob_function == "TUNE"


def test_sliders_stay_usable_next_to_the_hf_panel(window, qapp):
    window.state.set_hf_panel("ON")
    pump(qapp, 0.05)
    page = window.main_page
    assert page.hf_panel.isVisible()
    for slider in page.sliders.sliders.values():
        assert slider.track_rect().width() >= 40
        assert slider.mapTo(window, QtCore.QPoint(slider.width(), 0)).x() <= page.hf_panel.x()
    assert page.sliders.mapTo(window, QtCore.QPoint(0, sliders.HEIGHT)).y() <= window.softkeys.y()


# --------------------------------------------------------------------------- toolbar layers
def test_fnc_steps_through_the_toolbar_layers(window):
    state = window.state
    state.tune_to(146_520_000, "NFM", 5000)
    state.set_dsp_nr_level(0)
    assert keys(window) == ["BAND", "MODE", "KNOB", "FNC 1/3"]
    window.key_pressed(3)
    assert keys(window) == ["MEM", "SCAN:BAND", f"FILT {vfo_bar.filter_text(state.filter_hz)}", "FNC 2/3"]
    window.key_pressed(3)
    assert keys(window) == ["NR OFF", "A<>B", "REC", "FNC 3/3"]
    window.key_pressed(3)
    assert keys(window) == ["BAND", "MODE", "KNOB", "FNC 1/3"]
    window.key_long_pressed(3)                         # hold FNC: the menu
    assert window.stack.currentIndex() == window.PAGE_MENU
    window.key_pressed(3)
    assert keys(window) == ["BAND", "MODE", "KNOB", "FNC 1/3"]
    assert all(len(text) <= 10 for row in window.main_page.TOOLBAR for text in row)


def test_layer_one_band_mode_knob(window):
    state = window.state
    band, mode = state.band_index, state.mode
    window.key_long_pressed(0)
    assert state.band_index == (band + 1) % len(state.bands)
    window.key_long_pressed(1)
    assert state.mode != mode or len(config.MODES) == 1
    window.key_pressed(2)
    assert state.knob_function == "VOLUME" and keys(window)[2] == "KNOB:AF"
    window.key_pressed(2)
    assert keys(window)[2] == "KNOB:SQL"
    window.key_long_pressed(2)
    assert window.stack.currentIndex() == window.PAGE_SETTINGS


def test_layer_two_memories_scan_filter(window):
    state = window.state
    state.tune_to(7_100_000, "LSB", 100)
    layer(window, 2)
    widths = config.FILTER_WIDTHS["LSB"]
    assert len(widths) > 2
    first = state.filter_hz
    window.key_pressed(2)
    assert state.filter_hz != first
    assert keys(window)[2] == f"FILT {vfo_bar.filter_text(state.filter_hz)}"
    assert "Filter" in window.toast.text()
    window.key_long_pressed(2)                         # held: the other way
    assert state.filter_hz == first
    state.set_mode("WFM")
    assert keys(window)[2] == f"FILT {vfo_bar.filter_text(state.filter_hz)}", "the label did not follow the mode"

    window.key_long_pressed(1)                         # hold SCAN: the other source
    assert state.scan_source == "band" and "No memories" in window.toast.text()
    state.import_preset_memories()
    window.key_long_pressed(1)
    assert state.scan_source == "memory" and keys(window)[1] == "SCAN:MEM"
    window.key_long_pressed(1)
    assert state.scan_source == "band"
    state.tune_to(146_520_000, "NFM", 5000)
    window.key_pressed(1)
    assert state.scanning and keys(window) == ["HOLD", "SKIP", "MEM", "STOP"]
    window.key_pressed(3)
    assert not state.scanning and keys(window)[3] == "FNC 2/3"
    window.key_pressed(0)
    assert window.stack.currentIndex() == window.PAGE_MEMORIES


def test_layer_three_noise_vfo_record(window, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda d: 10_000_000_000)
    state = window.state
    state.set_dsp_nr_level(0)
    state.set_dsp_nb_level(0)
    layer(window, 3)
    window.key_pressed(0)
    assert state.dsp_nr_level == 5 and keys(window)[0] == "NR 5"
    window.key_pressed(0)
    assert state.dsp_nr_level == 0 and keys(window)[0] == "NR OFF"
    window.key_long_pressed(0)
    assert state.dsp_nb_level == 5
    window.key_long_pressed(0)
    assert state.dsp_nb_level == 0

    state.tune_to(146_520_000, "NFM", 5000)
    window.key_long_pressed(1)                         # hold A<>B: A > B
    assert state.vfo_b["vfo_hz"] == 146_520_000
    state.tune_to(7_074_000, "USB", 100)
    window.key_pressed(1)
    assert (state.vfo_hz, state.mode) == (146_520_000, "NFM") and state.vfo_b["vfo_hz"] == 7_074_000

    window.key_pressed(2)
    assert state.recording and keys(window)[2] == "STOP REC"
    assert "REC" in window.main_page.vfo_bar.text()
    window.key_pressed(2)
    assert not state.recording and keys(window)[2] == "REC"
    window.key_long_pressed(2)
    assert window.stack.currentIndex() == window.PAGE_RECORDINGS


# --------------------------------------------------------------------------- menu page
def test_every_menu_tile_opens_its_page_and_back_returns_to_the_radio(window, qapp):
    page = window.menu_page
    settings = window.settings_page
    expected = {"band": window.PAGE_BAND, "mode": window.PAGE_MODE, "memories": window.PAGE_MEMORIES,
                "keypad": window.PAGE_KEYPAD, "recordings": window.PAGE_RECORDINGS,
                "scan_log": window.PAGE_SCAN_LOG, "network": window.PAGE_NETWORK,
                "adsb": window.PAGE_ADSB, "location": window.PAGE_LOCATION}
    assert len(page.buttons) == len(page.ENTRIES) == len({target for *_rest, target in page.ENTRIES})
    for index, (name, _hint, target) in enumerate(page.ENTRIES):
        window.show_page(window.PAGE_MENU)
        pump(qapp, 0.02)
        assert page.buttons[index].text().startswith(name)
        page.buttons[index].click()
        pump(qapp, 0.02)
        if target == "hf_panel":
            assert window.stack.currentIndex() == window.PAGE_MAIN
            continue
        if target.startswith("settings:"):
            assert window.stack.currentIndex() == window.PAGE_SETTINGS, name
            assert settings.TABS[settings.stack.currentIndex()] == target.partition(":")[2]
            assert settings.selected == settings.first_row[settings.stack.currentIndex()]
        else:
            assert window.stack.currentIndex() == expected[target], name
        window.key_pressed(3)                          # BACK / STOP
        assert window.stack.currentIndex() == window.PAGE_MAIN, f"{name} did not return to the radio"
    assert_sane(window.state)


def test_menu_works_from_the_keys_and_the_knob(window, qapp):
    page = window.menu_page
    window.key_long_pressed(3)
    assert page.selected == 0
    window.knob_turned(2)
    assert page.ENTRIES[page.selected][0] == "MEMORIES"
    window.key_pressed(1)                              # DOWN a row
    assert page.ENTRIES[page.selected][0] == "DISPLAY"
    window.encoder_pushed()
    assert window.stack.currentIndex() == window.PAGE_SETTINGS
    assert window.settings_page.TABS[window.settings_page.stack.currentIndex()] == "DISPLAY"
    window.key_long_pressed(3)
    window.key_long_pressed(3)
    assert page.selected == 0, "the menu did not start from the top again"


def test_pages_reached_from_settings_still_go_back_to_settings(window, qapp):
    """Only a page opened straight from the menu returns to the radio."""
    page = window.settings_page
    window.show_page(window.PAGE_MENU)
    window.menu_page.buttons[[e[2] for e in window.menu_page.ENTRIES].index("location")].click()
    assert window.stack.currentIndex() == window.PAGE_LOCATION
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN

    window.show_page(window.PAGE_SETTINGS)
    assert page.select_row("Location") and not page.select_row("No such row")
    window.key_pressed(2)
    assert window.stack.currentIndex() == window.PAGE_LOCATION
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_SETTINGS


def test_menu_tiles_fit_the_screen(window, qapp):
    window.show_page(window.PAGE_MENU)
    pump(qapp, 0.05)
    colours, _ = image_stats(window)
    assert colours > 8
    for button in window.menu_page.buttons:
        bottom = button.mapTo(window, QtCore.QPoint(0, button.height())).y()
        assert bottom <= window.softkeys.y() and button.height() >= 44
        longest = max(button.text().split("\n"), key=len)
        assert button.fontMetrics().horizontalAdvance(longest) <= button.width() - 8, button.text()


# --------------------------------------------------------------------------- spectrum
def test_frequency_scale_is_on_top_of_the_panadapter(window, qapp):
    spectrum = window.main_page.spectrum
    pump(qapp, 0.1)
    top = spectrum.spec_plot.getAxis("top")
    assert top.isVisible() and top.style["showValues"]
    assert not spectrum.wf_plot.getAxis("bottom").isVisible()
    assert not spectrum.spec_plot.getAxis("bottom").isVisible()
    controls = spectrum.controls.geometry()
    assert spectrum.rect().contains(controls), "zoom / seek buttons hang off the spectrum"
    trace = spectrum.spec_plot.getViewBox().sceneBoundingRect().height()
    waterfall = spectrum.wf_plot.getViewBox().sceneBoundingRect().height()
    assert 0.6 < trace / waterfall < 1.6
