"""Meter area, right of the VFO bar: the HF conditions chip, the clock and the S-meter."""
from __future__ import annotations

from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from . import style

S9_DB = -73.0            # level shown as S9 (nominal: the scale is as good as the S-meter calibration)
S_UNIT_DB = 6.0


def s_units(db: float) -> str:
    """'S7', 'S9', 'S9+20': the reading the way it is spoken."""
    if db >= S9_DB:
        over = round(db - S9_DB)
        return f"S9+{over}" if over >= 1 else "S9"
    return f"S{max(0, int((db - S9_DB) // S_UNIT_DB) + 9)}"


class Chip(QtWidgets.QLabel):
    """Small tappable status label (the HF conditions summary)."""

    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("chip")
        self.setProperty("active", False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, on: bool) -> None:
        if self.property("active") != on:
            self.setProperty("active", on)
            style.repolish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class SMeter(QtWidgets.QWidget):
    """S-units and dB as text over a bar: S1..S9 in green, above S9 in red, with a peak tick."""

    MIN_DB = -127.0         # S0
    MAX_DB = -13.0          # S9+60

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setMinimumSize(150, 60)
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

    def reading(self) -> tuple[str, str]:
        return s_units(self._level), f"{self._level:.0f} dB"

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        margin = 8
        bar = QtCore.QRectF(margin, 27, self.width() - 2 * margin, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(style.METER_BG))
        p.drawRoundedRect(bar, 3, 3)

        def frac(db: float) -> float:
            return (db - self.MIN_DB) / (self.MAX_DB - self.MIN_DB)

        level, s9 = frac(self._level), frac(S9_DB)
        p.setBrush(QtGui.QColor(style.OK))
        p.drawRoundedRect(QtCore.QRectF(bar.left(), bar.top(), bar.width() * min(level, s9), bar.height()), 3, 3)
        if level > s9:
            p.setBrush(QtGui.QColor(style.REC))
            p.drawRect(QtCore.QRectF(bar.left() + bar.width() * s9, bar.top(),
                                     bar.width() * (level - s9), bar.height()))
        px = bar.left() + bar.width() * frac(self._peak)
        p.setPen(QtGui.QPen(QtGui.QColor(style.TEXT), 2))
        p.drawLine(QtCore.QPointF(px, bar.top() - 2), QtCore.QPointF(px, bar.bottom() + 2))

        font = QtGui.QFont(self.font())
        font.setPixelSize(10)
        p.setFont(font)
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        scale = (("1", -121), ("3", -109), ("5", -97), ("7", -85), ("9", -73), ("+20", -53), ("+40", -33))
        for label, db in scale:                # the bar ends at S9+60
            x = bar.left() + bar.width() * frac(db)
            p.drawText(QtCore.QRectF(x - 14, bar.bottom() + 2, 28, 13), Qt.AlignmentFlag.AlignCenter, label)

        units, db_text = self.reading()
        font.setPixelSize(19)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QtGui.QColor(style.REC if self._level > S9_DB else style.TEXT))
        p.drawText(QtCore.QRectF(margin, 2, 90, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, units)
        font.setPixelSize(13)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QtGui.QColor(style.TEXT))
        p.drawText(QtCore.QRectF(self.width() - margin - 80, 2, 80, 22),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, db_text)
        p.end()


class MeterPanel(QtWidgets.QFrame):
    """HF conditions chip and clock over the S-meter."""

    WIDTH = 172

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("meter")
        self.setFixedWidth(self.WIDTH)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 0)
        layout.setSpacing(0)
        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(6, 0, 8, 0)
        top.setSpacing(4)
        self.chip = Chip("")
        self.chip.setToolTip("HF conditions panel")
        self.clock = QtWidgets.QLabel("")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.chip, 0)
        top.addWidget(self.clock, 1)
        layout.addLayout(top)
        self.smeter = SMeter(state)
        layout.addWidget(self.smeter, 1)

    def retheme(self) -> None:
        self.smeter.update()
