"""VFO bar: the strip across the top of the main screen, laid out like piHPSDR's.

Three lines. The top one is the receiver chain (mode and filter width, AGC, noise
reduction, notch, squelch, decoders ...), the middle one the VFO A dial with VFO B beside
it, and the bottom one how the radio is being operated (band, step, zoom, what the knob
does, scanning). As in piHPSDR an indicator is grey while its feature is off and in the
accent colour while it is on, so the whole receiver can be read at a glance.

Everything is also a touch target: a digit of VFO A selects that decade as the tuning
step, a long press on VFO A opens the keypad, VFO B swaps A and B, and an indicator opens
the page (or the Settings row) that changes it. What a tap does is reported through
:attr:`VfoBar.activated`; the main window carries it out.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState, format_step
from . import style

KNOB_NAMES = {"TUNE": "TUNE", "VOLUME": "AF", "SQUELCH": "SQL", "GAIN": "RF"}   # as on the sliders

HEIGHT = 92
_ROW_PX = 13                 # font size of the two indicator lines
_TOP_BASE, _BOTTOM_BASE = 16, 87          # text baselines of the indicator lines
_TOP_BAND = (0, 23)          # y, height of the touch band around each line
_BOTTOM_BAND = (70, 22)
_DIAL_Y, _DIAL_H = 23, 47
_A_RIGHT = 306               # right edge of the VFO A digits
_B_LEFT = 326
_HOLD_MS = 600               # long press on VFO A: keypad


@dataclass(frozen=True)
class Indicator:
    key: str
    text: str
    kind: str = "off"        # "off" grey, "on" accent, "info" plain, "alert" red
    action: str = ""         # what a tap asks for, e.g. "mode" or "settings:Notch"


def filter_text(hz: float) -> str:
    """Filter width the way piHPSDR writes it: 2.4k, 12.5k, 500."""
    return f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:g}"


def top_indicators(s: RadioState, rds_available: bool = True) -> tuple[list[Indicator], list[Indicator]]:
    """The receiver chain, left to right, and what is right-aligned on the same line."""
    mode = f"{s.mode} {filter_text(s.filter_hz)}"
    if s.mode == "CW":
        if s.cw_wpm > 0:
            mode += f" {s.cw_wpm:.0f}wpm"
        mode += f" {s.cw_pitch_hz}Hz"

    def flag(key: str, text: str, on: bool, row: str) -> Indicator:
        return Indicator(key, text, "on" if on else "off", f"settings:{row}")

    squelched = s.squelch_db > config.SQUELCH_MIN_DB
    left = [
        Indicator("mode", mode, "on", "mode"),
        flag("agc", "AGC", s.agc, "Tuner AGC"),
        flag("nr", f"NR{s.dsp_nr_level}" if s.dsp_nr_level > 0 else "NR", s.dsp_nr_level > 0, "Noise reduction"),
        flag("nb", f"NB{s.dsp_nb_level}" if s.dsp_nb_level > 0 else "NB", s.dsp_nb_level > 0, "Noise blanker"),
        flag("notch", "NCH", s.dsp_notch, "Notch"),
        flag("hpf", "HPF", s.dsp_hpf, "High-pass"),
        flag("lpf", "LPF", s.dsp_lpf, "Low-pass"),
        Indicator("squelch", f"SQL{s.squelch_db:.0f}" if squelched else "SQL",
                  "on" if squelched else "off", "knob:SQUELCH"),
        flag("rds", "RDS", s.rds_enabled and s.mode == "WFM" and rds_available, "RDS on FM broadcast"),
        flag("decoder", s.decoder if s.decoder != "OFF" else "DEC", s.decoder != "OFF", "Data decoder"),
        flag("bias", "BIAS", s.bias_tee, "Bias tee (4.5 V)"),
    ]
    right = [Indicator("rec", "\u25cf REC" if s.recording else "REC",
                       "alert" if s.recording else "off", "settings:Record now")]
    return left, right


def bottom_indicators(s: RadioState) -> tuple[list[Indicator], list[Indicator]]:
    """How the radio is being operated. Left items are dropped from the end if space runs out."""
    gain = "AGC" if s.agc else f"{s.gain_db:.0f}dB"
    left = [
        Indicator("band", s.band.name, "info", "band"),
        Indicator("step", f"Step {format_step(s.step_hz)}", "on", "step"),
        Indicator("zoom", f"Zoom {s.spectrum_zoom}", "on" if s.spectrum_zoom > 1 else "off", "zoom"),
        Indicator("knob", f"Knob {KNOB_NAMES.get(s.knob_function, s.knob_function)}",
                  "on" if s.knob_function != "TUNE" else "off", "knob"),
        Indicator("volume", f"AF {s.volume}", "info", "knob:VOLUME"),
        Indicator("gain", f"RF {gain}", "info", "knob:GAIN"),
    ]
    right = []
    if s.scanning:
        text = ("HOLD" if s.scan_held else "SCAN") + (":MEM" if s.scan_source == "memory" else ":BAND")
        right.append(Indicator("scan", text + ("+P" if s.priority else ""), "on", ""))
    if s.playing:
        right.append(Indicator("play", "\u25b6 PLAY", "on", "recordings"))
    if s.timer_running:
        right.append(Indicator("timer", "TMR REC", "alert", "settings:Timer"))
    elif s.timer_armed:
        right.append(Indicator("timer", f"TMR {s.timer_start_min // 60:02d}:{s.timer_start_min % 60:02d}Z",
                               "on", "settings:Timer"))
    if s.network_mode:
        right.append(Indicator("net", "NET", "on", ""))
    return left, right


def _mono(px: int) -> QtGui.QFont:
    font = QtGui.QFont("DejaVu Sans Mono")
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPixelSize(px)
    font.setBold(True)
    return font


def _sans(widget: QtWidgets.QWidget, px: int, bold: bool = False) -> QtGui.QFont:
    font = QtGui.QFont(widget.font())      # the widget's family, so that measuring matches drawing
    font.setPixelSize(px)
    font.setBold(bold)
    return font


def _split(hz: int) -> tuple[str, str, str]:
    """(MHz, kHz, Hz) digit groups of a frequency: 146520000 -> ("146", "520", "000")."""
    hz = max(0, int(hz))
    return str(hz // 1_000_000), f"{hz // 1000 % 1000:03d}", f"{hz % 1000:03d}"


class VfoBar(QtWidgets.QWidget):
    step_tapped = Signal(int)    # Hz: the decade of the VFO A digit that was tapped
    activated = Signal(str)      # "keypad", "swap", or an Indicator.action

    IGNORED_FIELDS = frozenset({"signal_db", "cw_text", "decoder_lines", "rds", "aircraft", "space_wx",
                                "memories", "scan_log", "lockouts", "location"})

    def __init__(self, state: RadioState, rds_available: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.rds_available = rds_available
        self.setFixedHeight(HEIGHT)
        self.setMinimumWidth(480)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._digits: list[tuple[QtCore.QRect, int]] = []      # VFO A digit cells and their weights
        self._hits: list[tuple[QtCore.QRect, Indicator]] = []  # indicator touch bands
        self._press: QtCore.QPoint | None = None
        self._hold = QtCore.QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(_HOLD_MS)
        self._hold.timeout.connect(self._held)
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field not in self.IGNORED_FIELDS:
            self.update()

    def retheme(self) -> None:
        self.update()

    # ------------------------------------------------------------------ what is shown
    def indicators(self) -> list[Indicator]:
        """Every indicator, whether or not there was room to draw it."""
        top = top_indicators(self.state, self.rds_available)
        bottom = bottom_indicators(self.state)
        return [*top[0], *top[1], *bottom[0], *bottom[1]]

    def text(self) -> str:
        """The indicators that are lit (or plain values), as one line of text."""
        return "  ".join(ind.text for ind in self.indicators() if ind.kind != "off")

    def dial_a_rect(self) -> QtCore.QRect:
        return QtCore.QRect(0, _DIAL_Y, _A_RIGHT + 8, _DIAL_H)

    def dial_b_rect(self) -> QtCore.QRect:
        return QtCore.QRect(_B_LEFT - 8, _DIAL_Y, self.width() - _B_LEFT + 8, _DIAL_H)

    def digit_rect(self, weight: int) -> QtCore.QRect | None:
        """Where the VFO A digit worth *weight* Hz was last drawn."""
        return next((rect for rect, w in self._digits if w == weight), None)

    def indicator_rect(self, key: str) -> QtCore.QRect | None:
        """Touch band of an indicator, or None if it did not fit on its line."""
        return next((rect for rect, ind in self._hits if ind.key == key), None)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), QtGui.QColor(style.PANEL))
        self._hits = []
        self._paint_row(p, _TOP_BASE, _TOP_BAND, *top_indicators(self.state, self.rds_available))
        self._paint_row(p, _BOTTOM_BASE, _BOTTOM_BAND, *bottom_indicators(self.state))
        self._paint_dial_a(p)
        self._paint_dial_b(p)
        p.end()

    def _paint_row(self, p: QtGui.QPainter, baseline: int, band: tuple[int, int],
                   left: list[Indicator], right: list[Indicator]) -> None:
        colours = {"off": style.TEXT_DIM, "on": style.ACCENT, "info": style.TEXT, "alert": style.REC}
        fonts = {False: _sans(self, _ROW_PX), True: _sans(self, _ROW_PX, bold=True)}
        metrics = {bold: QtGui.QFontMetrics(font) for bold, font in fonts.items()}
        gap = 13

        def draw(ind: Indicator, x: int, width: int) -> None:
            bold = ind.kind != "off"
            p.setFont(fonts[bold])
            p.setPen(QtGui.QColor(colours[ind.kind]))
            p.drawText(x, baseline, ind.text)
            if ind.action:
                self._hits.append((QtCore.QRect(x - gap // 2, band[0], width + gap, band[1]), ind))

        limit = self.width() - 8
        for ind in reversed(right):
            width = metrics[ind.kind != "off"].horizontalAdvance(ind.text)
            limit -= width
            draw(ind, limit, width)
            limit -= gap
        x = 8
        for ind in left:
            width = metrics[ind.kind != "off"].horizontalAdvance(ind.text)
            if x + width > limit:
                break                         # the rest are the least important: leave them out
            draw(ind, x, width)
            x += width + gap

    def _paint_dial_a(self, p: QtGui.QPainter) -> None:
        s = self.state
        big, small = _mono(46), _mono(27)
        fm_big, fm_small = QtGui.QFontMetrics(big), QtGui.QFontMetrics(small)
        big_w, small_w = fm_big.horizontalAdvance("0"), fm_small.horizontalAdvance("0")
        dot_w = big_w // 2
        baseline = _DIAL_Y + (_DIAL_H + fm_big.ascent() - fm_big.descent()) // 2 + 1
        mhz, khz, hz = _split(s.vfo_hz)
        # Highlight the decade the knob steps (the one below for 5 k / 12.5 k / 25 k steps).
        weights = [10 ** n for n in range(10)]
        lit = max((w for w in weights if w <= s.step_hz), default=1)
        lit_colour = QtGui.QColor(style.ACCENT if s.knob_function == "TUNE" else style.ACCENT_2)

        self._digits = []
        x = _A_RIGHT - 3 * small_w
        cells = [(ch, x + i * small_w, small_w, small, 10 ** (2 - i)) for i, ch in enumerate(hz)]
        x -= 5 + 3 * big_w
        cells += [(ch, x + i * big_w, big_w, big, 10 ** (5 - i)) for i, ch in enumerate(khz)]
        dot_x = x - dot_w
        x = dot_x - len(mhz) * big_w
        cells += [(ch, x + i * big_w, big_w, big, 10 ** (5 + len(mhz) - i)) for i, ch in enumerate(mhz)]
        for ch, cx, width, font, weight in cells:
            self._digits.append((QtCore.QRect(cx, _DIAL_Y, width, _DIAL_H), weight))
            p.setFont(font)
            if weight == lit:
                p.setPen(QtGui.QPen(lit_colour, 3))
                p.drawLine(cx + 3, _DIAL_Y + _DIAL_H - 2, cx + width - 3, _DIAL_Y + _DIAL_H - 2)
                p.setPen(lit_colour)
            else:
                p.setPen(QtGui.QColor(style.TEXT))
            p.drawText(cx, baseline, ch)
        p.setFont(big)
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        p.drawText(QtCore.QRect(dot_x - 6, _DIAL_Y, dot_w + 12, baseline - _DIAL_Y + fm_big.descent()),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, ".")
        p.setFont(_sans(self, 16, bold=True))
        p.setPen(QtGui.QColor(style.ACCENT))
        p.drawText(8, baseline - 14, "A")

    def _paint_dial_b(self, p: QtGui.QPainter) -> None:
        b = self.state.vfo_b
        big, small = _mono(27), _mono(18)
        fm_big = QtGui.QFontMetrics(big)
        baseline = _DIAL_Y + 27
        mhz, khz, hz = _split(b["vfo_hz"])
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        p.setFont(_sans(self, 16, bold=True))
        p.drawText(_B_LEFT, baseline, "B")
        x = _B_LEFT + 20
        p.setFont(big)
        head = f"{mhz}.{khz}"
        p.drawText(x, baseline, head)
        p.setFont(small)
        p.drawText(x + fm_big.horizontalAdvance(head) + 3, baseline, hz)
        p.setFont(_sans(self, 12))
        p.drawText(x, baseline + 16, f"{b['mode']}  \u00b7  {format_step(b['step_hz'])}")

    # ------------------------------------------------------------------ touch
    @staticmethod
    def _pos(event) -> QtCore.QPoint:
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press = self._pos(event)
        if self.dial_a_rect().contains(self._press):
            self._hold.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._hold.stop()
        press, self._press = self._press, None
        if press is None:
            return                            # the long press already opened the keypad
        self.tap(press)

    def _held(self) -> None:
        if self._press is not None:
            self._press = None
            self.activated.emit("keypad")

    def tap(self, pos: QtCore.QPoint) -> None:
        """What a short tap at *pos* does."""
        for rect, weight in self._digits:
            if rect.adjusted(-1, 0, 1, 0).contains(pos):
                if config.STEPS_HZ[0] <= weight <= config.STEPS_HZ[-1]:
                    self.step_tapped.emit(weight)
                return
        if self.dial_b_rect().contains(pos):
            self.activated.emit("swap")
            return
        for rect, ind in self._hits:
            if rect.contains(pos):
                self.activated.emit(ind.action)
                return
