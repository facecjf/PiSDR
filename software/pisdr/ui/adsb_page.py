"""ADS-B map: a radar-style plot of the aircraft the decoder is tracking, plus a list.

Shown while a dump1090-family decoder owns the dongle (see ``radio/adsb_decoder.py``); BACK
gives the dongle back to the receiver. North is up, the receiver is in the middle and the
rings are distances from it in nautical miles.

    ZOOM - / ZOOM +   map range (config.ADSB_RANGES_NM)
    LABELS            FULL (callsign + flight level) / NAME / OFF (selected aircraft only)
    knob / tap        select an aircraft; push the knob to clear the selection
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque

import numpy as np

from .. import adsb, config, outlines
from ..adsb import Aircraft
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from . import style
from .list_pages import ListRow
from .pickers import Page

log = logging.getLogger(__name__)

UP, DOWN = "\u2191", "\u2193"
LEVEL_RATE_FPM = 250          # slower climbs / descents than this are drawn as level flight
TAP_RADIUS_PX = 36            # a tap this close to an aircraft selects it
TRAIL_BREAK_DEG = 0.5         # a jump this big between two positions starts a new trail
EDGE_PX = 16                  # between the outer ring and the top / bottom of the plot


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


def detail_html(ac: Aircraft) -> str:
    """The selected aircraft, spelled out, for the panel beside the map."""
    dim = style.TEXT_DIM

    def field(label: str, value: str) -> str:
        return f"<span style='color:{dim}'>{label}</span> {value}"

    if ac.on_ground:
        height = "on the ground"
    elif ac.altitude_ft is None:
        height = "--"
    else:
        height = f"{ac.altitude_ft:,} ft"
        if trend(ac):
            height += f" &nbsp;{trend(ac)}{abs(ac.vert_rate_fpm):,} fpm"
    speed = "--" if ac.speed_kt is None else f"{ac.speed_kt:.0f} kt"
    track = "--" if ac.track_deg is None else f"{ac.track_deg:03.0f}\u00b0"
    if not ac.has_position:
        where = f"<span style='color:{dim}'>no position received yet</span>"
    elif ac.distance_nm is None:
        where = f"{abs(ac.lat):.2f}\u00b0{'N' if ac.lat >= 0 else 'S'} {abs(ac.lon):.2f}\u00b0{'E' if ac.lon >= 0 else 'W'}"
    else:
        where = (f"{ac.distance_nm:.1f} nm &nbsp;{adsb.compass_point(ac.bearing_deg)} "
                 f"<span style='color:{dim}'>({ac.bearing_deg:03.0f}\u00b0)</span>")
    signal = "--" if ac.rssi_db is None else f"{ac.rssi_db:.0f} dB"
    return "<br>".join((
        f"<span style='font-size:21px; font-weight:bold; color:{style.ACCENT}'>{ac.name}</span>"
        f" &nbsp;<span style='color:{dim}'>{ac.icao if ac.callsign else 'no callsign yet'}</span>",
        field("ALT", height),
        f"{field('SPD', speed)} &nbsp; {field('TRK', track)}",
        field("POS", where),
        f"{field('SQK', ac.squawk or '--')} &nbsp; {field('SIG', signal)}",
    ))


def _font(pixels: int, bold: bool = False) -> QtGui.QFont:
    font = QtGui.QFont()
    font.setPixelSize(pixels)
    font.setBold(bold)
    return font


def _colour(name: str, alpha: int = 255) -> QtGui.QColor:
    colour = QtGui.QColor(name)
    colour.setAlpha(alpha)
    return colour


# --------------------------------------------------------------------------- the plot
class RadarView(QtWidgets.QWidget):
    """North-up plot of aircraft around :attr:`centre`. The page feeds it; it only draws."""

    picked = Signal(str)          # ICAO address of the tapped aircraft, "" for empty sky

    _ARROW = QtGui.QPolygonF([QtCore.QPointF(0, -10), QtCore.QPointF(6.5, 7),
                              QtCore.QPointF(0, 3), QtCore.QPointF(-6.5, 7)])

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.centre: tuple[float, float] | None = None
        self.centre_source = ""                       # "settings", "ip", "traffic" or ""
        self.selected = ""
        self.trails: dict[str, deque] = {}            # ICAO -> (lat, lon) positions, oldest first
        self._hits: list[tuple[str, float, float]] = []
        self._outline_key: tuple | None = None
        self._outline_paths: dict[int, QtGui.QPainterPath] = {}
        self.setMinimumSize(240, 240)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # ------------------------------------------------------------------ geometry
    def scale(self) -> float:
        """Pixels per nautical mile: the selected range reaches the top and bottom of the plot."""
        return max(1.0, min(self.width(), self.height()) / 2.0 - EDGE_PX) / self.state.adsb_range_nm

    def to_screen(self, east_nm: float, north_nm: float) -> tuple[float, float]:
        scale = self.scale()
        return self.width() / 2.0 + east_nm * scale, self.height() / 2.0 - north_nm * scale

    def _reach_nm(self) -> float:
        """Distance from the centre to a corner of the plot."""
        return math.hypot(self.width(), self.height()) / 2.0 / self.scale()

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
        p.fillRect(self.rect(), QtGui.QColor(style.METER_BG))
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        if self.centre is not None:
            self._paint_outlines(p)
        self._paint_rings(p)
        self._hits = []
        if self.centre is not None:
            self._paint_trails(p)
            self._paint_aircraft(p)
        self._paint_notes(p)
        p.end()

    def _outlines(self) -> dict[int, QtGui.QPainterPath]:
        """Coast / border paths around the centre in nautical miles, rebuilt only when the view changes."""
        key = (self.centre, self.state.adsb_range_nm, self.width(), self.height())
        if key != self._outline_key:
            self._outline_key = key
            self._outline_paths = {}
            for kind, east, north in outlines.near(self.centre[0], self.centre[1], self._reach_nm() * 1.02):
                path = self._outline_paths.setdefault(kind, QtGui.QPainterPath())
                path.addPolygon(QtGui.QPolygonF([QtCore.QPointF(float(e), -float(n))
                                                 for e, n in zip(east, north)]))
        return self._outline_paths

    def _paint_outlines(self, p: QtGui.QPainter) -> None:
        pens = {
            outlines.COAST: QtGui.QPen(_colour(style.TEXT_DIM, 200), 1.3),
            outlines.LAKE: QtGui.QPen(_colour(style.TEXT_DIM, 200), 1.3),
            outlines.BORDER: QtGui.QPen(_colour(style.TEXT_DIM, 150), 1.0, Qt.PenStyle.DashLine),
            outlines.STATE: QtGui.QPen(_colour(style.TEXT_DIM, 95), 1.0, Qt.PenStyle.DotLine),
        }
        p.save()
        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.scale(self.scale(), self.scale())
        p.setBrush(Qt.BrushStyle.NoBrush)
        for kind, path in self._outlines().items():
            pen = pens[kind]
            pen.setCosmetic(True)                  # width in pixels whatever the zoom
            p.setPen(pen)
            p.drawPath(path)
        p.restore()

    def _paint_rings(self, p: QtGui.QPainter) -> None:
        cx, cy, scale = self.width() / 2.0, self.height() / 2.0, self.scale()
        spacing = dict(config.ADSB_RANGES_NM).get(self.state.adsb_range_nm, self.state.adsb_range_nm)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(_colour(style.BORDER, 150), 1))
        p.drawLine(QtCore.QPointF(cx, 0), QtCore.QPointF(cx, self.height()))
        p.drawLine(QtCore.QPointF(0, cy), QtCore.QPointF(self.width(), cy))
        p.setFont(_font(11))
        ring = spacing
        while ring <= self._reach_nm():
            radius = ring * scale
            p.setPen(QtGui.QPen(QtGui.QColor(style.BORDER), 1))
            p.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)
            if ring <= self.state.adsb_range_nm:   # labelled just inside each ring, on the north line
                p.setPen(QtGui.QColor(style.TEXT_DIM))
                unit = " nm" if ring == self.state.adsb_range_nm else ""
                p.drawText(QtCore.QPointF(cx + 4, cy - radius + 12), f"{ring}{unit}")
            ring += spacing
        p.setFont(_font(12, bold=True))
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        for letter, rect in (("N", QtCore.QRectF(cx - 22, 0, 18, EDGE_PX)),
                             ("S", QtCore.QRectF(cx - 22, self.height() - EDGE_PX, 18, EDGE_PX)),
                             ("W", QtCore.QRectF(2, cy - 18, 18, 16)),
                             ("E", QtCore.QRectF(self.width() - 20, cy - 18, 18, 16))):
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, letter)
        if self.centre is not None and self.centre_source != "traffic":
            p.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT_2), 1.5))     # the receiver
            p.drawEllipse(QtCore.QPointF(cx, cy), 4.0, 4.0)
            p.drawPoint(QtCore.QPointF(cx, cy))

    def _paint_trails(self, p: QtGui.QPainter) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for icao, trail in self.trails.items():
            if len(trail) < 2:
                continue
            points = np.asarray(trail, dtype=float)
            east, north = adsb.project(self.centre[0], self.centre[1], points[:, 0], points[:, 1])
            line = QtGui.QPolygonF([QtCore.QPointF(*self.to_screen(float(e), float(n)))
                                    for e, n in zip(east, north)])
            chosen = icao == self.selected
            p.setPen(QtGui.QPen(_colour(style.ACCENT_2 if chosen else style.ACCENT, 210 if chosen else 90),
                                1.6 if chosen else 1.2))
            p.drawPolyline(line)

    def _paint_aircraft(self, p: QtGui.QPainter) -> None:
        drawn: list[tuple[Aircraft, float, float]] = []
        for ac in self.state.aircraft:
            if not ac.has_position:
                continue
            east, north = adsb.project(self.centre[0], self.centre[1], ac.lat, ac.lon)
            x, y = self.to_screen(float(east), float(north))
            if -20 <= x <= self.width() + 20 and -20 <= y <= self.height() + 20:
                drawn.append((ac, x, y))
        drawn.sort(key=lambda item: item[0].icao == self.selected)    # the selected one goes on top
        self._hits = [(ac.icao, x, y) for ac, x, y in drawn]
        taken = [QtCore.QRectF(x - 9, y - 9, 18, 18) for _ac, x, y in drawn]
        p.setFont(_font(12, bold=True))
        for ac, x, y in drawn:
            chosen = ac.icao == self.selected
            fresh = ac.seen_pos_s <= config.ADSB_STALE_S and not ac.on_ground
            colour = QtGui.QColor(style.ACCENT_2 if chosen else style.ACCENT if fresh else style.TEXT_DIM)
            self._paint_glyph(p, ac, x, y, colour)
            if chosen:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QtGui.QPen(colour, 1.5))
                p.drawEllipse(QtCore.QPointF(x, y), 15.0, 15.0)
            lines = tag_lines(ac, self.state.adsb_labels, chosen)
            if lines:
                p.setPen(QtGui.QColor(style.TEXT) if chosen or fresh else colour)
                self._paint_tag(p, lines, x, y, taken)

    def _paint_tag(self, p: QtGui.QPainter, lines: list[str], x: float, y: float,
                   taken: list[QtCore.QRectF]) -> None:
        """Draw an aircraft's label in the first spot around it that is free and on the plot."""
        metrics = p.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 2.0
        height = 14.0 * len(lines)
        spots = [QtCore.QRectF(x + 13, y - 13, width, height),              # right, left, below, above
                 QtCore.QRectF(x - 13 - width, y - 13, width, height),
                 QtCore.QRectF(x - width / 2, y + 12, width, height),
                 QtCore.QRectF(x - width / 2, y - 12 - height, width, height)]
        on_plot = [spot for spot in spots if spot.left() >= 2 and spot.right() <= self.width() - 2]
        free = [spot for spot in on_plot if not any(spot.intersects(other) for other in taken)]
        spot = (free or on_plot or spots)[0]
        taken.append(spot)
        for row, line in enumerate(lines):
            p.drawText(QtCore.QPointF(spot.left(), spot.top() + 11 + row * 14), line)

    def _paint_glyph(self, p: QtGui.QPainter, ac: Aircraft, x: float, y: float, colour: QtGui.QColor) -> None:
        p.setPen(QtGui.QPen(QtGui.QColor(style.METER_BG), 1))
        p.setBrush(colour)
        if ac.on_ground:
            p.drawRect(QtCore.QRectF(x - 3.5, y - 3.5, 7, 7))
        elif ac.track_deg is None:
            p.drawEllipse(QtCore.QPointF(x, y), 4.5, 4.5)
        else:
            p.save()
            p.translate(x, y)
            p.rotate(ac.track_deg)                 # the arrow points up = north = track 0
            p.drawPolygon(self._ARROW)
            p.restore()

    def _paint_notes(self, p: QtGui.QPainter) -> None:
        dim = QtGui.QColor(style.TEXT_DIM)
        p.setFont(_font(12, bold=True))
        p.setPen(QtGui.QColor(style.ACCENT))
        p.drawText(QtCore.QPointF(8, 17), f"{self.state.adsb_range_nm} nm")
        note = {"ip": "Approximate centre, from your IP address - set a ZIP code in Settings > SYSTEM",
                "traffic": "Centred on the traffic - set a ZIP code in Settings > SYSTEM > Location"
                }.get(self.centre_source, "")
        if note:
            p.setFont(_font(11))
            p.setPen(dim)
            p.drawText(QtCore.QPointF(8, self.height() - 7), note)
        message, colour = self._overlay()
        if not message:
            return
        p.setFont(_font(16, bold=True))
        box = QtCore.QRectF(30, self.height() * 0.62, self.width() - 60, self.height() * 0.3)
        wrapped = QtGui.QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        wrapped.setWrapMode(QtGui.QTextOption.WrapMode.WordWrap)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_colour(style.METER_BG, 215))
        p.drawRoundedRect(p.boundingRect(box, message, wrapped).adjusted(-10, -6, 10, 6), 6, 6)
        p.setPen(colour)
        p.drawText(box, message, wrapped)

    def _overlay(self) -> tuple[str, QtGui.QColor]:
        """Text across the plot when there is nothing to draw, and its colour."""
        s = self.state
        if s.adsb_error:
            return f"{s.adsb_error}\nPress BACK to get the radio back.", QtGui.QColor(style.REC)
        if not s.aircraft:
            return "Listening on 1090 MHz...", QtGui.QColor(style.TEXT_DIM)
        if not any(ac.has_position for ac in s.aircraft):
            heard = len(s.aircraft)
            return (f"{heard} aircraft heard - waiting for {'its' if heard == 1 else 'a'} position",
                    QtGui.QColor(style.TEXT_DIM))
        return "", QtGui.QColor(style.TEXT_DIM)


