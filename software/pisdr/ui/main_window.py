"""Main window: the main screen, the other pages and the soft-key bar; routes all controls.

The main screen follows piHPSDR's layout: a VFO bar with status indicators around the
frequency dials, the meter, MENU and HIDE buttons, the panadapter and waterfall, a sliders
area, and a toolbar. The toolbar is the soft-key bar: four cells over the four physical
keys, the last of which (FNC) steps through the toolbar's layers.
"""
from __future__ import annotations

import logging
import time

from .. import config
from ..backlight import Backlight
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..radio.backend import Backend
from ..rec_timer import RecTimer
from ..scan import Scanner
from ..spacewx import SpaceWxFetcher
from ..state import RadioState, format_step
from . import style
from .adsb_page import AdsbPage
from .cw_panel import CwPanel
from .hf_panel import HfPanel
from .info_strips import DecoderPanel, RdsPanel
from .list_pages import NetworkPage, RecordingsPage, ScanLogPage
from .meter import Chip, MeterPanel
from .pickers import BandPicker, Keypad, LocationPage, MemoriesPage, MenuPage, ModePicker, Page
from .settings_page import SettingsPage
from .sliders import PopupSlider, SliderBar
from .softkeys import SoftKeyBar
from .spectrum import SpectrumWaterfall
from .vfo_bar import KNOB_NAMES, VfoBar, filter_text

log = logging.getLogger(__name__)


def _kv(key) -> int:
    """Integer value of a Qt key, for PyQt5 (int enums) and PyQt6 (Python enums) alike."""
    return int(getattr(key, "value", key))


