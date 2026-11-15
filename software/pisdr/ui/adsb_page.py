"""ADS-B map: a 360-degree radar of the aircraft the decoder is tracking, with flight data and a list.

Shown while a dump1090-family decoder owns the dongle (see ``radio/adsb_decoder.py``); BACK
gives the dongle back to the receiver. North is up, the receiver is in the middle and the
rings are distances from it in nautical miles. The map fills the left of the screen with
its buttons laid over it, the flight data is on the right; the colours are the theme's.

The soft-key bar is hidden on this page (the buttons on the map do the same), but the four
keys under the screen still work:

    key 1 / key 2     ZOOM - / ZOOM + (config.ADSB_RANGES_NM); hold: ground traffic / trails on or off
    key 3             LIST, the flights as a table; hold: labels FULL / NAME / OFF
    key 4             BACK
    knob / tap        select an aircraft; push the knob to go back to AUTO

With nothing selected the page is in AUTO: it shows each aircraft in turn for a few seconds.
"""
from __future__ import annotations

import math
import time
from collections import deque

import numpy as np

from .. import adsb, airlines, config, outlines, zipcodes
from ..adsb import Aircraft
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from . import style
from .pickers import Page

UP, DOWN = "\u2191", "\u2193"
LEVEL_RATE_FPM = 250          # slower climbs / descents than this are drawn as level flight
TAP_RADIUS_PX = 36            # a tap this close to an aircraft selects it
TRAIL_BREAK_DEG = 0.5         # a jump this big between two positions starts a new trail
EDGE_PX = 16                  # between the ring of the selected range and the top / bottom of the map
SORTS = ("DIST", "ALT", "AIRLINE")


# --------------------------------------------------------------------------- font and colours
def _font(widget: QtWidgets.QWidget, pixels: int, bold: bool = False) -> QtGui.QFont:
    """The program's typeface (the widget's own family) *pixels* high."""
    font = QtGui.QFont(widget.font())
    font.setPixelSize(pixels)
    font.setBold(bold)
    return font


def _colour(name: str, alpha: int = 255) -> QtGui.QColor:
    colour = QtGui.QColor(name)
    colour.setAlpha(alpha)
    return colour


def altitude_colour(ac: Aircraft) -> str:
    """The theme's colour for low, middle or high; dim on the ground or with no altitude yet."""
    if ac.on_ground or ac.altitude_ft is None:
        return style.TEXT_DIM
    low, high = config.ADSB_ALT_BANDS_FT
    return style.ALTITUDE[0 if ac.altitude_ft < low else 1 if ac.altitude_ft < high else 2]


def aircraft_colour(ac: Aircraft, shown: bool) -> str:
    """Colour of an aircraft on the radar."""
    if shown:
        return style.TEXT
    if ac.emergency:
        return style.REC
    if ac.has_position and ac.seen_pos_s > config.ADSB_STALE_S:
        return style.TEXT_DIM
    return altitude_colour(ac)


def _button(parent: QtWidgets.QWidget, text: str, width: int, height: int, slot, big: bool = False) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text, parent)
    button.setObjectName("radarbtn")
    button.setProperty("big", big)
    button.setFixedSize(width, height)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.clicked.connect(slot)
    return button


def _set_flag(button: QtWidgets.QPushButton, name: str, on: bool) -> None:
    if button.property(name) != on:
        button.setProperty(name, on)
        button.style().unpolish(button)
        button.style().polish(button)


# --------------------------------------------------------------------------- text
def trend(ac: Aircraft) -> str:
    """Arrow for an aircraft that is climbing or descending, else nothing."""
    if ac.vert_rate_fpm is None or abs(ac.vert_rate_fpm) < LEVEL_RATE_FPM:
        return ""
    return UP if ac.vert_rate_fpm > 0 else DOWN


def level_text(ac: Aircraft) -> str:
    """Altitude the way a radar data block shows it: hundreds of feet ("350" = 35,000 ft)."""
    if ac.on_ground:
        return "GND"
    if ac.altitude_ft is None:
        return ""
    return f"{max(0, round(ac.altitude_ft / 100)):03d}{trend(ac)}"


def tag_lines(ac: Aircraft, labels: str, selected: bool) -> list[str]:
    """Lines of text drawn beside an aircraft for a LABELS setting."""
    if selected or labels == "FULL":
        return [line for line in (ac.name, level_text(ac)) if line]
    return [ac.name] if labels == "NAME" else []


def flight_fields(ac: Aircraft | None) -> dict[str, str]:
    """What the flight data panel says about an aircraft (or about no aircraft)."""
    if ac is None:
        return {"callsign": "------", "type": "", "airline": "--", "altitude": "--", "climb": "--",
                "speed": "--", "track": "--", "distance": "--", "squawk": "--", "signal": ""}
    if ac.on_ground:
        altitude = "GROUND"
    else:
        altitude = "--" if ac.altitude_ft is None else f"{ac.altitude_ft:,} ft"
    if ac.vert_rate_fpm is None:
        climb = "--"
    else:
        climb = f"{trend(ac)}{abs(ac.vert_rate_fpm):,} fpm" if trend(ac) else "level"
    if not ac.has_position:
        distance = "no position"
    elif ac.distance_nm is None:
        distance = (f"{abs(ac.lat):.2f}{'N' if ac.lat >= 0 else 'S'} "
                    f"{abs(ac.lon):.2f}{'E' if ac.lon >= 0 else 'W'}")
    else:
        distance = f"{ac.distance_nm:.1f} nm {adsb.compass_point(ac.bearing_deg)}"
    kind = adsb.CATEGORIES.get(ac.category, "")
    return {
        "callsign": ac.name,
        "type": " ".join(part for part in (ac.icao if ac.callsign else "NO CALLSIGN YET", kind) if part),
        "airline": airlines.operator(ac.callsign) or "--",
        "altitude": altitude,
        "climb": climb,
        "speed": "--" if ac.speed_kt is None else f"{ac.speed_kt:.0f} kts",
        "track": "--" if ac.track_deg is None else f"{ac.track_deg:03.0f} deg",
        "distance": distance,
        "squawk": ac.squawk or "--",
        "signal": "" if ac.rssi_db is None else f"SIG {ac.rssi_db:.0f} dB",
    }


