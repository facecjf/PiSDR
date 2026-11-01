"""HF conditions side panel: solar indices, band conditions, 24 h Kp strip, local weather."""
from __future__ import annotations

import time

from .. import config
from ..qt import Qt, QtCore, QtGui, QtWidgets
from ..spacewx import BAND_GROUPS, SpaceWx, compass
from ..state import RadioState
from . import style


def _age(ts: float) -> str:
    if not ts:
        return "no data"
    s = time.time() - ts
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    return f"{s / 3600:.0f} h ago"


def _k_color(k: float | None) -> str:
    if k is None:
        return style.TEXT_DIM
    return style.OK if k < 3 else style.ACCENT if k < 5 else style.REC


def _cond_color(text: str) -> str:
    t = text.lower()
    if t.startswith("good"):
        return style.OK
    if t.startswith("fair"):
        return style.ACCENT
    if t.startswith("poor"):
        return style.REC
    return style.TEXT_DIM


def _fmt(value, spec: str = "", none: str = "--") -> str:
    return none if value is None else format(value, spec)


class _StatTile(QtWidgets.QFrame):
    def __init__(self, caption: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("stat")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(0)
        self.value = QtWidgets.QLabel("--")
        self.value.setObjectName("statval")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = QtWidgets.QLabel(caption)
        self.caption.setObjectName("statcap")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")


class _KpBars(QtWidgets.QWidget):
    """Eight 3-hourly Kp bars (24 h), coloured quiet/unsettled/storm."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        self.values: list[float] = []

    def set_values(self, values: list[float]) -> None:
        self.values = list(values)[-8:]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(style.METER_BG))
        w, h = self.width(), self.height()
        font = QtGui.QFont(self.font())
        font.setPixelSize(10)
        p.setFont(font)
        p.setPen(QtGui.QColor(style.TEXT_DIM))
        p.drawText(QtCore.QRectF(4, 0, 60, 12), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Kp 24 h")
        if not self.values:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "--")
            p.end()
            return
        n = 8
        slot = (w - 8) / n
        base = h - 3
        p.setPen(Qt.PenStyle.NoPen)
        for i, k in enumerate(self.values[-n:]):
            idx = i + (n - len(self.values[-n:]))
            bar_h = max(2.0, (base - 13) * min(k, 9.0) / 9.0)
            x = 4 + idx * slot + 2
            p.setBrush(QtGui.QColor(_k_color(k)))
            p.drawRoundedRect(QtCore.QRectF(x, base - bar_h, slot - 4, bar_h), 2, 2)
        p.end()


class HfPanel(QtWidgets.QFrame):
    """Fixed-width column shown to the right of the spectrum while on HF."""

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.setObjectName("hfpanel")
        self.setFixedWidth(config.HF_PANEL_W)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 6)
        layout.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("HF CONDITIONS")
        title.setObjectName("section")
        self.age = QtWidgets.QLabel("")
        self.age.setObjectName("age")
        self.age.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(title)
        head.addWidget(self.age, 1)
        layout.addLayout(head)

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(4)
        self.tiles = {name: _StatTile(name) for name in ("SFI", "SSN", "A", "K")}
        for tile in self.tiles.values():
            tiles.addWidget(tile, 1)
        layout.addLayout(tiles)

        self.solar_line = QtWidgets.QLabel("")
        self.solar_line.setObjectName("line")
        self.geo_line = QtWidgets.QLabel("")
        self.geo_line.setObjectName("line")
        layout.addWidget(self.solar_line)
        layout.addWidget(self.geo_line)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(1)
        for col, text in enumerate(("BAND", "DAY", "NIGHT")):
            hdr = QtWidgets.QLabel(text)
            hdr.setObjectName("gridhead")
            hdr.setAlignment(Qt.AlignmentFlag.AlignLeft if col == 0 else Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(hdr, 0, col)
        self.band_labels: dict[str, tuple[QtWidgets.QLabel, QtWidgets.QLabel]] = {}
        for row, name in enumerate(BAND_GROUPS, start=1):
            lab = QtWidgets.QLabel(name)
            lab.setObjectName("band")
            day = QtWidgets.QLabel("--")
            night = QtWidgets.QLabel("--")
            for w in (day, night):
                w.setObjectName("cond")
                w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(lab, row, 0)
            grid.addWidget(day, row, 1)
            grid.addWidget(night, row, 2)
            self.band_labels[name] = (day, night)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        layout.addLayout(grid)

        self.kp = _KpBars()
        layout.addWidget(self.kp)

        self.wx_line = QtWidgets.QLabel("")
        self.wx_line.setObjectName("line")
        self.wx_line.setWordWrap(True)
        layout.addWidget(self.wx_line)
        layout.addStretch(1)

        state.changed.connect(self._on_changed)
        self._age_timer = QtCore.QTimer(self)
        self._age_timer.setInterval(30_000)
        self._age_timer.timeout.connect(self._refresh_age)
        self._age_timer.start()
        self.retheme()

    # ------------------------------------------------------------------ theming
    def retheme(self) -> None:
        self.setStyleSheet(f"""
QFrame#hfpanel {{ background: {style.PANEL}; border-radius: 6px; }}
QFrame#hfpanel QLabel {{ background: transparent; }}
QFrame#hfpanel QLabel#age {{ font-size: 12px; color: {style.TEXT_DIM}; }}
QFrame#hfpanel QLabel#line {{ font-size: 13px; color: {style.TEXT}; }}
QFrame#hfpanel QLabel#gridhead {{ font-size: 11px; font-weight: bold; color: {style.TEXT_DIM}; }}
QFrame#hfpanel QLabel#band {{ font-size: 14px; color: {style.TEXT}; }}
QFrame#hfpanel QLabel#cond {{ font-size: 14px; font-weight: bold; }}
QFrame#stat {{ background: {style.PANEL_HI}; border: 1px solid {style.BORDER}; border-radius: 6px; }}
QFrame#stat QLabel#statval {{ font-size: 22px; font-weight: bold; color: {style.TEXT}; }}
QFrame#stat QLabel#statcap {{ font-size: 10px; color: {style.TEXT_DIM}; }}
""")
        self.kp.update()
        self.refresh()

    def _on_changed(self, field: str) -> None:
        if field == "space_wx":
            self.refresh()

    # ------------------------------------------------------------------ content
    def refresh(self) -> None:
        d: SpaceWx | None = self.state.space_wx
        if d is None:
            d = SpaceWx()
        dim = style.TEXT_DIM
        self.tiles["SFI"].set(_fmt(d.sfi))
        self.tiles["SSN"].set(_fmt(d.ssn))
        self.tiles["A"].set(_fmt(d.a_index), None if d.a_index is None or d.a_index < 20 else style.ACCENT)
        self.tiles["K"].set(_fmt(d.k_index), _k_color(d.k_index))

        wind = _fmt(d.solar_wind, ".0f")
        bz = _fmt(d.bz, "+.1f")
        bz_col = style.REC if d.bz is not None and d.bz <= -5 else style.TEXT
        self.solar_line.setText(
            f"<span style='color:{dim}'>X-ray</span> {d.xray or '--'} &nbsp; "
            f"<span style='color:{dim}'>Wind</span> {wind} km/s &nbsp; "
            f"<span style='color:{dim}'>Bz</span> <span style='color:{bz_col}'>{bz}</span>")
        self.geo_line.setText(
            f"<span style='color:{dim}'>Geomag</span> {d.geomag or '--'} &nbsp; "
            f"<span style='color:{dim}'>Noise</span> {d.noise or '--'} &nbsp; "
            f"<span style='color:{dim}'>Aurora</span> {_fmt(d.aurora)}")

        for name, (day, night) in self.band_labels.items():
            conds = d.bands.get(name, ["--", "--"])
            for label, text in zip((day, night), conds):
                label.setText(text)
                label.setStyleSheet(f"color: {_cond_color(text)};")

        self.kp.set_values(d.kp_history)

        wx = d.wx
        if wx and wx.get("temp") is not None:
            tu = wx.get("temp_unit", "°")
            desc = wx.get("desc", "")
            desc_col = style.REC if "thunder" in desc.lower() else style.TEXT
            where = wx.get("city") or ""
            self.wx_line.setText(
                f"<span style='color:{desc_col}'><b>{desc}</b></span> "
                f"{wx['temp']:.0f}{tu} &nbsp; {_fmt(wx.get('humidity'), '.0f')}% &nbsp; "
                f"{_fmt(wx.get('pressure'), '.0f')} hPa &nbsp; "
                f"{compass(wx.get('wind_dir'))} {_fmt(wx.get('wind'), '.0f')} {wx.get('wind_unit', '')}"
                + (f" <span style='color:{dim}'>· {where}</span>" if where else ""))
        else:
            self.wx_line.setText(f"<span style='color:{dim}'>Local weather: {'--' if d.wx_at else 'waiting for network'}</span>")
        self._refresh_age()

    def _refresh_age(self) -> None:
        d = self.state.space_wx
        ts = d.solar_at if d else 0.0
        text = _age(ts)
        stale = not ts or time.time() - ts > 2 * config.SPACEWX_PERIOD_S["solar"]
        self.age.setText(("offline · " if stale and ts else "") + text)
        self.age.setStyleSheet(f"color: {style.ACCENT if stale else style.TEXT_DIM}; font-size: 12px;")
