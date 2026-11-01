"""Main window: top status bar, page stack, soft-key bar; routes all physical controls."""
from __future__ import annotations

import logging
import time

from .. import config
from ..qt import Qt, QtCore, QtGui, QtWidgets
from ..radio.backend import Backend
from ..scan import Scanner
from ..state import RadioState, format_step
from . import style
from .freq_display import FreqDisplay, SMeter
from .pickers import BandPicker, Keypad, MemoriesPage, ModePicker, Page, SettingsPage
from .softkeys import SoftKeyBar
from .spectrum import SpectrumWaterfall

log = logging.getLogger(__name__)


def _kv(key) -> int:
    """Integer value of a Qt key, for PyQt5 (int enums) and PyQt6 (Python enums) alike."""
    return int(getattr(key, "value", key))


class MainPage(Page):
    """Frequency readout, S-meter and the spectrum/waterfall."""

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(2)
        self.freq = FreqDisplay(state)
        self.smeter = SMeter(state)
        row.addWidget(self.freq, 1)
        row.addWidget(self.smeter, 0)
        layout.addLayout(row)
        self.spectrum = SpectrumWaterfall(state)
        layout.addWidget(self.spectrum, 1)

        self.freq.step_tapped.connect(self._step_tapped)
        # Spectrum tap-to-tune is wired by MainWindow so scanning can be cancelled.

    def _step_tapped(self, hz: int) -> None:
        self.state.set_step_hz(hz)
        self.state.set_knob_function("TUNE")
        self.state.message.emit(f"Step {format_step(self.state.step_hz)}")

    def softkey_labels(self) -> list[str]:
        if self.state.scanning:
            hold = "RESUME" if self.state.scan_held else "HOLD"
            source = "MEM" if self.state.scan_source == "band" else "BAND"
            return [hold, "SKIP", source, "STOP"]
        fn = self.state.knob_function
        func_label = "FUNC" if fn == "TUNE" else f"FUNC:{fn[:3]}"
        return ["BAND", "MODE", func_label, "MEM"]

    def on_softkey(self, index: int) -> None:
        # Handled by the main window because BAND/MODE/MEM open other pages.
        pass