class MainPage(Page):
    """VFO bar, meter and MENU / HIDE on top; panadapter and waterfall; the sliders area."""

    # Toolbar layers: what the first three keys do. The fourth is always FNC (next layer).
    TOOLBAR = (("BAND", "MODE", "KNOB"),
               ("MEM", "SCAN", "FILT"),
               ("NR", "A<>B", "REC"))

    menu_requested = Signal()

    def __init__(self, state: RadioState, rds_available: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.layer = 0
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(2)
        self.vfo_bar = VfoBar(state, rds_available)
        self.meter = MeterPanel(state)
        top.addWidget(self.vfo_bar, 1)
        top.addWidget(self.meter, 0)
        buttons = QtWidgets.QVBoxLayout()
        buttons.setSpacing(2)
        self.menu_btn = self._button("MENU", "menubtn", self.menu_requested)
        self.hide_btn = self._button("HIDE", "hidebtn", self._toggle_sliders)
        buttons.addWidget(self.menu_btn, 1)
        buttons.addWidget(self.hide_btn, 1)
        top.addLayout(buttons, 0)
        layout.addLayout(top)

        # Below: strips, spectrum and sliders on the left, HF conditions on the right.
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(2)
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(2)
        self.cw_panel = CwPanel(state)
        left.addWidget(self.cw_panel)
        self.rds_panel = RdsPanel(state)
        left.addWidget(self.rds_panel)
        self.decoder_panel = DecoderPanel(state)
        left.addWidget(self.decoder_panel)
        self.spectrum = SpectrumWaterfall(state)
        left.addWidget(self.spectrum, 1)
        self.sliders = SliderBar(state)
        left.addWidget(self.sliders)
        split.addLayout(left, 1)
        self.hf_panel = HfPanel(state)
        split.addWidget(self.hf_panel, 0)
        layout.addLayout(split, 1)

        self.vfo_bar.step_tapped.connect(self._step_tapped)
        state.changed.connect(self._on_changed)
        self.update_hf_visibility()
        self._show_sliders()
        # Spectrum tap-to-tune is wired by MainWindow so scanning can be cancelled.

    @staticmethod
    def _button(text: str, name: str, slot) -> QtWidgets.QPushButton:
        btn = QtWidgets.QPushButton(text)
        btn.setObjectName(name)
        btn.setFixedWidth(64)
        btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(slot)
        return btn

    def _on_changed(self, field: str) -> None:
        if field in ("hf_panel", "vfo_hz", "band_index"):
            self.update_hf_visibility()
        elif field == "sliders_visible":
            self._show_sliders()

    def update_hf_visibility(self) -> None:
        self.hf_panel.setVisible(self.state.hf_panel_visible)

    def _toggle_sliders(self) -> None:
        self.state.set_sliders_visible(not self.state.sliders_visible)

    def _show_sliders(self) -> None:
        self.sliders.setVisible(self.state.sliders_visible)
        self.hide_btn.setText("HIDE" if self.state.sliders_visible else "SHOW")

    def _step_tapped(self, hz: int) -> None:
        self.state.set_step_hz(hz)
        self.state.set_knob_function("TUNE")
        self.state.message.emit(f"Step {format_step(self.state.step_hz)}")

    # ------------------------------------------------------------------ toolbar
    def next_layer(self) -> None:
        self.layer = (self.layer + 1) % len(self.TOOLBAR)
        self.softkeys_changed.emit()

    def toolbar_action(self, index: int) -> str:
        """Name of what key *index* does on the current layer."""
        return self.TOOLBAR[self.layer][index] if index < 3 else "FNC"

    def _label(self, action: str) -> str:
        s = self.state
        if action == "KNOB":
            return "KNOB" if s.knob_function == "TUNE" else f"KNOB:{KNOB_NAMES[s.knob_function]}"
        if action == "SCAN":
            return "SCAN:MEM" if s.scan_source == "memory" else "SCAN:BAND"
        if action == "FILT":
            return f"FILT {filter_text(s.filter_hz)}"
        if action == "NR":
            return f"NR {s.dsp_nr_level}" if s.dsp_nr_level > 0 else "NR OFF"
        if action == "REC":
            return "STOP REC" if s.recording else "REC"
        return action

    def softkey_labels(self) -> list[str]:
        if self.state.scanning:
            hold = "RESUME" if self.state.scan_held else "HOLD"
            source = "MEM" if self.state.scan_source == "band" else "BAND"
            return [hold, "SKIP", source, "STOP"]
        return [self._label(action) for action in self.TOOLBAR[self.layer]] + [
            f"FNC {self.layer + 1}/{len(self.TOOLBAR)}"]

    def on_softkey(self, index: int) -> None:
        # Handled by the main window because most keys open other pages.
        pass


class MainWindow(QtWidgets.QMainWindow):
    (PAGE_MAIN, PAGE_BAND, PAGE_MODE, PAGE_KEYPAD, PAGE_SETTINGS, PAGE_MEMORIES,
     PAGE_RECORDINGS, PAGE_SCAN_LOG, PAGE_NETWORK, PAGE_ADSB, PAGE_LOCATION, PAGE_MENU) = range(12)

    _ACTIVITY_EVENTS = (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.TouchBegin,
                        QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.Wheel)

    def __init__(self, state: RadioState, backend: Backend, windowed: bool = False) -> None:
        super().__init__()
        self.state = state
        self.backend = backend
        self.scanner = Scanner(state, self)
        self.rec_timer = RecTimer(state, self)
        self.backlight = Backlight()
        self._closed = False
        self.setWindowTitle("PiSDR")
        style.apply_theme(state.theme)
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

        # Status line of every page but the main one, whose VFO bar shows all this and more.
        self.topbar = QtWidgets.QFrame()
        self.topbar.setObjectName("topbar")
        self.topbar.setFixedHeight(28)
        bar = QtWidgets.QHBoxLayout(self.topbar)
        bar.setContentsMargins(8, 0, 8, 0)
        bar.setSpacing(10)
        self.topbar_left = QtWidgets.QLabel("")
        self.topbar_left.setTextFormat(Qt.TextFormat.RichText)
        # Clip instead of growing when many status items are shown.
        self.topbar_left.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                       QtWidgets.QSizePolicy.Policy.Preferred)
        self.hf_chip = Chip("")
        self.hf_chip.setToolTip("HF conditions panel")
        self.hf_chip.clicked.connect(self._hf_chip_tapped)
        self.topbar_right = QtWidgets.QLabel("")
        self.topbar_right.setTextFormat(Qt.TextFormat.RichText)
        self.topbar_right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.topbar_left, 1)
        bar.addWidget(self.hf_chip, 0)
        bar.addWidget(self.topbar_right, 0)
        layout.addWidget(self.topbar)

        self.stack = QtWidgets.QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.main_page = MainPage(state, backend.rds_available)
        self.band_page = BandPicker(state)
        self.mode_page = ModePicker(state)
        self.keypad_page = Keypad(state)
        self.settings_page = SettingsPage(state, backend, self.backlight)
        self.memories_page = MemoriesPage(state)
        self.recordings_page = RecordingsPage(state, backend)
        self.scan_log_page = ScanLogPage(state)
        self.network_page = NetworkPage(state, backend)
        self.adsb_page = AdsbPage(state, backend)
        self.location_page = LocationPage(state)
        self.menu_page = MenuPage(state)
        pages = (self.main_page, self.band_page, self.mode_page, self.keypad_page, self.settings_page,
                 self.memories_page, self.recordings_page, self.scan_log_page, self.network_page,
                 self.adsb_page, self.location_page, self.menu_page)
        for page in pages:
            self.stack.addWidget(page)
            page.done.connect(lambda p=page: self._page_done(p))
            page.done_main.connect(self.show_main)
            page.softkeys_changed.connect(lambda p=page: self._page_softkeys(p))
        self.settings_page.quit_requested.connect(self.close)
        self.settings_page.open_page.connect(self._open_named)
        self.network_page.stop_requested.connect(self._leave_network)
        self.band_page.adsb_picked.connect(self._enter_adsb)
        self.adsb_page.stop_requested.connect(self._leave_adsb)
        self.menu_page.open_page.connect(self._open_named)
        self.main_page.menu_requested.connect(lambda: self.show_page(self.PAGE_MENU))
        self.main_page.vfo_bar.activated.connect(self._vfo_bar_tapped)
        self.main_page.sliders.assign.connect(self._assign_knob)
        self.main_page.meter.chip.clicked.connect(self._hf_chip_tapped)
        self._from_menu: Page | None = None      # a page opened from the menu goes back to the radio

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
        # Shown instead of a slider while the sliders area is hidden.
        self.popup = PopupSlider(state, central)

        # Screen dimming: any touch / key / knob counts as activity.
        self._last_activity = time.monotonic()
        self._dimmed = False
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._dim_timer = QtCore.QTimer(self)
        self._dim_timer.timeout.connect(self._check_dim)
        self._dim_timer.start(1000)
        self.backlight.set_percent(state.brightness)

        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._update_topbar)
        self._clock.start(1000)

        state.changed.connect(self._on_state_changed)
        state.message.connect(self.show_message)
        backend.status.connect(self.show_message)
        self.scanner.status.connect(self.show_message)
        self.rec_timer.status.connect(self.show_message)
        self.rec_timer.started.connect(self._timer_started)
        backend.spectrum_ready.connect(self.main_page.spectrum.push_spectrum)
        backend.center_changed.connect(self.main_page.spectrum.set_center)
        backend.cw_text.connect(state.append_cw_text)
        self.main_page.spectrum.tuned.connect(self._spectrum_tuned)

        # Solar / weather feeds for the HF conditions panel and the top-bar chip.
        self.spacewx = SpaceWxFetcher(state, self)
        self.spacewx.start()

        self.show_main()
        self._update_topbar()

    def _hf_chip_tapped(self) -> None:
        if self.stack.currentIndex() != self.PAGE_MAIN:
            self.show_main()
        self.state.toggle_hf_panel()

    def _spectrum_tuned(self, hz: float) -> None:
        if self.state.scanning:
            self.scanner.stop()
            self._refresh_main_softkeys()
        self.state.set_vfo(hz)

    def _timer_started(self) -> None:
        self.scanner.stop()
        self.backend.stop_playback()
        self._refresh_main_softkeys()

    # ------------------------------------------------------------------ pages
    def current_page(self) -> Page:
        return self.stack.currentWidget()  # type: ignore[return-value]

    def show_page(self, index: int) -> None:
        if self.state.network_mode and index != self.PAGE_NETWORK:
            return   # the dongle belongs to rtl_tcp; only the network page's STOP gets out
        if self.state.adsb_mode and index != self.PAGE_ADSB:
            return   # ... or to the ADS-B decoder; only the map's BACK gets out
        if index != self.PAGE_MAIN and self.state.scanning:
            self.scanner.stop()
        if index in (self.PAGE_MAIN, self.PAGE_MENU):
            self._from_menu = None
        self.topbar.setVisible(index != self.PAGE_MAIN)
        self.popup.hide()
        self.stack.setCurrentIndex(index)
        # Waterfall paints compete with ALSA on the Pi; pause FFT while off the main page.
        self.backend.set_spectrum_enabled(index == self.PAGE_MAIN)
        page = self.current_page()
        page.on_shown()
        self.softkeys.set_labels(page.softkey_labels())

    def show_main(self) -> None:
        self.show_page(self.PAGE_MAIN)

    def toggle_page(self, index: int) -> None:
        self.show_main() if self.stack.currentIndex() == index else self.show_page(index)

    def _page_done(self, page: Page) -> None:
        if page.return_to == "settings" and page is not self._from_menu:
            self.show_page(self.PAGE_SETTINGS)
        else:
            self.show_main()

    def _page_softkeys(self, page: Page) -> None:
        if self.current_page() is page:
            self.softkeys.set_labels(page.softkey_labels())

    def _open_named(self, name: str) -> None:
        """Open what a Settings row, a menu tile or a VFO bar indicator points at."""
        pages = {"band": self.PAGE_BAND, "mode": self.PAGE_MODE, "memories": self.PAGE_MEMORIES,
                 "keypad": self.PAGE_KEYPAD, "recordings": self.PAGE_RECORDINGS,
                 "scan_log": self.PAGE_SCAN_LOG, "location": self.PAGE_LOCATION}
        from_menu = self.stack.currentIndex() in (self.PAGE_MENU, self.PAGE_MAIN)
        if name == "network":
            self._enter_network()
        elif name == "adsb":
            self._enter_adsb()
        elif name == "hf_panel":
            self.show_main()
            self.state.toggle_hf_panel()
        elif name.startswith("settings:"):
            target = name.partition(":")[2]
            self.show_page(self.PAGE_SETTINGS)
            if self.stack.currentIndex() == self.PAGE_SETTINGS:
                if target in self.settings_page.TABS:
                    self.settings_page.select_tab(target)
                else:
                    self.settings_page.select_row(target)
        elif name in pages:
            self.show_page(pages[name])
            if from_menu and self.stack.currentIndex() == pages[name]:
                self._from_menu = self.current_page()

    def _vfo_bar_tapped(self, action: str) -> None:
        """A tap on the VFO bar: a dial, or one of the indicators."""
        s = self.state
        if action == "swap":
            self._swap_vfo()
        elif action == "step":
            s.cycle_step()
        elif action == "zoom":
            s.cycle_spectrum_zoom(1)
        elif action == "knob":
            s.cycle_knob_function()
        elif action.startswith("knob:"):
            self._assign_knob(action.partition(":")[2])
        elif action:
            self._open_named(action)

    def _assign_knob(self, function: str) -> None:
        """Give the knob to a slider, or back to tuning if it already has it."""
        self.state.set_knob_function("TUNE" if self.state.knob_function == function else function)

    def _swap_vfo(self) -> None:
        if self.state.scanning:
            self.scanner.stop()
        self.state.swap_vfo()
        self.show_message(f"A<>B  {self.state.vfo_hz / 1e6:.4f} MHz {self.state.mode}")

    def _enter_network(self) -> None:
        self.scanner.stop()
        self.show_message("Handing the dongle to rtl_tcp...")
        QtWidgets.QApplication.processEvents()
        error = self.backend.start_network_mode()
        if error:
            self.show_message(error)
            return
        self.show_page(self.PAGE_NETWORK)

    def _leave_network(self) -> None:
        self.show_message("Taking the dongle back...")
        QtWidgets.QApplication.processEvents()
        error = self.backend.stop_network_mode()
        self.show_main()
        self.show_message(error or "Radio back on the dongle")

    def _enter_adsb(self) -> None:
        """Lend the dongle to the ADS-B decoder and show its map (from the band picker or Settings)."""
        if self.state.adsb_mode:
            return
        self.scanner.stop()
        self.show_message("Handing the dongle to the ADS-B decoder...")
        QtWidgets.QApplication.processEvents()
        error = self.backend.start_adsb_mode()
        if error:
            self.show_message(error)
            return
        self.toast.hide()
        self.show_page(self.PAGE_ADSB)

    def _leave_adsb(self) -> None:
        self.show_message("Taking the dongle back...")
        QtWidgets.QApplication.processEvents()
        error = self.backend.stop_adsb_mode()
        self.show_main()
        self.show_message(error or "Radio back on the dongle")

    def _refresh_main_softkeys(self) -> None:
        if self.stack.currentIndex() == self.PAGE_MAIN:
            self.softkeys.set_labels(self.main_page.softkey_labels())

    # ------------------------------------------------------------------ control routing
    def key_pressed(self, index: int) -> None:
        self._activity()
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
        self._toolbar(self.main_page.toolbar_action(index), long=False)
        self._refresh_main_softkeys()

    def key_long_pressed(self, index: int) -> None:
        self._activity()
        if self.stack.currentIndex() != self.PAGE_MAIN:
            if not self.current_page().on_softkey_long(index):
                self.show_main()
            else:
                self.softkeys.set_labels(self.current_page().softkey_labels())
            return
        if self.state.scanning:
            if index == 1:
                self.scanner.lockout_current()   # long SKIP: never stop here again
            elif index == 3:
                self.scanner.stop()
            self._refresh_main_softkeys()
            return
        self._toolbar(self.main_page.toolbar_action(index), long=True)
        self._refresh_main_softkeys()

    def _toolbar(self, action: str, long: bool) -> None:
        """One toolbar key on the main page: a short press, or held."""
        s = self.state
        if action == "BAND":
            s.cycle_band() if long else self.show_page(self.PAGE_BAND)
        elif action == "MODE":
            s.cycle_mode() if long else self.show_page(self.PAGE_MODE)
        elif action == "KNOB":
            if long:
                self.show_page(self.PAGE_SETTINGS)
            else:
                s.cycle_knob_function()
                self.show_message(f"Knob: {s.knob_function}")
        elif action == "MEM":
            self.scanner.start() if long else self.show_page(self.PAGE_MEMORIES)
        elif action == "SCAN":
            self.scanner.cycle_source() if long else self.scanner.start()
        elif action == "FILT":
            if len(config.FILTER_WIDTHS[s.mode]) < 2:
                self.show_message(f"{s.mode} has one filter width")
            else:
                s.step_filter(-1 if long else 1)
                self.show_message(f"Filter {format_step(s.filter_hz)}")
        elif action == "NR":
            if long:
                s.set_dsp_nb_level(0 if s.dsp_nb_level else 5)
                self.show_message(f"Noise blanker {s.dsp_nb_level or 'off'}")
            else:
                s.set_dsp_nr_level(0 if s.dsp_nr_level else 5)
                self.show_message(f"Noise reduction {s.dsp_nr_level or 'off'}")
        elif action == "A<>B":
            if long:
                s.copy_vfo_to_b()
                self.show_message("A > B")
            else:
                self._swap_vfo()
        elif action == "REC":
            self.show_page(self.PAGE_RECORDINGS) if long else s.toggle_recording()
        elif action == "FNC":
            self.show_page(self.PAGE_MENU) if long else self.main_page.next_layer()

    def knob_turned(self, detents: int) -> None:
        self._activity()
        if self.state.scanning and self.stack.currentIndex() == self.PAGE_MAIN:
            # Manual tune while scanning cancels the scan (same as a handheld).
            self.scanner.stop()
            self._refresh_main_softkeys()
        if not self.current_page().on_knob(detents):
            self.state.knob_turned(detents)
            function = self.state.knob_function
            if (function != "TUNE" and not self.state.sliders_visible
                    and self.stack.currentIndex() == self.PAGE_MAIN):
                self.popup.move((self.width() - self.popup.width()) // 2, 210)
                self.popup.show_level(function)

    def encoder_pushed(self) -> None:
        self._activity()
        if self.state.scanning and self.stack.currentIndex() == self.PAGE_MAIN:
            self.scanner.toggle_hold()
            self._refresh_main_softkeys()
            return
        if not self.current_page().on_encoder_push():
            self.state.cycle_step()

    def encoder_held(self) -> None:
        self._activity()
        self.toggle_page(self.PAGE_KEYPAD)

    # ------------------------------------------------------------------ screen brightness
    def _activity(self) -> None:
        self._last_activity = time.monotonic()
        if self._dimmed:
            self._dimmed = False
            self.backlight.set_percent(self.state.brightness)

    def _check_dim(self) -> None:
        after = self.state.dim_after_s
        if after and not self._dimmed and time.monotonic() - self._last_activity >= after:
            self._dimmed = True
            self.backlight.set_percent(min(self.state.brightness, config.DIM_LEVEL_PCT))

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
        if field in ("knob_function", "recording", "scanning", "scan_source", "memories",
                     "mode", "filter_index", "dsp_nr_level"):
            self._refresh_main_softkeys()
        if field == "theme":
            self._apply_theme(self.state.theme)
        elif field == "brightness" and not self._dimmed:
            self.backlight.set_percent(self.state.brightness)
        elif field == "dim_after_s":
            self._activity()
        if field not in ("signal_db", "cw_text", "decoder_lines", "rds", "aircraft"):
            self._update_topbar()

    def _apply_theme(self, name: str) -> None:
        style.apply_theme(name)
        self.setStyleSheet(style.STYLESHEET)
        mp = self.main_page
        for w in (mp.spectrum, mp.cw_panel, mp.rds_panel, mp.decoder_panel, mp.hf_panel,
                  mp.vfo_bar, mp.meter, mp.sliders):
            retheme = getattr(w, "retheme", None)
            if retheme:
                retheme()
            else:
                w.update()
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
        if s.adsb_mode:      # the receiver is paused: its mode, step and squelch mean nothing here
            parts = [f"<span style='color:{acc}'><b>ADS-B MAP</b></span>",
                     f"{config.ADSB_FREQ_HZ / 1e6:.0f} MHz",
                     f"<span style='color:{dim}'>receiver paused</span>"]
        if s.network_mode:
            parts.append(f"<span style='color:{acc}'><b>NET</b></span>")
        if s.bias_tee:
            parts.append(f"<span style='color:{acc}'><b>BIAS</b></span>")
        if s.recording:
            parts.append(f"<span style='color:{style.REC}'><b>&#9679; REC</b></span>")
        elif s.timer_armed:
            parts.append(f"<span style='color:{dim}'>TMR</span> {s.timer_start_min // 60:02d}:"
                         f"{s.timer_start_min % 60:02d}Z")
        if s.playing:
            parts.append(f"<span style='color:{style.OK}'><b>&#9654; PLAY</b></span>")
        if s.decoder != "OFF":
            parts.append(f"<span style='color:{acc}'><b>{s.decoder}</b></span>")
        if s.dsp_nr_level > 0:
            parts.append(f"<span style='color:{acc}'><b>NR{s.dsp_nr_level}</b></span>")
        if s.dsp_nb_level > 0:
            parts.append(f"<span style='color:{acc}'><b>NB{s.dsp_nb_level}</b></span>")
        if s.dsp_notch:
            parts.append(f"<span style='color:{acc}'><b>NCH</b></span>")
        if s.scanning:
            held = "HOLD" if s.scan_held else "SCAN"
            src = "MEM" if s.scan_source == "memory" else "BAND"
            prio = "+P" if s.priority else ""
            parts.append(f"<span style='color:{acc}'><b>{held}:{src}{prio}</b></span>")
        knob = f"<span style='color:{acc}'><b>KNOB {'SELECT' if s.adsb_mode else s.knob_function}</b></span>"
        clock = time.strftime("%H:%MZ", time.gmtime()) if s.clock_utc else time.strftime("%H:%M")
        self.topbar_left.setText(" &nbsp;&nbsp; ".join(parts))
        self.topbar_right.setText(f"{knob} &nbsp;&nbsp; {clock}")
        wx = s.space_wx
        if wx is not None and wx.sfi is not None:
            chip = f"\u2600 SFI {wx.sfi}  K{wx.k_index if wx.k_index is not None else '-'}"
        else:
            chip = "\u2600 HF"
        for hf_chip in (self.hf_chip, self.main_page.meter.chip):
            hf_chip.setText(chip)
            hf_chip.set_active(s.hf_panel_visible)
        self.main_page.meter.clock.setText(clock)

    # ------------------------------------------------------------------ events
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        etype = event.type()
        if etype in self._ACTIVITY_EVENTS:
            self._activity()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        """Keyboard emulation of the panel controls for development on a PC."""
        key = _kv(event.key())
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if key == _kv(Qt.Key.Key_Q) and ctrl:
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
        elif self.stack.currentIndex() == self.PAGE_LOCATION and _kv(Qt.Key.Key_0) <= key <= _kv(Qt.Key.Key_9):
            self.location_page.key(chr(key))
        elif _kv(Qt.Key.Key_0) <= key <= _kv(Qt.Key.Key_9) or key == _kv(Qt.Key.Key_Period):
            if self.stack.currentIndex() != self.PAGE_KEYPAD:
                self.show_page(self.PAGE_KEYPAD)
            if self.stack.currentIndex() == self.PAGE_KEYPAD:
                self.keypad_page.key(chr(key))
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        self.knob_turned(1 if event.angleDelta().y() > 0 else -1)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._closed:      # a signal and the user can both ask; shut down once
            self._closed = True
            QtWidgets.QApplication.instance().removeEventFilter(self)
            self.scanner.stop()
            self.spacewx.stop()
            self.backend.stop_playback()
            self.state.save()
            self.backend.stop()   # also finalises a recording in progress
        super().closeEvent(event)