# --------------------------------------------------------------------------- the page
class AdsbPage(Page):
    """Radar plot on the left, selected aircraft and the nearest few on the right."""

    stop_requested = Signal()
    ROWS = 6

    def __init__(self, state: RadioState, backend, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.backend = backend
        self.selected = ""                             # ICAO address
        self.order: list[Aircraft] = []                # nearest first; the knob steps through this
        self.message_rate: float | None = None
        self._traffic_centre: tuple[float, float] | None = None
        self._counted: tuple[float, int] | None = None   # (time, messages) at the previous update
        self._first_row = 0

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 6, 2)
        layout.setSpacing(6)
        self.radar = RadarView(state)
        self.radar.picked.connect(self.select)
        layout.addWidget(self.radar, 1)

        side = QtWidgets.QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(3)
        self.title = QtWidgets.QLabel("ADS-B  1090 MHz")
        self.rate = QtWidgets.QLabel("")
        self.rate.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.stats = QtWidgets.QLabel("")
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(self.title, 1)
        heading.addWidget(self.rate, 0)
        side.addLayout(heading)
        for label in (self.title, self.rate, self.stats):
            label.setObjectName("section")
            label.setFixedHeight(19)
        self.detail = QtWidgets.QLabel("")
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.detail.setContentsMargins(4, 0, 0, 0)
        self.detail.setFixedHeight(112)
        side.addWidget(self.stats)
        side.addWidget(self.detail)
        self.rows: list[ListRow] = []
        for index in range(self.ROWS):
            row = ListRow()
            row.clicked.connect(lambda i=index: self._row_tapped(i))
            self.rows.append(row)
            side.addWidget(row)
        self.more = QtWidgets.QLabel("")
        self.more.setObjectName("section")
        self.more.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        side.addWidget(self.more)
        side.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setFixedWidth(config.ADSB_PANEL_W)
        holder.setLayout(side)
        layout.addWidget(holder, 0)

        state.changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------ data
    def on_shown(self) -> None:
        self.selected = ""
        self.message_rate = None
        self._counted = None
        self._traffic_centre = None
        self._first_row = 0
        self.radar.trails.clear()
        self.refresh()

    def _on_state_changed(self, field: str) -> None:
        if field == "aircraft":
            if self.state.adsb_mode:
                self.refresh()
        elif field in ("adsb_range_nm", "adsb_labels", "theme"):
            self.radar.update()

    def refresh(self) -> None:
        """Take in the newest aircraft list: centre, distances, trails, then redraw."""
        aircraft: list[Aircraft] = self.state.aircraft
        centre, source = self._find_centre(aircraft)
        if centre is not None and source != "traffic":
            adsb.locate(aircraft, centre[0], centre[1])       # only the receiver gives real distances
        self.order = adsb.by_distance(aircraft)
        if self.selected and all(ac.icao != self.selected for ac in aircraft):
            self.selected = ""
        self._remember_positions(aircraft)
        self._count_messages()
        self.radar.centre, self.radar.centre_source = centre, source
        self.radar.selected = self.selected
        self.radar.update()
        self._fill_panel()

    def _find_centre(self, aircraft: list[Aircraft]) -> tuple[tuple[float, float] | None, str]:
        where = adsb.receiver_position(self.state)
        if where is not None:
            return (where[0], where[1]), where[2]
        if self._traffic_centre is None:               # fixed once found, or the map would swim
            self._traffic_centre = adsb.middle_of(aircraft)
        return self._traffic_centre, "traffic" if self._traffic_centre else ""

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

    # ------------------------------------------------------------------ panel
    def selected_aircraft(self) -> Aircraft | None:
        return next((ac for ac in self.order if ac.icao == self.selected), None)

    def _fill_panel(self) -> None:
        on_map = sum(1 for ac in self.order if ac.has_position)
        self.rate.setText("" if self.message_rate is None else f"{self.message_rate:.0f} msg/s")
        self.stats.setText(f"{len(self.order)} aircraft  \u00b7  {on_map} on map")
        chosen = self.selected_aircraft()
        if chosen is not None:
            self.detail.setText(detail_html(chosen))
        else:
            self.detail.setText(f"<span style='color:{style.TEXT_DIM}'>Turn the knob or tap an "
                                "aircraft<br>to see its details.</span>")
        index = self.order.index(chosen) if chosen is not None else 0
        last_first = max(0, len(self.order) - self.ROWS)
        if chosen is None:
            self._first_row = 0
        elif not self._first_row <= index < self._first_row + self.ROWS:
            self._first_row = index - self.ROWS // 2
        self._first_row = max(0, min(self._first_row, last_first))
        for offset, row in enumerate(self.rows):
            position = self._first_row + offset
            ac = self.order[position] if position < len(self.order) else None
            row.setVisible(ac is not None)
            if ac is None:
                continue
            row.left.setText(ac.name)
            row.right.setText(self._row_value(ac))
            row.set_selected(ac.icao == self.selected)
        hidden = len(self.order) - self.ROWS
        self.more.setText(f"{self._first_row + 1}-{self._first_row + self.ROWS} of {len(self.order)}"
                          if hidden > 0 else "")

    @staticmethod
    def _row_value(ac: Aircraft) -> str:
        if not ac.has_position:
            return f"{level_text(ac)}  no pos".strip()
        if ac.distance_nm is None:
            return level_text(ac)
        return f"{level_text(ac)}  {ac.distance_nm:.0f} nm".strip()

    def _row_tapped(self, index: int) -> None:
        position = self._first_row + index
        if position < len(self.order):
            self.select(self.order[position].icao)

    # ------------------------------------------------------------------ selection
    def select(self, icao: str) -> None:
        self.selected = icao if any(ac.icao == icao for ac in self.order) else ""
        self.radar.selected = self.selected
        self.radar.update()
        self._fill_panel()

    def step_selection(self, delta: int) -> None:
        """Next / previous aircraft by distance; from no selection the knob starts at either end."""
        if not self.order:
            return
        chosen = self.selected_aircraft()
        if chosen is None:
            index = 0 if delta > 0 else len(self.order) - 1
        else:
            index = (self.order.index(chosen) + delta) % len(self.order)
        self.select(self.order[index].icao)

    # ------------------------------------------------------------------ controls
    def softkey_labels(self) -> list[str]:
        return ["ZOOM \u2212", "ZOOM +", f"LABELS {self.state.adsb_labels}", "BACK"]

    def on_softkey(self, index: int) -> None:
        if index == 0:
            self.state.step_adsb_range(+1)
        elif index == 1:
            self.state.step_adsb_range(-1)
        elif index == 2:
            self.state.cycle_adsb_labels()
        elif index == 3:
            self.stop_requested.emit()

    def on_softkey_long(self, index: int) -> bool:
        return True                                    # no hidden ways out: BACK returns the dongle

    def on_knob(self, detents: int) -> bool:
        self.step_selection(1 if detents > 0 else -1)
        return True

    def on_encoder_push(self) -> bool:
        self.select("")
        return True