def sorted_flights(order: list[Aircraft], sort: str) -> list[Aircraft]:
    """The list in one of SORTS: nearest first, lowest first, or by operator then callsign."""
    if sort == "ALT":
        return sorted(order, key=lambda ac: (not ac.on_ground and ac.altitude_ft is None,
                                             -1 if ac.on_ground else ac.altitude_ft or 0, ac.name))
    if sort == "AIRLINE":
        return sorted(order, key=lambda ac: (airlines.operator(ac.callsign).upper() or "\uffff", ac.name))
    return list(order)


# --------------------------------------------------------------------------- aircraft shapes
def _outline(half: list[tuple[float, float]]) -> QtGui.QPolygonF:
    """A shape that is the same either side of its centre line, from its right-hand edge (nose first)."""
    points = half + [(-x, y) for x, y in reversed(half) if x]
    return QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])


# Seen from above, nose up, in pixels.
_JET = _outline([(0, -12), (1.7, -9.5), (2.1, -4), (11.5, 3.5), (11.5, 6), (2.1, 2.5),
                 (1.7, 8), (5.2, 11), (5.2, 12.5), (0, 11.2)])
_LIGHT = _outline([(0, -9), (1.5, -7.5), (1.7, -4.5), (10.5, -4), (10.5, -0.8), (1.7, -0.3),
                   (1.2, 6), (4.2, 6.8), (4.2, 9), (0, 8.6)])
_SIZES = {"heavy": 1.15, "jet": 1.0, "small": 0.82, "light": 0.95}


def paint_aircraft_shape(p: QtGui.QPainter, ac: Aircraft, x: float, y: float, colour: QtGui.QColor) -> None:
    """One aircraft at (x, y): an airliner, a light aircraft or a helicopter pointing along its track."""
    p.setPen(QtGui.QPen(QtGui.QColor(style.BG), 1))
    p.setBrush(colour)
    kind = ac.kind
    if ac.on_ground:
        p.drawRect(QtCore.QRectF(x - 4, y - 4, 8, 8))
    elif ac.track_deg is None or kind == "other":
        p.drawEllipse(QtCore.QPointF(x, y), 5.0, 5.0)
    else:
        p.save()
        p.translate(x, y)
        p.rotate(ac.track_deg)                     # the shapes point up = north = track 0
        if kind == "heli":
            p.drawRect(QtCore.QRectF(-0.9, 2.5, 1.8, 8.5))
            p.drawRect(QtCore.QRectF(-3.2, 10, 6.4, 1.8))
            p.drawEllipse(QtCore.QPointF(0, -2), 3.4, 5.6)
            p.setPen(QtGui.QPen(colour, 1.8))
            p.drawLine(QtCore.QPointF(-8, -10), QtCore.QPointF(8, 6))
            p.drawLine(QtCore.QPointF(8, -10), QtCore.QPointF(-8, 6))
        else:
            p.scale(_SIZES[kind], _SIZES[kind])
            p.drawPolygon(_LIGHT if kind == "light" else _JET)
        p.restore()


