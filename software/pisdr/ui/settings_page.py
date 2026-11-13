"""Tabbed settings: RADIO / AUDIO / DISPLAY / SCAN / SYSTEM.

All rows form one list, so UP / DOWN (and the knob on action rows) walk through every tab
in order; the visible tab follows the selection. Tap a tab to jump to it, tap a row to select
it, tap again (or CHANGE / push the knob) to toggle or activate; the knob steps values.
Long-press UP / DOWN jumps to the previous / next tab.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from .. import config, sysinfo, zipcodes
from ..qt import Qt, QtWidgets, Signal
from ..state import RadioState
from . import style
from .pickers import Page

log = logging.getLogger(__name__)


@dataclass
class _Row:
    label: str
    get: Callable[[], str]
    adjust: Callable[[int], None]    # adjust(0) = activate / toggle, adjust(+-n) = knob
    action: bool = False             # knob moves the selection instead of the value


class SettingRow(QtWidgets.QFrame):
    """One settings line: label on the left, value on the right, tap to select/activate."""

    clicked = Signal()

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.setFixedHeight(34)
        self.setProperty("selected", False)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)
        self.label = QtWidgets.QLabel(label)
        self.value = QtWidgets.QLabel("")
        self.value.setObjectName("value")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.value, 0)

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            for w in (self, self.label, self.value):
                style.repolish(w)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


def _onoff(value: bool) -> str:
    return "ON" if value else "OFF"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}Z"


def _short(text: str, n: int = 16) -> str:
    return text if len(text) <= n else text[: n - 1] + "\u2026"


class SettingsPage(Page):
    quit_requested = Signal()
    open_page = Signal(str)       # "recordings", "scan_log", "network", "adsb", "location"

    TABS = ("RADIO", "AUDIO", "DISPLAY", "SCAN", "SYSTEM")
    IGNORED_FIELDS = {"signal_db", "cw_text", "cw_wpm", "decoder_lines", "rds", "space_wx", "aircraft"}

    def __init__(self, state: RadioState, backend=None, backlight=None, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.backend = backend
        self.backlight = backlight
        self.selected = 0
        self._confirm: tuple[str, float] | None = None
        self._rec_count: int | None = None
        self.rows: list[_Row] = []
        self.row_tab: list[int] = []
        self.widgets: list[SettingRow] = []
        self.first_row: list[int] = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(4)
        tabbar = QtWidgets.QHBoxLayout()
        tabbar.setSpacing(4)
        self.tab_buttons: list[QtWidgets.QPushButton] = []
        for i, name in enumerate(self.TABS):
            btn = QtWidgets.QPushButton(name)
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, i=i: self._select_tab(i))
            tabbar.addWidget(btn, 1)
            self.tab_buttons.append(btn)
        outer.addLayout(tabbar)
        self.stack = QtWidgets.QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.info = QtWidgets.QLabel("")
        self.info.setObjectName("info")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.info.setWordWrap(True)

        for tab_index, columns in enumerate(self._layout()):
            self.first_row.append(len(self.rows))
            page = QtWidgets.QWidget()
            hbox = QtWidgets.QHBoxLayout(page)
            hbox.setContentsMargins(0, 0, 0, 0)
            hbox.setSpacing(10)
            for sections in columns:
                col = QtWidgets.QVBoxLayout()
                col.setSpacing(3)
                for title, rows in sections:
                    head = QtWidgets.QLabel(title)
                    head.setObjectName("section")
                    head.setFixedHeight(20)
                    col.addWidget(head)
                    if isinstance(rows, QtWidgets.QWidget):
                        col.addWidget(rows)
                        continue
                    for row in rows:
                        self._add_row(row, tab_index, col)
                    col.addSpacing(4)
                col.addStretch(1)
                hbox.addLayout(col, 1)
            self.stack.addWidget(page)

        state.changed.connect(self._on_changed)
        self.refresh()

    # ------------------------------------------------------------------ construction
    def _add_row(self, row: _Row, tab_index: int, col: QtWidgets.QVBoxLayout) -> None:
        index = len(self.rows)
        self.rows.append(row)
        self.row_tab.append(tab_index)
        widget = SettingRow(row.label)
        widget.clicked.connect(lambda i=index: self._tap(i))
        col.addWidget(widget)
        self.widgets.append(widget)

    def _layout(self) -> list:
        """Per tab: [left column sections, right column sections]; a section is (title, rows)."""
        s = self.state
        receiver = [
            _Row("Filter width", lambda: f"{s.filter_hz / 1000:g} kHz {s.mode}", lambda d: s.step_filter(d or 1)),
            _Row("Tuner AGC", lambda: _onoff(s.agc), lambda d: s.set_agc(not s.agc)),
            _Row("RF gain", lambda: f"{s.gain_db:.0f} dB" + (" (AGC)" if s.agc else ""),
                 lambda d: s.set_gain(s.gain_db + (d or 1) * config.GAIN_STEP_DB)),
            _Row("Squelch", lambda: "open" if s.squelch_db <= config.SQUELCH_MIN_DB else f"{s.squelch_db:.0f} dB",
                 lambda d: s.set_squelch(config.SQUELCH_MIN_DB if d == 0 and s.squelch_db > config.SQUELCH_MIN_DB
                                         else s.squelch_db + (d or 30) * config.SQUELCH_STEP_DB)),
            _Row("Bias tee (4.5 V)", lambda: _onoff(s.bias_tee), lambda d: s.set_bias_tee(not s.bias_tee)),
            _Row("PPM correction", lambda: f"{s.ppm:+d} ppm",
                 lambda d: s.set_ppm(0 if d == 0 else s.ppm + d)),
            _Row("S-meter calibration", lambda: f"{s.smeter_cal_db:+.0f} dB",
                 lambda d: s.set_smeter_cal(config.SMETER_CAL_DB if d == 0 else s.smeter_cal_db + d)),
        ]
        decoders = [
            _Row("CW pitch", lambda: f"{s.cw_pitch_hz} Hz",
                 lambda d: s.set_cw_pitch(config.CW_PITCH_HZ if d == 0
                                          else s.cw_pitch_hz + d * config.CW_PITCH_STEP_HZ)),
            _Row("Data decoder", lambda: s.decoder, lambda d: s.cycle_decoder(d or 1)),
            _Row("Clear decoder text", lambda: f"{len(s.decoder_lines)} lines",
                 lambda d: s.clear_decoder_lines() if d == 0 else None, action=True),
            _Row("RDS on FM broadcast", self._rds_value, lambda d: s.set_rds_enabled(not s.rds_enabled)),
        ]
        audio = [
            _Row("Volume", lambda: f"{s.volume} %", lambda d: s.set_volume(s.volume + (d or 5) * config.VOLUME_STEP)),
            _Row("Noise reduction", lambda: "OFF" if s.dsp_nr_level <= 0 else f"{s.dsp_nr_level}", self._adjust_nr),
            _Row("Noise blanker", lambda: "OFF" if s.dsp_nb_level <= 0 else f"{s.dsp_nb_level}", self._adjust_nb),
            _Row("Notch", lambda: "OFF" if not s.dsp_notch else f"{s.dsp_notch_hz} Hz", self._adjust_notch),
            _Row("High-pass", lambda: "OFF" if not s.dsp_hpf else f"{s.dsp_hpf_hz} Hz", self._adjust_hpf),
            _Row("Low-pass", lambda: "OFF" if not s.dsp_lpf else f"{s.dsp_lpf_hz} Hz", self._adjust_lpf),
        ]
        recording = [
            _Row("Record now", lambda: "REC" if s.recording else "OFF", lambda d: s.toggle_recording()),
            _Row("Recordings", lambda: f"{self._recordings_count()} \u25b8",
                 lambda d: self.open_page.emit("recordings") if d == 0 else None, action=True),
            _Row("Timer start", lambda: _hhmm(s.timer_start_min), self._adjust_timer_start),
            _Row("Timer length", lambda: f"{s.timer_length_min} min", lambda d: s.cycle_timer_length(d or 1)),
            _Row("Timer repeat", lambda: "DAILY" if s.timer_daily else "ONCE",
                 lambda d: s.set_timer_daily(not s.timer_daily)),
            _Row("Timer", self._timer_value, lambda d: s.arm_timer(not s.timer_armed)),
        ]
        screen = [
            _Row("Theme", lambda: s.theme, lambda d: s.cycle_theme(d or 1)),
            _Row("Brightness", self._brightness_value,
                 lambda d: s.set_brightness(100 if d == 0 else s.brightness + 10 * d)),
            _Row("Dim after", self._dim_value, lambda d: s.cycle_dim_after(d or 1)),
            _Row("Clock", lambda: "UTC" if s.clock_utc else "LOCAL", lambda d: s.set_clock_utc(not s.clock_utc)),
            _Row("Knob acceleration", lambda: _onoff(s.knob_accel), lambda d: s.set_knob_accel(not s.knob_accel)),
            _Row("Tap snaps to signal", lambda: s.snap_mode, lambda d: s.cycle_snap_mode(d or 1)),
            _Row("HF conditions panel", lambda: s.hf_panel, lambda d: s.cycle_hf_panel(d or 1)),
        ]
        spectrum = [
            _Row("Spectrum rate", lambda: f"{s.fft_fps} fps", lambda d: s.set_fft_fps(s.fft_fps + 5 * (d or 1))),
            _Row("Waterfall speed", lambda: f"{s.waterfall_speed} row/frame",
                 lambda d: s.set_waterfall_speed((s.waterfall_speed + (d or 1) - 1) % 4 + 1)),
            _Row("Zoom", lambda: f"x{s.spectrum_zoom}", lambda d: s.cycle_spectrum_zoom(d or 1)),
            _Row("Averaging", lambda: config.SPECTRUM_AVG_NAMES[s.spectrum_avg], lambda d: s.cycle_spectrum_avg(d or 1)),
            _Row("Peak hold", lambda: _onoff(s.peak_hold), lambda d: s.set_peak_hold(not s.peak_hold)),
            _Row("Trace style", lambda: s.spectrum_style, lambda d: s.cycle_spectrum_style(d or 1)),
            _Row("Waterfall colours", lambda: s.wf_palette, lambda d: s.cycle_wf_palette(d or 1)),
            _Row("Labels", lambda: s.spectrum_labels, lambda d: s.cycle_spectrum_labels(d or 1)),
        ]
        scan = [
            _Row("Dwell per channel", lambda: f"{s.scan_dwell_ms} ms", lambda d: s.cycle_scan_dwell(d or 1)),
            _Row("Resume after", lambda: "STAY" if s.scan_resume_ms == 0 else f"{s.scan_resume_ms / 1000:g} s",
                 lambda d: s.cycle_scan_resume(d or 1)),
            _Row("Trigger level", lambda: f"{s.scan_floor_db:.0f} dB",
                 lambda d: s.set_scan_floor(config.SCAN_FLOOR_DB if d == 0 else s.scan_floor_db + d)),
            _Row("Memory group", lambda: _short(s.memory_group, 12), lambda d: s.cycle_memory_group(d or 1)),
            _Row("ON NOW only", lambda: _onoff(s.memory_scan_on_now),
                 lambda d: s.set_memory_scan_on_now(not s.memory_scan_on_now)),
        ]
        priority = [
            _Row("Priority channel", lambda: _short(s.priority["name"]) if s.priority else "OFF",
                 self._adjust_priority),
            _Row("Priority check every", lambda: f"{s.priority_interval_s} s", lambda d: s.cycle_priority_interval(d or 1)),
            _Row("Lockouts", lambda: f"{len(s.lockouts)} \u00b7 CLEAR" if s.lockouts else "none",
                 lambda d: self._confirmed("lockouts", s.clear_lockouts) if d == 0 else None, action=True),
            _Row("Scan log", lambda: f"{len(s.scan_log)} hits \u25b8",
                 lambda d: self.open_page.emit("scan_log") if d == 0 else None, action=True),
        ]
        system = [
            _Row("Load HF presets", lambda: "", lambda d: s.import_preset_memories() if d == 0 else None, action=True),
            _Row("Network mode (rtl_tcp)", lambda: "\u25b8",
                 lambda d: self.open_page.emit("network") if d == 0 else None, action=True),
            _Row("ADS-B aircraft map", lambda: "\u25b8",
                 lambda d: self.open_page.emit("adsb") if d == 0 else None, action=True),
            _Row("Location", lambda: f"{zipcodes.label(s)} \u25b8",
                 lambda d: self.open_page.emit("location") if d == 0 else None, action=True),
            _Row("Exit PiSDR", lambda: "", lambda d: self.quit_requested.emit() if d == 0 else None, action=True),
            _Row("Shut down Pi", lambda: "",
                 lambda d: self._confirmed("shutdown", self._shutdown) if d == 0 else None, action=True),
        ]
        return [
            [[("RECEIVER", receiver)], [("MODES & DECODERS", decoders)]],
            [[("AUDIO DSP", audio)], [("RECORDING", recording)]],
            [[("SCREEN & CONTROLS", screen)], [("SPECTRUM", spectrum)]],
            [[("SCAN", scan)], [("PRIORITY / LOCKOUTS / LOG", priority)]],
            [[("SYSTEM", system)], [("INFO", self.info)]],
        ]

    # ------------------------------------------------------------------ value helpers
    def _rds_value(self) -> str:
        if self.backend is not None and not self.backend.rds_available:
            return "not installed"
        return _onoff(self.state.rds_enabled)

    def _brightness_value(self) -> str:
        note = self.backlight.status() if self.backlight is not None else "no panel"
        return f"{self.state.brightness} %" + (f" ({note})" if note else "")

    def _dim_value(self) -> str:
        sec = self.state.dim_after_s
        if sec == 0:
            return "never"
        return f"{sec} s" if sec < 60 else f"{sec // 60} min"

    def _timer_value(self) -> str:
        s = self.state
        if s.timer_running:
            return "RECORDING"
        if s.timer_armed and s.timer_target:
            t = s.timer_target
            return f"ARMED {t['vfo_hz'] / 1e6:.4f} {t['mode']}"
        return "OFF"

    def _recordings_count(self) -> int:
        if self._rec_count is None:
            try:
                self._rec_count = sum(1 for _ in config.RECORDINGS_DIR.glob("*.wav"))
            except OSError:
                self._rec_count = 0
        return self._rec_count

    # ------------------------------------------------------------------ adjusters
    def _adjust_nr(self, d: int) -> None:
        if d == 0:
            self.state.set_dsp_nr_level(0 if self.state.dsp_nr_level else 5)
        else:
            self.state.set_dsp_nr_level(self.state.dsp_nr_level + d)

    def _adjust_nb(self, d: int) -> None:
        if d == 0:
            self.state.set_dsp_nb_level(0 if self.state.dsp_nb_level else 5)
        else:
            self.state.set_dsp_nb_level(self.state.dsp_nb_level + d)

    def _adjust_notch(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_notch()
        elif self.state.dsp_notch:
            self.state.set_dsp_notch_hz(self.state.dsp_notch_hz + d * 50)
        else:
            self.state.set_dsp_notch(True)

    def _adjust_hpf(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_hpf()
        elif self.state.dsp_hpf:
            self.state.set_dsp_hpf_hz(self.state.dsp_hpf_hz + d * 25)
        else:
            self.state.set_dsp_hpf(True)

    def _adjust_lpf(self, d: int) -> None:
        if d == 0:
            self.state.toggle_dsp_lpf()
        elif self.state.dsp_lpf:
            self.state.set_dsp_lpf_hz(self.state.dsp_lpf_hz + d * 250)
        else:
            self.state.set_dsp_lpf(True)

    def _adjust_timer_start(self, d: int) -> None:
        if d == 0:
            tm = time.gmtime()
            now_min = tm.tm_hour * 60 + tm.tm_min
            self.state.adjust_timer_start((now_min // 5 + 1) * 5 - self.state.timer_start_min)
        else:
            self.state.adjust_timer_start(5 * d)

    def _adjust_priority(self, d: int) -> None:
        s = self.state
        if d != 0:
            s.cycle_priority(d)
        elif s.priority:
            s.set_priority(None)
        else:
            s.set_priority(s.priority_from_current())
            s.message.emit(f"Priority: {s.priority['name']}")

    def _confirmed(self, key: str, action: Callable[[], None]) -> None:
        """Destructive rows need a second press within 3 s."""
        now = time.monotonic()
        if self._confirm and self._confirm[0] == key and now - self._confirm[1] < 3.0:
            self._confirm = None
            action()
        else:
            self._confirm = (key, now)
            self.state.message.emit("Press again to confirm")

    def _shutdown(self) -> None:
        if self.state.recording:
            self.state.set_recording(False)     # close the WAV before the power goes
        self.state.save()
        self.state.message.emit("Shutting down...")
        if sys.platform.startswith("linux"):
            subprocess.Popen(["systemctl", "poweroff"])
        else:
            log.info("Shutdown requested (ignored on %s)", sys.platform)

    # ------------------------------------------------------------------ view
    def _on_changed(self, field: str) -> None:
        if field == "recording":
            self._rec_count = None
        if field not in self.IGNORED_FIELDS and self.isVisible():
            self.refresh()

    def on_shown(self) -> None:
        self._rec_count = None
        self._confirm = None
        self._refresh_info()
        self.refresh()

    def _refresh_info(self) -> None:
        dim = style.TEXT_DIM
        backend = type(self.backend).__name__ if self.backend is not None else "?"
        backend = {"GnuRadioBackend": "GNU Radio + RTL-SDR", "SimBackend": "Simulator"}.get(backend, backend)
        bl = self.backlight
        if backend != "Simulator" and self.backend is not None and self.backend.rds_available:
            rds = "in use"
        else:
            rds = "installed" if sysinfo.have_module("rds") else "not installed"
        lines = [
            ("Radio", backend),
            ("IP address", f"{sysinfo.lan_address()} ({sysinfo.hostname()})"),
            ("rtl_tcp", "installed" if sysinfo.have_tool("rtl_tcp") else "missing (rtl-sdr)"),
            ("multimon-ng", "installed" if sysinfo.have_tool("multimon-ng") else "missing"),
            ("gr-rds", rds),
            ("Backlight", bl.path.name if bl is not None and bl.path is not None else "none (HDMI?)"),
            ("Recordings", str(config.RECORDINGS_DIR)),
        ]
        self.info.setText("<br>".join(f"<span style='color:{dim}'>{k}</span>&nbsp; {v}" for k, v in lines))

    def refresh(self) -> None:
        tab = self.row_tab[self.selected]
        self.stack.setCurrentIndex(tab)
        for i, btn in enumerate(self.tab_buttons):
            btn.setChecked(i == tab)
        for i, row in enumerate(self.rows):
            if self.row_tab[i] != tab:
                continue
            self.widgets[i].value.setText(row.get())
            self.widgets[i].set_selected(i == self.selected)

    def _select_tab(self, tab: int) -> None:
        self.selected = self.first_row[tab % len(self.TABS)]
        self.refresh()

    def select_tab(self, name: str) -> None:
        """Show the tab called *name* (RADIO, AUDIO ...) with its first row selected."""
        if name in self.TABS:
            self._select_tab(self.TABS.index(name))

    def select_row(self, label: str) -> bool:
        """Select the row called *label* and show its tab; False if there is no such row."""
        for index, row in enumerate(self.rows):
            if row.label == label:
                self.selected = index
                self.refresh()
                return True
        return False

    def _tap(self, index: int) -> None:
        if self.selected == index or self.rows[index].action:
            self.selected = index
            self.rows[index].adjust(0)
        self.selected = index
        self.refresh()

    # ------------------------------------------------------------------ controls
    def softkey_labels(self) -> list[str]:
        return ["UP", "DOWN", "CHANGE", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            self.selected = (self.selected - 1) % len(self.rows)
        elif index == 1:
            self.selected = (self.selected + 1) % len(self.rows)
        elif index == 2:
            self.rows[self.selected].adjust(0)
        else:
            self.done.emit()
            return
        self.refresh()

    def on_softkey_long(self, index: int) -> bool:
        if index in (0, 1):
            tab = self.row_tab[self.selected] + (1 if index == 1 else -1)
            self._select_tab(tab)
            return True
        return False

    def on_knob(self, detents: int) -> bool:
        if self.rows[self.selected].action:
            self.selected = (self.selected + detents) % len(self.rows)
        else:
            self.rows[self.selected].adjust(detents)
        self.refresh()
        return True

    def on_encoder_push(self) -> bool:
        self.rows[self.selected].adjust(0)
        self.refresh()
        return True