class MainWindow(QtWidgets.QMainWindow):
    PAGE_MAIN, PAGE_BAND, PAGE_MODE, PAGE_KEYPAD, PAGE_SETTINGS, PAGE_MEMORIES = range(6)

    def __init__(self, state: RadioState, backend: Backend, windowed: bool = False) -> None:
        super().__init__()
        self.state = state
        self.backend = backend
        self.scanner = Scanner(state, self)
        self.setWindowTitle("PiSDR")
        self.setStyleSheet(style.STYLESHEET)
        self.setFixedSize(config.SCREEN_W, config.SCREEN_H)
        if not windowed:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
            self.setCursor(Qt.CursorShape.BlankCursor)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.topbar = QtWidgets.QLabel("")
        self.topbar.setObjectName("topbar")
        self.topbar.setFixedHeight(30)
        self.topbar.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.topbar)

        self.stack = QtWidgets.QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.main_page = MainPage(state)
        self.band_page = BandPicker(state)
        self.mode_page = ModePicker(state)
        self.keypad_page = Keypad(state)
        self.settings_page = SettingsPage(state)
        self.memories_page = MemoriesPage(state)
        for page in (self.main_page, self.band_page, self.mode_page, self.keypad_page,
                     self.settings_page, self.memories_page):
            self.stack.addWidget(page)
            page.done.connect(self.show_main)
        self.settings_page.quit_requested.connect(self.close)

        self.softkeys = SoftKeyBar()
        layout.addWidget(self.softkeys)
        self.softkeys.pressed.connect(self.key_pressed)
        self.softkeys.long_pressed.connect(self.key_long_pressed)

        # Toast message overlay.
        self.toast = QtWidgets.QLabel(central)
        self.toast.setObjectName("toast")
        self.toast.hide()
        self._toast_timer = QtCore.QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

        # Tap the frequency display (not a digit) to open the keypad.
        self.main_page.freq.installEventFilter(self)
        self._freq_press_t = 0.0

        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._update_topbar)
        self._clock.start(1000)

        state.changed.connect(self._on_state_changed)
        state.message.connect(self.show_message)
        backend.status.connect(self.show_message)
        self.scanner.status.connect(self.show_message)
        backend.spectrum_ready.connect(self.main_page.spectrum.push_spectrum)
        backend.center_changed.connect(self.main_page.spectrum.set_center)
        self.main_page.spectrum.tuned.connect(self._spectrum_tuned)

        self.show_main()
        self._update_topbar()

    def _spectrum_tuned(self, hz: float) -> None:
        if self.state.scanning:
            self.scanner.stop()
            self._refresh_main_softkeys()
        self.state.set_vfo(hz)

    # ------------------------------------------------------------------ pages
    def current_page(self) -> Page:
        return self.stack.currentWidget()  # type: ignore[return-value]

    def show_page(self, index: int) -> None:
        if index != self.PAGE_MAIN and self.state.scanning:
            self.scanner.stop()
        self.stack.setCurrentIndex(index)
        page = self.current_page()
        page.on_shown()
        self.softkeys.set_labels(page.softkey_labels())

    def show_main(self) -> None:
        self.show_page(self.PAGE_MAIN)

    def toggle_page(self, index: int) -> None:
        self.show_main() if self.stack.currentIndex() == index else self.show_page(index)

    def _refresh_main_softkeys(self) -> None:
        if self.stack.currentIndex() == self.PAGE_MAIN:
            self.softkeys.set_labels(self.main_page.softkey_labels())

    # ------------------------------------------------------------------ control routing
    def key_pressed(self, index: int) -> None:
        self.softkeys.flash(index)
        if self.stack.currentIndex() != self.PAGE_MAIN:
            self.current_page().on_softkey(index)
            self.softkeys.set_labels(self.current_page().softkey_labels())
            return
        if self.state.scanning:
            if index == 0:
                self.scanner.toggle_hold()
            elif index == 1:
                self.scanner.skip()
            elif index == 2:
                self.scanner.cycle_source()
            elif index == 3:
                self.scanner.stop()
            self._refresh_main_softkeys()
            return
        if index == 0:
            self.show_page(self.PAGE_BAND)
        elif index == 1:
            self.show_page(self.PAGE_MODE)
        elif index == 2:
            self.state.cycle_knob_function()
            self.show_message(f"Knob: {self.state.knob_function}")
        elif index == 3:
            self.show_page(self.PAGE_MEMORIES)

    def key_long_pressed(self, index: int) -> None:
        if self.stack.currentIndex() != self.PAGE_MAIN:
            if not self.current_page().on_softkey_long(index):
                self.show_main()
            return
        if index == 3:
            # Long MEM: start/stop band scan (or memory scan if that source is selected).
            if self.state.scanning:
                self.scanner.stop()
            else:
                self.scanner.start()
            self._refresh_main_softkeys()
        elif index == 2:
            self.show_page(self.PAGE_SETTINGS)
        elif index == 0:
            self.state.cycle_band()
        elif index == 1:
            self.state.cycle_mode()

    def knob_turned(self, detents: int) -> None:
        if self.state.scanning and self.stack.currentIndex() == self.PAGE_MAIN:
            # Manual tune while scanning cancels the scan (same as a handheld).
            self.scanner.stop()
            self._refresh_main_softkeys()
        if not self.current_page().on_knob(detents):
            self.state.knob_turned(detents)

    def encoder_pushed(self) -> None:
        if self.state.scanning and self.stack.currentIndex() == self.PAGE_MAIN:
            self.scanner.toggle_hold()
            self._refresh_main_softkeys()
            return
        if not self.current_page().on_encoder_push():
            self.state.cycle_step()

    def encoder_held(self) -> None:
        self.toggle_page(self.PAGE_KEYPAD)

    # ------------------------------------------------------------------ feedback
    def show_message(self, text: str) -> None:
        self.toast.setText(text)
        self.toast.setWordWrap(len(text) > 60)
        if len(text) > 60:
            self.toast.setFixedWidth(self.width() - 80)
        else:
            self.toast.setMinimumWidth(0)
            self.toast.setMaximumWidth(self.width())
        self.toast.adjustSize()
        self.toast.move((self.width() - self.toast.width()) // 2, 112)
        self.toast.show()
        self.toast.raise_()
        # Long diagnostics stay up longer so they can be read.
        self._toast_timer.start(8000 if len(text) > 60 else 1800)

    def _on_state_changed(self, field: str) -> None:
        if field in ("knob_function", "recording", "scanning", "scan_source", "memories"):
            self._refresh_main_softkeys()
        if field != "signal_db":
            self._update_topbar()

    def _update_topbar(self) -> None:
        s = self.state
        dim = style.TEXT_DIM
        acc = style.ACCENT
        parts = [
            f"<b>{s.band.name}</b>",
            f"<span style='color:{style.ACCENT_2}'><b>{s.mode}</b></span>",
            f"<span style='color:{dim}'>STEP</span> {format_step(s.step_hz)}",
            f"<span style='color:{dim}'>VOL</span> {s.volume}",
            f"<span style='color:{dim}'>SQL</span> {'open' if s.squelch_db <= config.SQUELCH_MIN_DB else f'{s.squelch_db:.0f}'}",
            f"<span style='color:{dim}'>GAIN</span> {'AGC' if s.agc else f'{s.gain_db:.0f} dB'}",
        ]
        if s.bias_tee:
            parts.append(f"<span style='color:{acc}'><b>BIAS</b></span>")
        if s.recording:
            parts.append(f"<span style='color:{style.REC}'><b>&#9679; REC</b></span>")
        if s.scanning:
            held = "HOLD" if s.scan_held else "SCAN"
            src = "MEM" if s.scan_source == "memory" else "BAND"
            parts.append(f"<span style='color:{acc}'><b>{held}:{src}</b></span>")
        knob = f"<span style='color:{acc}'><b>KNOB {s.knob_function}</b></span>"
        clock = time.strftime("%H:%M")
        left = " &nbsp;&nbsp; ".join(parts)
        self.topbar.setText(
            f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
            f"<td align='left'>{left}</td><td align='right'>{knob} &nbsp;&nbsp; {clock}</td></tr></table>")

    # ------------------------------------------------------------------ events
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # Long-press on the frequency display opens the keypad; digits are handled by the widget.
        if obj is self.main_page.freq:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self._freq_press_t = time.monotonic()
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if time.monotonic() - self._freq_press_t > 0.6:
                    self.show_page(self.PAGE_KEYPAD)
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        """Keyboard emulation of the panel controls for development on a PC."""
        key = _kv(event.key())
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if key == _kv(Qt.Key.Key_Q) and ctrl or key == _kv(Qt.Key.Key_Escape):
            self.close()
        elif key in (_kv(Qt.Key.Key_Up), _kv(Qt.Key.Key_Right)):
            self.knob_turned(10 if shift else 1)
        elif key in (_kv(Qt.Key.Key_Down), _kv(Qt.Key.Key_Left)):
            self.knob_turned(-10 if shift else -1)
        elif key in (_kv(Qt.Key.Key_Return), _kv(Qt.Key.Key_Enter), _kv(Qt.Key.Key_Space)):
            self.encoder_held() if shift else self.encoder_pushed()
        elif _kv(Qt.Key.Key_F1) <= key <= _kv(Qt.Key.Key_F4):
            index = key - _kv(Qt.Key.Key_F1)
            self.key_long_pressed(index) if shift else self.key_pressed(index)
        elif key == _kv(Qt.Key.Key_Escape):
            self.show_main()
        elif _kv(Qt.Key.Key_0) <= key <= _kv(Qt.Key.Key_9) or key == _kv(Qt.Key.Key_Period):
            if self.stack.currentIndex() != self.PAGE_KEYPAD:
                self.show_page(self.PAGE_KEYPAD)
            self.keypad_page.key(chr(key))
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        self.knob_turned(1 if event.angleDelta().y() > 0 else -1)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.scanner.stop()
        self.state.save()
        self.backend.stop()
        super().closeEvent(event)