# --------------------------------------------------------------------------- the radar
class RadarView(QtWidgets.QWidget):
    """North-up radar around :attr:`centre`. The page feeds it; it draws and reports taps."""

    picked = Signal(str)          # ICAO address of the tapped aircraft, "" for empty sky
    back_requested = Signal()

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.aircraft: list[Aircraft] = []
        self.centre: tuple[float, float] | None = None
        self.centre_source = ""                       # "settings", "ip", "traffic" or ""
        self.selected = ""                            # the aircraft whose data is being shown
        self.trails: dict[str, deque] = {}            # ICAO -> (lat, lon) positions, oldest first
        self.place = ""                               # where the centre is, in words
        self.rate = ""                                # message rate, in words
        self.total = 0
        self._hits: list[tuple[str, float, float]] = []
        self._static: QtGui.QPixmap | None = None
        self._static_key: tuple | None = None
        self._sweep_deg = 0.0
        self._sweep = QtCore.QTimer(self)
        self._sweep.setInterval(80)
        self._sweep.timeout.connect(self._advance_sweep)
        self.setMinimumSize(300, 240)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        self.zoom_in = _button(self, "+", 36, 32, lambda: state.step_adsb_range(-1), big=True)
        self.zoom_out = _button(self, "\u2212", 36, 32, lambda: state.step_adsb_range(+1), big=True)
        self.labels_btn = _button(self, "", 76, 36, lambda: state.cycle_adsb_labels())
        self.trails_btn = _button(self, "", 76, 36, state.toggle_adsb_trails)
        self.ground_btn = _button(self, "", 76, 36, state.toggle_adsb_ground)
        self.back_btn = _button(self, "BACK", 76, 36, self.back_requested, big=True)
        self.sync_buttons()

    def sync_buttons(self) -> None:
        s = self.state
        self.labels_btn.setText(f"LABELS\n{s.adsb_labels}")
        self.trails_btn.setText(f"TRAILS\n{'ON' if s.adsb_trails else 'OFF'}")
        self.ground_btn.setText(f"GROUND\n{'ON' if s.adsb_ground else 'OFF'}")
        _set_flag(self.labels_btn, "off", s.adsb_labels == "OFF")
        _set_flag(self.trails_btn, "off", not s.adsb_trails)
        _set_flag(self.ground_btn, "off", not s.adsb_ground)

    def resizeEvent(self, event) -> None:  # noqa: N802
        right = self.width() - 82
        self.zoom_in.move(6, 38)
        self.zoom_out.move(46, 38)
        self.labels_btn.move(right, 54)
        self.trails_btn.move(right, 94)
        self.ground_btn.move(right, self.height() - 42)
        self.back_btn.move(6, self.height() - 42)
        super().resizeEvent(event)

    # ------------------------------------------------------------------ sweep
    def showEvent(self, event) -> None:  # noqa: N802
        self._sweep.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._sweep.stop()
        super().hideEvent(event)

    def _advance_sweep(self) -> None:
        self._sweep_deg = (self._sweep_deg + 360.0 * self._sweep.interval() / 1000.0 / config.ADSB_SWEEP_S) % 360.0
        self.update()

    # ------------------------------------------------------------------ geometry
    def radius(self) -> float:
        """Of the ring at the selected range, in pixels: it reaches the top and bottom of the map."""
        return max(1.0, min(self.width(), self.height()) / 2.0 - EDGE_PX)

    def scale(self) -> float:
        """Pixels per nautical mile."""
        return self.radius() / self.state.adsb_range_nm

    def to_screen(self, east_nm: float, north_nm: float) -> tuple[float, float]:
        scale = self.scale()
        return self.width() / 2.0 + east_nm * scale, self.height() / 2.0 - north_nm * scale

    def reach_nm(self) -> float:
        """Distance from the centre to a corner of the map."""
        return math.hypot(self.width(), self.height()) / 2.0 / self.scale()

    def rings_nm(self) -> list[int]:
        """Distances of the rings, out to the corners of the map."""
        spacing = dict(config.ADSB_RANGES_NM).get(self.state.adsb_range_nm, self.state.adsb_range_nm)
        return list(range(spacing, int(self.reach_nm()) + 1, spacing))

    def aircraft_at(self, x: float, y: float) -> str:
        """ICAO address of the drawn aircraft nearest a point, "" if none is close."""
        best, best_d = "", float(TAP_RADIUS_PX)
        for icao, ax, ay in self._hits:
            d = math.hypot(ax - x, ay - y)
            if d <= best_d:
                best, best_d = icao, d
        return best

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position() if hasattr(event, "position") else event.pos()     # Qt 6 / Qt 5
        self.picked.emit(self.aircraft_at(pos.x(), pos.y()))

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.drawPixmap(0, 0, self._static_layer())
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        self._paint_sweep(p)
        self._paint_corners(p)                         # under the aircraft: those are what the map is for
        self._hits = []
        if self.centre is not None:
            if self.state.adsb_trails:
                self._paint_trails(p)
            self._paint_aircraft(p)
        self._paint_overlay(p)
        p.end()

    def _static_layer(self) -> QtGui.QPixmap:
        """Everything that only changes with the view: outlines, rings and compass letters."""
        ratio = self.devicePixelRatioF()
        key = (self.centre, self.centre_source == "traffic", self.state.adsb_range_nm,
               self.width(), self.height(), ratio, style.CURRENT_THEME)
        if key != self._static_key or self._static is None:
            self._static_key = key
            self._static = QtGui.QPixmap(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))
            self._static.setDevicePixelRatio(ratio)
            self._static.fill(QtGui.QColor(style.BG))
            p = QtGui.QPainter(self._static)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
            if self.centre is not None:
                self._paint_outlines(p)
            self._paint_scale(p)
            p.end()
        return self._static

    def _paint_outlines(self, p: QtGui.QPainter) -> None:
        """Coasts and borders, dim, under everything else."""
        pens = {
            outlines.COAST: QtGui.QPen(_colour(style.TEXT_DIM, 210), 1.2),
            outlines.LAKE: QtGui.QPen(_colour(style.TEXT_DIM, 210), 1.2),
            outlines.BORDER: QtGui.QPen(_colour(style.TEXT_DIM, 160), 1.2, Qt.PenStyle.DashLine),
            outlines.STATE: QtGui.QPen(_colour(style.TEXT_DIM, 110), 1.0, Qt.PenStyle.DotLine),
        }
        paths: dict[int, QtGui.QPainterPath] = {}
        for kind, east, north in outlines.near(self.centre[0], self.centre[1], self.reach_nm() * 1.02):
            paths.setdefault(kind, QtGui.QPainterPath()).addPolygon(
                QtGui.QPolygonF([QtCore.QPointF(float(e), -float(n)) for e, n in zip(east, north)]))
        p.save()
        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.scale(self.scale(), self.scale())
        p.setBrush(Qt.BrushStyle.NoBrush)
        for kind, path in paths.items():
            pen = pens[kind]
            pen.setCosmetic(True)                  # width in pixels whatever the zoom
            p.setPen(pen)
            p.drawPath(path)
        p.restore()

    def _paint_scale(self, p: QtGui.QPainter) -> None:
        width, height = self.width(), self.height()
        cx, cy, scale = width / 2.0, height / 2.0, self.scale()
        centre = QtCore.QPointF(cx, cy)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(_colour(style.ACCENT, 150), 1.1))
        p.drawLine(QtCore.QPointF(cx, 0), QtCore.QPointF(cx, height))
        p.drawLine(QtCore.QPointF(0, cy), QtCore.QPointF(width, cy))
        rings = self.rings_nm()
        for nm in rings:                               # the ring of the selected range is the heavy one
            chosen = nm == self.state.adsb_range_nm
            p.setPen(QtGui.QPen(_colour(style.ACCENT, 255 if chosen else 150), 2.2 if chosen else 1.1))
            p.drawEllipse(centre, nm * scale, nm * scale)
        p.setFont(_font(self, 13, True))
        p.setPen(QtGui.QColor(style.TEXT))
        for letter, box in (("N", QtCore.QRectF(cx - 22, 0, 18, EDGE_PX)),
                            ("S", QtCore.QRectF(cx - 22, height - EDGE_PX, 18, EDGE_PX)),
                            ("W", QtCore.QRectF(2, cy - 19, 18, 16)),
                            ("E", QtCore.QRectF(width - 20, cy - 19, 18, 16))):
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, letter)

        # Distances along the west-east line, just inside each ring.
        p.setFont(_font(self, 10))
        metrics = p.fontMetrics()
        for nm in rings:
            if nm * scale > cx - 24:                   # (that would be under the W and the E)
                break
            text = str(nm)
            room = metrics.horizontalAdvance(text) + 2
            for side in (-1, 1):
                edge = cx + side * (nm * scale - 3)
                box = QtCore.QRectF(edge - room if side > 0 else edge, cy - 15, room, 13)
                p.fillRect(box, QtGui.QColor(style.BG))
                p.setPen(QtGui.QColor(style.TEXT_DIM))
                p.drawText(box.adjusted(0, -2, 0, 2), Qt.AlignmentFlag.AlignCenter, text)
        if self.centre is not None and self.centre_source != "traffic":
            p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT), 1.2))          # the receiver
            p.setBrush(QtGui.QColor(style.TEXT))
            p.drawEllipse(centre, 3.0, 3.0)

    def _paint_sweep(self, p: QtGui.QPainter) -> None:
        if not self._sweep.isActive():
            return
        cx, cy = self.width() / 2.0, self.height() / 2.0
        radius = math.hypot(cx, cy)                    # out to the corners
        lines = 14
        for index in range(lines):
            angle = math.radians(self._sweep_deg - index * 1.5)
            p.setPen(QtGui.QPen(_colour(style.ACCENT, int(190 * (1 - index / lines))), 1.2))
            p.drawLine(QtCore.QPointF(cx, cy),
                       QtCore.QPointF(cx + radius * math.sin(angle), cy - radius * math.cos(angle)))

    def _paint_trails(self, p: QtGui.QPainter) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        shown = {ac.icao: ac for ac in self.aircraft}
        for icao, trail in self.trails.items():
            if len(trail) < 2 or icao not in shown:
                continue
            points = np.asarray(trail, dtype=float)
            east, north = adsb.project(self.centre[0], self.centre[1], points[:, 0], points[:, 1])
            line = QtGui.QPolygonF([QtCore.QPointF(*self.to_screen(float(e), float(n)))
                                    for e, n in zip(east, north)])
            chosen = icao == self.selected
            p.setPen(QtGui.QPen(_colour(style.ACCENT_2 if chosen else altitude_colour(shown[icao]), 230 if chosen else 120),
                                1.8 if chosen else 1.2))
            p.drawPolyline(line)

    def _reserved(self) -> list[QtCore.QRectF]:
        """Where the buttons and the corner readouts are: aircraft labels keep out of these."""
        width = self.width()
        buttons = (self.zoom_in, self.zoom_out, self.labels_btn, self.trails_btn, self.ground_btn, self.back_btn)
        return [QtCore.QRectF(0, 0, 122, 114), QtCore.QRectF(width - 122, 0, 122, 52),
                QtCore.QRectF(width - 86, 134, 86, 58), *(QtCore.QRectF(button.geometry()) for button in buttons)]

    def _paint_aircraft(self, p: QtGui.QPainter) -> None:
        drawn: list[tuple[Aircraft, float, float]] = []
        for ac in self.aircraft:
            if not ac.has_position:
                continue
            east, north = adsb.project(self.centre[0], self.centre[1], ac.lat, ac.lon)
            x, y = self.to_screen(float(east), float(north))
            if -12 <= x <= self.width() + 12 and -12 <= y <= self.height() + 12:
                drawn.append((ac, x, y))
        drawn.sort(key=lambda item: item[0].icao == self.selected)    # the one being shown goes on top
        self._hits = [(ac.icao, x, y) for ac, x, y in drawn]
        taken = [QtCore.QRectF(x - 11, y - 11, 22, 22) for _ac, x, y in drawn] + self._reserved()
        p.setFont(_font(self, 11))
        for ac, x, y in drawn:
            chosen = ac.icao == self.selected
            if chosen:                                 # a lit disc: it stands out in one-colour themes too
                p.setBrush(_colour(style.ACCENT_2, 70))
                p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT_2), 2.0, Qt.PenStyle.DashLine))
                p.drawEllipse(QtCore.QPointF(x, y), 19.0, 19.0)
            paint_aircraft_shape(p, ac, x, y, QtGui.QColor(aircraft_colour(ac, chosen)))
            lines = tag_lines(ac, self.state.adsb_labels, chosen)
            if lines:
                stale = ac.seen_pos_s > config.ADSB_STALE_S or ac.on_ground
                p.setPen(QtGui.QColor(style.TEXT_DIM if stale and not chosen else style.TEXT))
                self._paint_tag(p, lines, x, y, taken)

    def _paint_tag(self, p: QtGui.QPainter, lines: list[str], x: float, y: float,
                   taken: list[QtCore.QRectF]) -> None:
        """Draw an aircraft's label in the first spot around it that is free and on the plot."""
        metrics = p.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 2.0
        height = 13.0 * len(lines)
        spots = [QtCore.QRectF(x + 14, y - 12, width, height),              # right, left, below, above,
                 QtCore.QRectF(x - 14 - width, y - 12, width, height),      # then the four corners
                 QtCore.QRectF(x - width / 2, y + 13, width, height),
                 QtCore.QRectF(x - width / 2, y - 13 - height, width, height),
                 QtCore.QRectF(x + 12, y + 8, width, height),
                 QtCore.QRectF(x + 12, y - 8 - height, width, height),
                 QtCore.QRectF(x - 12 - width, y + 8, width, height),
                 QtCore.QRectF(x - 12 - width, y - 8 - height, width, height)]
        on_plot = [spot for spot in spots if spot.left() >= 2 and spot.right() <= self.width() - 2]
        free = [spot for spot in on_plot if not any(spot.intersects(other) for other in taken)]
        spot = (free or on_plot or spots)[0]
        taken.append(spot)
        p.fillRect(spot.adjusted(-1, 0, 1, 1), _colour(style.BG, 205))    # readable over rings and coasts
        for row, line in enumerate(lines):
            p.drawText(QtCore.QPointF(spot.left(), spot.top() + 11 + row * 13), line)

    def notes(self) -> list[str]:
        """Why the centre may be off, along the bottom of the map."""
        return {"ip": ["CENTRE FROM IP ADDRESS", "SET A ZIP IN SETTINGS"],
                "traffic": ["CENTRED ON TRAFFIC", "SET A ZIP IN SETTINGS"]}.get(self.centre_source, [])

    def _paint_corners(self, p: QtGui.QPainter) -> None:
        """The readouts in the corners, each on a patch of background so the map does not show through."""
        width, height = self.width(), self.height()
        left = QtGui.QTextOption(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        right = QtGui.QTextOption(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        back = _colour(style.BG, 190)

        def label(box: QtCore.QRectF, text: str, side: QtGui.QTextOption = left) -> None:
            if text:
                p.fillRect(p.boundingRect(box, text, side).adjusted(-3, 0, 3, 0), back)
                p.drawText(box, text, side)

        p.setPen(QtGui.QColor(style.ACCENT))
        p.setFont(_font(self, 22, True))
        label(QtCore.QRectF(6, 2, 110, 32), f"{self.state.adsb_range_nm}nm")
        label(QtCore.QRectF(width - 116, 2, 110, 32), "ADS-B", right)
        p.setFont(_font(self, 16, True))
        label(QtCore.QRectF(6, 74, 90, 22), f"T.{self.total}")
        p.setFont(_font(self, 10))
        p.setPen(QtGui.QColor(style.ACCENT_2))
        label(QtCore.QRectF(6, 97, 110, 14), self.rate)
        p.setPen(QtGui.QColor(style.TEXT))
        label(QtCore.QRectF(width - 176, 34, 170, 16), self.place[:24], right)
        # What the three colours of the aircraft mean.
        low, high = (round(feet / 100) for feet in config.ADSB_ALT_BANDS_FT)
        top = 140
        p.fillRect(QtCore.QRectF(width - 84, top - 4, 80, 54), back)
        for row, text in enumerate((f"FL{high}+", f"FL{low}+", "LOW")):
            colour = QtGui.QColor(style.ALTITUDE[2 - row])
            p.fillRect(QtCore.QRectF(width - 80, top + row * 16, 11, 11), colour)
            p.setPen(colour)
            p.drawText(QtCore.QRectF(width - 64, top + row * 16 - 3, 60, 16), text, left)
        p.setPen(QtGui.QColor(style.ACCENT))
        notes = self.notes()
        for row, line in enumerate(notes):                     # beside the BACK button
            label(QtCore.QRectF(90, height - 4 - 14 * (len(notes) - row), 150, 14), line)

    def _paint_overlay(self, p: QtGui.QPainter) -> None:
        message, colour = self._overlay()
        if not message:
            return
        p.setFont(_font(self, 15))
        inset = self.width() / 2.0 - self.radius() * 0.9
        box = QtCore.QRectF(inset, self.height() * 0.58, self.width() - 2 * inset, self.height() * 0.3)
        wrapped = QtGui.QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        wrapped.setWrapMode(QtGui.QTextOption.WrapMode.WordWrap)
        p.setPen(QtGui.QPen(colour, 1.5))
        p.setBrush(_colour(style.BG, 235))
        p.drawRoundedRect(p.boundingRect(box, message, wrapped).adjusted(-10, -6, 10, 6), 6, 6)
        p.drawText(box, message, wrapped)

    def _overlay(self) -> tuple[str, QtGui.QColor]:
        """Text across the plot when there is nothing to draw, and its colour."""
        s = self.state
        if s.adsb_error:
            return f"{s.adsb_error}\nPress BACK to get the radio back.", QtGui.QColor(style.REC)
        if not s.aircraft:
            return "Listening on 1090 MHz...", QtGui.QColor(style.ACCENT)
        if not any(ac.has_position for ac in s.aircraft):
            heard = len(s.aircraft)
            return (f"{heard} aircraft heard - waiting for {'its' if heard == 1 else 'a'} position",
                    QtGui.QColor(style.ACCENT))
        return "", QtGui.QColor(style.ACCENT)


# --------------------------------------------------------------------------- flight data
class FlightPanel(QtWidgets.QWidget):
    """The aircraft being shown, spelled out: big values under small captions."""

    list_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.fields = flight_fields(None)
        self.auto = True
        self.setFixedWidth(config.ADSB_PANEL_W)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.list_btn = _button(self, "LIST \u25b6", config.ADSB_PANEL_W - 22, 38, self.list_requested, big=True)

    def show_flight(self, ac: Aircraft | None, auto: bool) -> None:
        self.fields, self.auto = flight_fields(ac), auto
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.list_btn.move(12, self.height() - 42)
        super().resizeEvent(event)

    def _fitted(self, p: QtGui.QPainter, text: str, pixels: int, room: float, bold: bool = False) -> None:
        """Select the largest font up to *pixels* in which *text* is no wider than *room*."""
        while True:
            p.setFont(_font(self, pixels, bold))
            if pixels <= 9 or p.fontMetrics().horizontalAdvance(text) <= room:
                return
            pixels -= 1

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        width, height, fields = self.width(), self.height(), self.fields
        left, room = 12, width - 18
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        p.fillRect(self.rect(), QtGui.QColor(style.BG))
        p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT), 2))
        p.drawLine(1, 0, 1, height)
        p.drawLine(left - 4, 33, width - 4, 33)

        def text(x: float, baseline: float, value: str, pixels: int, colour: str, space: float = room,
                 bold: bool = False) -> None:
            self._fitted(p, value, pixels, space, bold)
            p.setPen(QtGui.QColor(colour))
            p.drawText(QtCore.QPointF(x, baseline), value)

        text(left, 24, "FLIGHT DATA", 18, style.ACCENT, bold=True)
        p.setFont(_font(self, 12, True))
        p.setPen(QtGui.QColor(style.OK if self.auto else style.ACCENT_2))
        p.drawText(QtCore.QRectF(width - 90, 6, 84, 20), right, "AUTO" if self.auto else "PAUSED")

        text(left, 48, "CALLSIGN", 10, style.TEXT_DIM)
        self._fitted(p, fields["callsign"], 28, room - 20, True)
        plate = QtCore.QRectF(left - 2, 53, p.fontMetrics().horizontalAdvance(fields["callsign"]) + 20, 42)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(style.TEXT))           # the theme's text and background, swapped
        p.drawRoundedRect(plate, 7, 7)
        p.setPen(QtGui.QColor(style.BG))
        p.drawText(plate, Qt.AlignmentFlag.AlignCenter, fields["callsign"])
        text(left, 110, fields["type"], 11, style.OK, room - 84)
        p.setFont(_font(self, 10))
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        p.drawText(QtCore.QRectF(width - 100, 98, 94, 16), right, fields["signal"])
        text(left, 130, "AIRLINE", 10, style.TEXT_DIM)
        text(left, 154, fields["airline"], 19, style.TEXT, bold=True)

        # Three rows of two values each, spread over what is left above the LIST button.
        top, pitch = 164.0, max(44.0, (height - 164 - 46) / 3.0)
        half = left + room * 0.53
        value, near = style.ACCENT_2, style.ACCENT
        rows = ((("ALTITUDE", fields["altitude"], value, left), ("CLIMB", fields["climb"], value, half)),
                (("SPEED", fields["speed"], value, left), ("TRACK", fields["track"], value, half)),
                (("RADAR DISTANCE", fields["distance"], near, left), ("SQUAWK", fields["squawk"], value, left + room * 0.68)))
        for row, pair in enumerate(rows):
            y = top + row * pitch
            (_c0, _v0, _k0, x0), (_c1, _v1, _k1, x1) = pair
            for (caption, shown, colour, x), space in zip(pair, (x1 - x0 - 8, left + room - x1)):
                text(x, y + 13, caption, 10, style.TEXT_DIM, space)
                text(x, y + 13 + min(27.0, pitch - 18), shown, 21, colour, space, bold=True)
        p.end()


