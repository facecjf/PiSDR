"""Sliders area under the spectrum (AF volume, RF gain, squelch) and the pop-up slider.

Drag a slider to set its value. Tap its name to give it the knob (tap again to give the
knob back to tuning); the name is highlighted while the knob drives it. When the sliders
are hidden, turning the knob on one of them shows :class:`PopupSlider` in the middle of
the screen for a moment instead, as piHPSDR does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .. import config
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from . import style

HEIGHT = 40
_LABEL_W = 46
_VALUE_W = 58


@dataclass(frozen=True)
class Level:
    """One adjustable level: how to show it and how to set it from a 0..1 position."""
    name: str                              # short label: AF, RF, SQL
    title: str                             # for the pop-up
    knob: str                              # the knob function that drives it
    fraction: Callable[[], float]
    text: Callable[[], str]
    set_fraction: Callable[[float], None]
    fields: frozenset


def levels(s: RadioState) -> dict[str, Level]:
    """The levels by knob function name."""
    gain_span = config.GAIN_MAX_DB - config.GAIN_MIN_DB
    squelch_span = config.SQUELCH_MAX_DB - config.SQUELCH_MIN_DB
    return {
        "VOLUME": Level(
            "AF", "AF volume", "VOLUME",
            lambda: s.volume / 100.0,
            lambda: f"{s.volume}",
            lambda f: s.set_volume(round(f * 100)),
            frozenset({"volume"})),
        "GAIN": Level(
            "RF", "RF gain", "GAIN",
            lambda: (s.gain_db - config.GAIN_MIN_DB) / gain_span,
            lambda: "AGC" if s.agc else f"{s.gain_db:.0f} dB",
            lambda f: s.set_gain(round((config.GAIN_MIN_DB + f * gain_span) / config.GAIN_STEP_DB)
                                 * config.GAIN_STEP_DB),
            frozenset({"gain_db", "agc"})),
        "SQUELCH": Level(
            "SQL", "Squelch", "SQUELCH",
            lambda: (s.squelch_db - config.SQUELCH_MIN_DB) / squelch_span,
            lambda: "open" if s.squelch_db <= config.SQUELCH_MIN_DB else f"{s.squelch_db:.0f} dB",
            lambda f: s.set_squelch(round(config.SQUELCH_MIN_DB + f * squelch_span)),
            frozenset({"squelch_db"})),
    }


def _font(widget: QtWidgets.QWidget, px: int, bold: bool = False) -> QtGui.QFont:
    font = QtGui.QFont(widget.font())
    font.setPixelSize(px)
    font.setBold(bold)
    return font


def _paint_track(p: QtGui.QPainter, track: QtCore.QRectF, fraction: float, dimmed: bool = False) -> None:
    fraction = max(0.0, min(1.0, fraction))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(style.METER_BG))
    p.drawRoundedRect(track, 4, 4)
    fill = QtGui.QColor(style.TEXT_DIM if dimmed else style.ACCENT_2)
    p.setBrush(fill)
    p.drawRoundedRect(QtCore.QRectF(track.left(), track.top(), track.width() * fraction, track.height()), 4, 4)
    x = track.left() + track.width() * fraction
    p.setBrush(QtGui.QColor(style.TEXT))
    p.drawRoundedRect(QtCore.QRectF(x - 4, track.top() - 6, 8, track.height() + 12), 3, 3)


class LevelSlider(QtWidgets.QWidget):
    """Name, track and value of one level. The track is dragged; the name hands over the knob."""

    assign = Signal(str)      # knob function name: the label was tapped

    def __init__(self, state: RadioState, level: Level, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.level = level
        self.setFixedHeight(HEIGHT)
        self.setMinimumWidth(_LABEL_W + _VALUE_W + 50)
        self._dragging = False
        state.changed.connect(self._on_changed)

    def _on_changed(self, field: str) -> None:
        if field == "knob_function" or field in self.level.fields:
            self.update()

    def track_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(_LABEL_W, HEIGHT / 2 - 5, max(10, self.width() - _LABEL_W - _VALUE_W), 10)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(style.PANEL))
        on_knob = self.state.knob_function == self.level.knob
        label = QtCore.QRectF(4, 5, _LABEL_W - 10, HEIGHT - 10)
        if on_knob:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(style.ACCENT))
            p.drawRoundedRect(label, 4, 4)
        p.setFont(_font(self, 14, bold=True))
        p.setPen(QtGui.QColor(style.SELECT_FG if on_knob else style.TEXT_DIM))
        p.drawText(label, Qt.AlignmentFlag.AlignCenter, self.level.name)
        track = self.track_rect()
        _paint_track(p, track, self.level.fraction(), dimmed=self.level.knob == "GAIN" and self.state.agc)
        p.setFont(_font(self, 14))
        p.setPen(QtGui.QColor(style.TEXT))
        p.drawText(QtCore.QRectF(track.right() + 8, 0, _VALUE_W - 12, HEIGHT),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.level.text())
        p.end()

    # ------------------------------------------------------------------ touch
    @staticmethod
    def _x(event) -> float:
        return event.position().x() if hasattr(event, "position") else float(event.pos().x())

    def _set_from(self, x: float) -> None:
        track = self.track_rect()
        self.level.set_fraction(max(0.0, min(1.0, (x - track.left()) / track.width())))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        x = self._x(event)
        if x < _LABEL_W - 4:
            self.assign.emit(self.level.knob)
            return
        self._dragging = True
        self._set_from(x)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._set_from(self._x(event))

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        self._dragging = False


class SliderBar(QtWidgets.QWidget):
    """The three sliders side by side."""

    assign = Signal(str)

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(HEIGHT)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.sliders: dict[str, LevelSlider] = {}
        for knob, level in levels(state).items():
            slider = LevelSlider(state, level)
            slider.assign.connect(self.assign)
            layout.addWidget(slider, 1)
            self.sliders[knob] = slider

    def retheme(self) -> None:
        for slider in self.sliders.values():
            slider.update()


class PopupSlider(QtWidgets.QWidget):
    """A level shown over the screen for a moment while the knob changes it."""

    SHOW_MS = 1500

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.levels = levels(state)
        self.level: Level | None = None
        self.setFixedSize(340, 64)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.SHOW_MS)
        self._timer.timeout.connect(self.hide)

    def show_level(self, knob: str) -> None:
        self.level = self.levels.get(knob)
        if self.level is None:
            return
        self.update()
        self.show()
        self.raise_()
        self._timer.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self.level is None:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT), 2))
        p.setBrush(QtGui.QColor(style.PANEL_HI))
        p.drawRoundedRect(QtCore.QRectF(1, 1, self.width() - 2, self.height() - 2), 8, 8)
        p.setFont(_font(self, 16, bold=True))
        p.setPen(QtGui.QColor(style.TEXT))
        p.drawText(QtCore.QRectF(16, 6, 180, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.level.title)
        p.setPen(QtGui.QColor(style.ACCENT))
        p.drawText(QtCore.QRectF(self.width() - 136, 6, 120, 24),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.level.text())
        _paint_track(p, QtCore.QRectF(18, 40, self.width() - 36, 10), self.level.fraction())
        p.end()
