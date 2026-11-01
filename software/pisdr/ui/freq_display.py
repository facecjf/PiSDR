"""Big frequency readout with tap-to-select-step, plus a simple S-meter."""
from __future__ import annotations

from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState, format_freq
from . import style


class FreqDisplay(QtWidgets.QWidget):
    """Draws ``14.200.000`` and highlights the digit matching the tuning step.

    Tapping a digit selects that decade as the tuning step; the widget emits
    :attr:`step_tapped` with the step in Hz.
    """

    step_tapped = Signal(int)

    DIGIT_WEIGHTS_FROM_RIGHT = (1, 10, 100, 1_000, 10_000, 100_000,
                                1_000_000, 10_000_000, 100_000_000, 1_000_000_000)

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setMinimumHeight(72)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._font = QtGui.QFont("DejaVu Sans Mono")
        self._font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        self._font.setPixelSize(58)
        self._font.setBold(True)
        self._digit_rects: list[tuple[QtCore.QRect, int]] = []   # (rect, weight)
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field in ("vfo_hz", "step_index", "knob_function", "mode"):
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), QtGui.QColor(style.PANEL))
        p.setFont(self._font)
        fm = QtGui.QFontMetrics(self._font)

        text = format_freq(self.state.vfo_hz)
        digit_w = fm.horizontalAdvance("0")
        dot_w = fm.horizontalAdvance(".")
        total_w = sum(digit_w if c != "." else dot_w for c in text)
        unit_font = QtGui.QFont(self._font)
        unit_font.setPixelSize(16)
        unit_font.setBold(False)
        unit_w = QtGui.QFontMetrics(unit_font).horizontalAdvance("MHz") + 10

        x = self.width() - total_w - unit_w - 12
        y_base = (self.height() + fm.ascent() - fm.descent()) // 2
        step = self.state.step_hz
        tuning = self.state.knob_function == "TUNE"

        # Which decade digit to highlight: exact decade, or the decade below for 5k/12.5k/25k.
        hl_weight = max((w for w in self.DIGIT_WEIGHTS_FROM_RIGHT if w <= step), default=1)

        self._digit_rects = []
        digits_only = [c for c in text if c != "."]
        n_digits = len(digits_only)
        digit_index = 0
        for ch in text:
            if ch == ".":
                p.setPen(QtGui.QColor(style.TEXT_DIM))
                p.drawText(x, y_base, ch)
                x += dot_w
                continue
            weight = self.DIGIT_WEIGHTS_FROM_RIGHT[n_digits - 1 - digit_index]
            rect = QtCore.QRect(x, 4, digit_w, self.height() - 8)
            self._digit_rects.append((rect, weight))
            if weight == hl_weight:
                color = QtGui.QColor(style.ACCENT if tuning else style.ACCENT_2)
                p.setPen(QtGui.QPen(color, 3))
                p.drawLine(x + 3, self.height() - 7, x + digit_w - 3, self.height() - 7)
                p.setPen(color)
            else:
                p.setPen(QtGui.QColor(style.TEXT))
            p.drawText(x, y_base, ch)
            x += digit_w
            digit_index += 1

        p.setFont(unit_font)
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        p.drawText(x + 8, y_base, "MHz")
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        for rect, weight in self._digit_rects:
            if rect.adjusted(-2, -4, 2, 4).contains(pos):
                if 10 <= weight <= 1_000_000:
                    self.step_tapped.emit(weight)
                return
        super().mousePressEvent(event)


class SMeter(QtWidgets.QWidget):
    """Horizontal signal-strength bar, S1..S9 then +dB, driven by ``state.signal_db``."""

    S9_DB = -73.0           # dBm equivalent for S9 (nominal for the simulated scale)
    MIN_DB = -127.0         # S0
    MAX_DB = -13.0          # S9+60

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setFixedWidth(190)
        self.setMinimumHeight(72)
        self._level = self.MIN_DB
        self._peak = self.MIN_DB
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field != "signal_db":
            return
        target = max(self.MIN_DB, min(self.MAX_DB, self.state.signal_db))
        # Fast attack, slow decay like a real meter needle.
        self._level = target if target > self._level else self._level - (self._level - target) * 0.15
        self._peak = max(target, self._peak - 0.4)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(style.PANEL))
        margin = 10
        bar = QtCore.QRectF(margin, 26, self.width() - 2 * margin, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(style.METER_BG))
        p.drawRoundedRect(bar, 3, 3)

        def frac(db: float) -> float:
            return (db - self.MIN_DB) / (self.MAX_DB - self.MIN_DB)

        f_level = frac(self._level)
        s9 = frac(self.S9_DB)
        green = QtCore.QRectF(bar.left(), bar.top(), bar.width() * min(f_level, s9), bar.height())
        p.setBrush(QtGui.QColor(style.OK))
        p.drawRoundedRect(green, 3, 3)
        if f_level > s9:
            red = QtCore.QRectF(bar.left() + bar.width() * s9, bar.top(), bar.width() * (f_level - s9), bar.height())
            p.setBrush(QtGui.QColor(style.REC))
            p.drawRect(red)
        px = bar.left() + bar.width() * frac(self._peak)
        p.setPen(QtGui.QPen(QtGui.QColor(style.TEXT), 2))
        p.drawLine(QtCore.QPointF(px, bar.top() - 2), QtCore.QPointF(px, bar.bottom() + 2))

        # Scale labels.
        font = QtGui.QFont(self.font())
        font.setPixelSize(11)
        p.setFont(font)
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        for label, db in (("1", -121), ("3", -109), ("5", -97), ("7", -85), ("9", -73), ("+20", -53), ("+40", -33), ("+60", -13)):
            x = bar.left() + bar.width() * frac(db)
            p.drawText(QtCore.QRectF(x - 14, bar.bottom() + 2, 28, 14), Qt.AlignmentFlag.AlignCenter, label)
        p.drawText(QtCore.QRectF(margin, 4, 60, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "S")
        p.setPen(QtGui.QColor(style.TEXT))
        p.drawText(QtCore.QRectF(self.width() - margin - 80, 4, 80, 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{self._level:5.0f} dB")
        p.end()