# --------------------------------------------------------------------------- flight list
class FlightList(QtWidgets.QWidget):
    """The flights as a table, a page at a time: callsign, operator, distance, altitude, speed."""

    picked = Signal(str)          # ICAO address of the tapped line
    sort_requested = Signal(str)
    page_requested = Signal(int)  # +1 / -1
    radar_requested = Signal()

    HEADER_PX = 44

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.flights: list[Aircraft] = []
        self.shown = ""
        self.page = 0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.sort_buttons = {name: _button(self, name, 88, 32, lambda _=False, n=name: self.sort_requested.emit(n))
                             for name in SORTS}
        self.previous = _button(self, "\u25c0", 70, 32, lambda: self.page_requested.emit(-1), big=True)
        self.following = _button(self, "\u25b6", 70, 32, lambda: self.page_requested.emit(+1), big=True)
        self.radar_btn = _button(self, "\u25c0 RADAR", 88, 32, self.radar_requested)
        self.radar_btn.move(8, 5)
        for column, button in enumerate(self.sort_buttons.values()):
            button.move(110 + column * 94, 5)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.previous.move(self.width() - 154, 5)
        self.following.move(self.width() - 78, 5)
        super().resizeEvent(event)

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.flights) // config.ADSB_LIST_ROWS))

    def show_flights(self, flights: list[Aircraft], shown: str, sort: str, page: int) -> None:
        self.flights, self.shown = flights, shown
        self.page = max(0, min(page, self.pages - 1))
        for name, button in self.sort_buttons.items():
            _set_flag(button, "lit", name == sort)
        self.update()

    def row_height(self) -> float:
        return (self.height() - self.HEADER_PX) / config.ADSB_LIST_ROWS

    def visible(self) -> list[Aircraft]:
        first = self.page * config.ADSB_LIST_ROWS
        return self.flights[first:first + config.ADSB_LIST_ROWS]

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position() if hasattr(event, "position") else event.pos()
        row = int((pos.y() - self.HEADER_PX) // self.row_height()) if pos.y() >= self.HEADER_PX else -1
        if 0 <= row < len(self.visible()):
            self.picked.emit(self.visible()[row].icao)

    @staticmethod
    def cells(ac: Aircraft) -> tuple[str, str, str, str, str]:
        """Callsign, operator, distance (nm), altitude (ft) and speed (kt) as the table shows them."""
        distance = "--" if ac.distance_nm is None else f"{ac.distance_nm:.0f}"
        altitude = "GND" if ac.on_ground else "--" if ac.altitude_ft is None else f"{ac.altitude_ft}"
        speed = "--" if ac.speed_kt is None else f"{ac.speed_kt:.0f}"
        return ac.name, (airlines.operator(ac.callsign) or "NO CALLSIGN YET").upper(), distance, altitude, speed

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        width, row_h = self.width(), self.row_height()
        p.fillRect(self.rect(), QtGui.QColor(style.BG))
        middle = Qt.AlignmentFlag.AlignVCenter
        p.setFont(_font(self, 20, True))
        p.setPen(QtGui.QColor(style.ACCENT))
        p.drawText(QtCore.QRectF(402, 4, width - 570, 34), middle | Qt.AlignmentFlag.AlignLeft,
                   f"({len(self.flights)}) {self.page + 1}/{self.pages}")
        p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT), 2))
        p.drawLine(4, self.HEADER_PX - 3, width - 4, self.HEADER_PX - 3)
        if not self.flights:
            p.setFont(_font(self, 18))
            p.setPen(QtGui.QColor(style.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "NO FLIGHTS")
        pixels = min(18, int(row_h * 0.55))
        big, heavy, small = _font(self, pixels), _font(self, pixels, True), _font(self, 11)
        first = self.page * config.ADSB_LIST_ROWS
        for row, ac in enumerate(self.visible()):
            y = self.HEADER_PX + row * row_h
            line = QtCore.QRectF(0, y, width, row_h)
            if ac.icao == self.shown:
                p.fillRect(line.adjusted(4, 1, -4, -1), QtGui.QColor(style.PANEL_HI))
            callsign, operator, distance, altitude, speed = self.cells(ac)

            def cell(x: float, room: float, value: str, colour: str, align=Qt.AlignmentFlag.AlignLeft) -> None:
                p.setPen(QtGui.QColor(colour))
                p.drawText(QtCore.QRectF(x, y, room, row_h), middle | align,
                           p.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, int(room)))

            p.setFont(big)
            cell(4, 34, str(first + row + 1), style.TEXT_DIM, Qt.AlignmentFlag.AlignRight)
            p.setFont(heavy)
            cell(48, 108, callsign, style.ALTITUDE[0] if ac.on_ground or ac.altitude_ft is None else altitude_colour(ac))
            p.setFont(big)
            cell(160, 290, operator, style.TEXT)
            cell(452, 56, distance, style.ACCENT_2, Qt.AlignmentFlag.AlignRight)
            cell(532, 80, altitude, style.ACCENT, Qt.AlignmentFlag.AlignRight)
            cell(660, 60, speed, style.OK, Qt.AlignmentFlag.AlignRight)
            p.setFont(small)
            for x, unit, colour, value in ((511, "nm", style.ACCENT_2, distance), (615, "ft", style.ACCENT, altitude),
                                           (723, "kts", style.OK, speed)):
                if value not in ("--", "GND"):
                    p.setPen(QtGui.QColor(colour))
                    p.drawText(QtCore.QRectF(x, y + 3, 40, row_h - 3), middle | Qt.AlignmentFlag.AlignLeft, unit)
        p.end()


# --------------------------------------------------------------------------- the page
class AdsbPage(Page):
    """Radar with the flight data beside it, or the flight list; one aircraft is always being shown."""

    stop_requested = Signal()

    def __init__(self, state: RadioState, backend, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.backend = backend
        self.selected = ""                             # ICAO address picked by the user; "" = AUTO
        self.auto = ""                                 # ... and the one AUTO is showing meanwhile
        self.order: list[Aircraft] = []                # nearest first; the knob steps through this
        self.view = "radar"                            # or "list"
        self.sort = SORTS[0]
        self.list_page = 0
        self.message_rate: float | None = None
        self._traffic_centre: tuple[float, float] | None = None
        self._counted: tuple[float, int] | None = None   # (time, messages) at the previous update

        self.radar = RadarView(state)
        self.radar.picked.connect(self.select)
        self.radar.back_requested.connect(self.stop_requested)
        self.panel = FlightPanel()
        self.panel.list_requested.connect(lambda: self.show_view("list"))
        self.flight_list = FlightList()
        self.flight_list.picked.connect(self._picked_in_list)
        self.flight_list.sort_requested.connect(self.set_sort)
        self.flight_list.page_requested.connect(self.step_list_page)
        self.flight_list.radar_requested.connect(lambda: self.show_view("radar"))

        radar_view = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(radar_view)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.radar, 1)
        row.addWidget(self.panel, 0)
        self.views = QtWidgets.QStackedLayout(self)
        self.views.setContentsMargins(0, 0, 0, 0)
        self.views.addWidget(radar_view)
        self.views.addWidget(self.flight_list)

        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setInterval(int(config.ADSB_AUTO_S * 1000))
        self._auto_timer.timeout.connect(self.auto_step)
        state.changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------ data
    def on_shown(self) -> None:
        self.selected = self.auto = ""
        self.message_rate = None
        self._counted = None
        self._traffic_centre = None
        self.list_page = 0
        self.radar.trails.clear()
        airlines.reload()
        self.show_view("radar")
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        self._auto_timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._auto_timer.stop()
        super().hideEvent(event)

    def _on_state_changed(self, field: str) -> None:
        if field == "aircraft":
            if self.state.adsb_mode:
                self.refresh()
        elif field == "adsb_ground":
            self.radar.sync_buttons()
            if self.state.adsb_mode:
                self.refresh()
        elif field in ("adsb_range_nm", "adsb_labels", "adsb_trails"):
            self.radar.sync_buttons()
            self.radar.update()

    def refresh(self) -> None:
        """Take in the newest aircraft list: centre, distances, trails, then redraw."""
        aircraft: list[Aircraft] = [ac for ac in self.state.aircraft if self.state.adsb_ground or not ac.on_ground]
        centre, source = self._find_centre(aircraft)
        if centre is not None and source != "traffic":
            adsb.locate(aircraft, centre[0], centre[1])       # only the receiver gives real distances
        self.order = adsb.by_distance(aircraft)
        if self.selected and all(ac.icao != self.selected for ac in aircraft):
            self.selected = ""
        if all(ac.icao != self.auto for ac in self._auto_candidates()):
            self.auto = self._auto_candidates()[0].icao if self._auto_candidates() else ""
        self._remember_positions(aircraft)
        self._count_messages()
        self.radar.aircraft = aircraft
        self.radar.centre, self.radar.centre_source = centre, source
        self.radar.total = len(self.order)
        self.radar.rate = "" if self.message_rate is None else f"{self.message_rate:.0f} msg/s"
        self.radar.place = self._place(source)
        self._show()

    def _find_centre(self, aircraft: list[Aircraft]) -> tuple[tuple[float, float] | None, str]:
        where = adsb.receiver_position(self.state)
        if where is not None:
            return (where[0], where[1]), where[2]
        if self._traffic_centre is None:               # fixed once found, or the map would swim
            self._traffic_centre = adsb.middle_of(aircraft)
        return self._traffic_centre, "traffic" if self._traffic_centre else ""

    def _place(self, source: str) -> str:
        """The centre of the radar in words, for its top-right corner."""
        if source == "settings":
            return (zipcodes.place(self.state)
                    or zipcodes.coordinates_text(self.state.wx_lat, self.state.wx_lon)).upper()
        if source == "ip":
            wx = getattr(self.state.space_wx, "wx", None) or {}
            return str(wx.get("city") or "IP LOCATION").upper()
        return ""

    def _remember_positions(self, aircraft: list[Aircraft]) -> None:
        trails = self.radar.trails
        current = {ac.icao for ac in aircraft}
        for icao in [icao for icao in trails if icao not in current]:
            del trails[icao]
        for ac in aircraft:
            if not ac.has_position:
                continue
            trail = trails.setdefault(ac.icao, deque(maxlen=config.ADSB_TRAIL_POINTS))
            if trail and (abs(trail[-1][0] - ac.lat) > TRAIL_BREAK_DEG
                          or abs((trail[-1][1] - ac.lon + 180.0) % 360.0 - 180.0) > TRAIL_BREAK_DEG):
                trail.clear()                          # a bad decode or a long silence: no line across the map
            if not trail or trail[-1] != (ac.lat, ac.lon):
                trail.append((ac.lat, ac.lon))

    def _count_messages(self) -> None:
        now, total = time.monotonic(), self.state.adsb_messages
        if self._counted is not None and now - self._counted[0] >= 0.2:
            self.message_rate = max(0.0, (total - self._counted[1]) / (now - self._counted[0]))
        if self._counted is None or now - self._counted[0] >= 0.2:
            self._counted = (now, total)

    # ------------------------------------------------------------------ what is shown
    @property
    def shown(self) -> str:
        """ICAO address of the aircraft whose data is on screen: the selection, or AUTO's choice."""
        return self.selected or self.auto

    def shown_aircraft(self) -> Aircraft | None:
        return next((ac for ac in self.order if ac.icao == self.shown), None)

    def selected_aircraft(self) -> Aircraft | None:
        return next((ac for ac in self.order if ac.icao == self.selected), None) if self.selected else None

    def listing(self) -> list[Aircraft]:
        """The flights in the order of the list view."""
        return sorted_flights(self.order, self.sort)

    def _auto_candidates(self) -> list[Aircraft]:
        """What AUTO steps through: the aircraft on the map, or all of them if none is."""
        return [ac for ac in self.order if ac.has_position] or self.order

    def auto_step(self) -> None:
        """AUTO: move on to the next aircraft (nothing happens while one is selected)."""
        candidates = self._auto_candidates()
        if self.selected or not candidates:
            return
        index = next((i for i, ac in enumerate(candidates) if ac.icao == self.auto), -1)
        self.auto = candidates[(index + 1) % len(candidates)].icao
        self._show()

    def _show(self) -> None:
        self.radar.selected = self.shown
        self.radar.update()
        self.panel.show_flight(self.shown_aircraft(), auto=not self.selected)
        self.flight_list.show_flights(self.listing(), self.shown, self.sort, self.list_page)
        self.list_page = self.flight_list.page

    def show_view(self, view: str) -> None:
        self.view = view
        self.views.setCurrentIndex(1 if view == "list" else 0)
        if view == "list":
            self._turn_to_shown()
        self.softkeys_changed.emit()

    def _turn_to_shown(self) -> None:
        """Turn the list to the page with the aircraft being shown."""
        index = next((i for i, ac in enumerate(self.listing()) if ac.icao == self.shown), None)
        if index is not None:
            self.list_page = index // config.ADSB_LIST_ROWS
        self._show()

    def set_sort(self, sort: str) -> None:
        self.sort = sort if sort in SORTS else SORTS[0]
        self.list_page = 0
        self._show()
        self.softkeys_changed.emit()

    def step_list_page(self, delta: int) -> None:
        self.list_page = (self.list_page + delta) % self.flight_list.pages
        self._show()

    # ------------------------------------------------------------------ selection
    def select(self, icao: str) -> None:
        """Hold on one aircraft; "" (or one that is not there) returns to AUTO."""
        self.selected = icao if any(ac.icao == icao for ac in self.order) else ""
        if self.selected:
            self.auto = self.selected                  # AUTO carries on from here later
        self._auto_timer.start()                       # a full turn for whatever is shown next
        if self.view == "list" and self.selected:
            self._turn_to_shown()
        else:
            self._show()

    def _picked_in_list(self, icao: str) -> None:
        self.select(icao)
        self.show_view("radar")

    def step_selection(self, delta: int) -> None:
        """Next / previous aircraft from the one being shown, in the order of the view."""
        flights = self.listing() if self.view == "list" else self.order
        if not flights:
            return
        index = next((i for i, ac in enumerate(flights) if ac.icao == self.shown), None)
        if index is None:
            index = 0 if delta > 0 else len(flights) - 1
        elif self.selected:
            index = (index + delta) % len(flights)
        self.select(flights[index].icao)               # the first movement holds what AUTO was showing

    # ------------------------------------------------------------------ controls
    def softkey_labels(self) -> list[str]:
        if self.view == "list":
            return [f"SORT {self.sort}", "PAGE", "RADAR", "BACK"]
        return ["ZOOM \u2212", "ZOOM +", "LIST", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 3:
            self.stop_requested.emit()
        elif self.view == "list":
            if index == 0:
                self.set_sort(SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)])
            elif index == 1:
                self.step_list_page(+1)
            else:
                self.show_view("radar")
        elif index == 0:
            self.state.step_adsb_range(+1)
        elif index == 1:
            self.state.step_adsb_range(-1)
        elif index == 2:
            self.show_view("list")

    def on_softkey_long(self, index: int) -> bool:
        """Held keys are the radar's on-screen buttons, for use without touching the screen."""
        s = self.state
        if self.view == "radar":
            if index == 0:
                s.toggle_adsb_ground()
                s.message.emit(f"Ground traffic {'shown' if s.adsb_ground else 'hidden'}")
            elif index == 1:
                s.toggle_adsb_trails()
                s.message.emit(f"Trails {'on' if s.adsb_trails else 'off'}")
            elif index == 2:
                s.cycle_adsb_labels()
                s.message.emit(f"Labels {s.adsb_labels}")
        return True                                    # no hidden ways out: BACK returns the dongle

    def on_knob(self, detents: int) -> bool:
        self.step_selection(1 if detents > 0 else -1)
        return True

    def on_encoder_push(self) -> bool:
        if self.view == "list":
            self.select(self.shown)                    # show this one on the radar
            self.show_view("radar")
        else:
            self.select("")
        return True
