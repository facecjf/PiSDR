"""Spectrum + waterfall display built on pyqtgraph. Tap anywhere to tune there.

Spectrum frames from the radio thread are coalesced onto a timer so the Qt event
queue never fills with stale FFT updates (that was starving ALSA on the Pi when the
waterfall was visible). Updates are skipped entirely while this widget is hidden.

On top of the trace: markers on detected signals (with their level above the noise),
frequency tags (memories, learned FM names, well-known frequencies) and a small control
strip for zoom and seek. A tap near a detected signal snaps to it (Settings > RADIO).
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from .. import config, signals
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from ..tags import TagStore
from . import style

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False, background=style.BG, foreground=style.TEXT_DIM)

_FILL_FLOOR_DB = -300.0      # fill the trace down past the bottom of any view
_WF_EMPTY_DB = -300.0        # below any level range, so unfilled waterfall rows draw as colour 0
_TAG_ROW_PX = 15
_WF_AXIS_PX = 20


def _colormap(stops: list) -> pg.ColorMap:
    pos = np.array([p for p, _ in stops])
    colors = np.array([[r, g, b, 255] for _, (r, g, b) in stops], dtype=np.ubyte)
    return pg.ColorMap(pos, colors)


def _font(px: int, bold: bool = False) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class _Controls(QtWidgets.QWidget):
    """Seek down / zoom out / zoom level / zoom in / seek up, floating over the spectrum."""

    seek = Signal(int)
    zoom = Signal(int)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for text, slot in (("\u25c0", lambda: self.seek.emit(-1)), ("\u2212", lambda: self.zoom.emit(-1))):
            row.addWidget(self._button(text, slot))
        self.level = QtWidgets.QLabel("x1")
        self.level.setObjectName("speczoom")
        self.level.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.level.setFixedSize(36, 28)
        row.addWidget(self.level)
        for text, slot in (("+", lambda: self.zoom.emit(1)), ("\u25b6", lambda: self.seek.emit(1))):
            row.addWidget(self._button(text, slot))
        self.setFixedSize(196, 28)

    def _button(self, text: str, slot) -> QtWidgets.QPushButton:
        btn = QtWidgets.QPushButton(text)
        btn.setObjectName("specbtn")
        btn.setFixedSize(36, 28)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(slot)
        return btn


class SpectrumWaterfall(QtWidgets.QWidget):
    """Two linked plots: spectrum on top, scrolling waterfall below.

    Frequencies are plotted in MHz. The widget keeps its own smoothed level range so the
    waterfall colours stay useful as gain or band changes.
    """

    tuned = Signal(float)   # Hz, emitted when the user taps a frequency or seeks

    def __init__(self, state: RadioState, tags: TagStore | None = None, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.tags = tags or TagStore(state, self)
        self.center_hz = 0.0
        self.span_hz = float(config.SAMPLE_RATE)
        self._rows = config.WATERFALL_ROWS
        # Waterfall is stored at half FFT width to cut ImageItem upload cost on the Pi.
        self._wf_cols = config.FFT_SIZE // 2
        self._wf = np.full((self._rows, self._wf_cols), _WF_EMPTY_DB, dtype=np.float32)
        self._lvl_min = -110.0
        self._lvl_max = -40.0
        self._avg: np.ndarray | None = None
        self._pending: np.ndarray | None = None
        self._frame_i = 0
        self._x_mhz: np.ndarray | None = None
        self._signals: list[signals.Signal] = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.glw.ci.layout.setSpacing(0)
        layout.addWidget(self.glw)

        self._build_spectrum_plot()
        self._build_waterfall_plot()
        self.glw.ci.layout.setRowStretchFactor(0, 45)
        self.glw.ci.layout.setRowStretchFactor(1, 55)

        self.controls = _Controls(self)
        self.controls.seek.connect(self.seek)
        self.controls.zoom.connect(self.state.step_spectrum_zoom)

        self.glw.scene().sigMouseClicked.connect(self._on_click)
        self.spec_plot.getViewBox().sigResized.connect(self._layout_labels)
        state.changed.connect(self._on_state_changed)
        self.tags.changed.connect(self._layout_labels)
        self.set_center(0.0, self.span_hz)

        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setInterval(config.SPECTRUM_UI_MS)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()
        self.retheme()

    # ------------------------------------------------------------------ construction
    def _build_spectrum_plot(self) -> None:
        self.spec_plot = self.glw.addPlot(row=0, col=0)
        self._style_plot(self.spec_plot)
        self.spec_plot.showGrid(x=True, y=True, alpha=0.12)
        self.spec_plot.setYRange(-120, -20, padding=0)
        self.spec_plot.getAxis("left").setWidth(34)
        self.spec_plot.getAxis("left").setStyle(tickFont=_font(11), tickLength=-4)
        self.spec_plot.hideAxis("bottom")
        self.peak_curve = self.spec_plot.plot()
        self.spec_curve = self.spec_plot.plot()
        for curve in (self.peak_curve, self.spec_curve):
            try:
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
            except Exception:
                pass
        self._peak: np.ndarray | None = None
        self.peak_curve.setVisible(self.state.peak_hold)
        self.passband = pg.LinearRegionItem(values=(0, 1), movable=False, pen=pg.mkPen(None))
        self.passband.setZValue(-5)
        self.spec_plot.addItem(self.passband)
        self.vfo_line = pg.InfiniteLine(angle=90, movable=False)
        self.spec_plot.addItem(self.vfo_line)

        # Signal markers: one scatter item for the triangles, a pool of text items for levels.
        self.sig_marks = pg.ScatterPlotItem(size=9, symbol="t", pen=None)
        self.sig_marks.setZValue(15)
        self.spec_plot.addItem(self.sig_marks, ignoreBounds=True)
        self.sig_texts = self._text_pool(config.SIGNAL_MAX_MARKERS, (0.5, 1.0), 10)
        # Frequency tags: dashed ticks plus a pool of labels along the top.
        self.tag_ticks = pg.PlotCurveItem(connect="pairs")
        self.tag_ticks.setZValue(-4)
        self.spec_plot.addItem(self.tag_ticks, ignoreBounds=True)
        self.tag_texts = self._text_pool(config.MAX_TAG_LABELS, (0.5, 0.0), 10)
        self.pb_text = self._text_pool(1, (0.5, 0.0), 12, bold=True)[0]

    def _build_waterfall_plot(self) -> None:
        self.wf_plot = self.glw.addPlot(row=1, col=0)
        self._style_plot(self.wf_plot)
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_plot.getAxis("left").setWidth(34)
        self.wf_plot.getAxis("left").setStyle(showValues=False, tickLength=0)
        self.wf_plot.getAxis("bottom").setStyle(tickFont=_font(11), tickLength=-4)
        self.wf_plot.getAxis("bottom").setHeight(_WF_AXIS_PX)
        self.wf_plot.setYRange(0, self._rows, padding=0)
        self.wf_plot.invertY(True)
        self.wf_img = pg.ImageItem(self._wf)
        self.wf_plot.addItem(self.wf_img)
        self.wf_vfo_line = pg.InfiniteLine(angle=90, movable=False)
        self.wf_plot.addItem(self.wf_vfo_line)

    def _text_pool(self, n: int, anchor: tuple[float, float], px: int,
                   bold: bool = False) -> list[pg.TextItem]:
        items = []
        for _ in range(n):
            item = pg.TextItem("", anchor=anchor)
            item.setFont(_font(px, bold))
            item.setZValue(20)
            item.setVisible(False)
            self.spec_plot.addItem(item, ignoreBounds=True)
            items.append(item)
        return items

    @staticmethod
    def _style_plot(plot: pg.PlotItem) -> None:
        plot.setMenuEnabled(False)
        plot.hideButtons()
        plot.setMouseEnabled(x=False, y=False)
        plot.showGrid(x=True, y=False, alpha=0.15)
        vb = plot.getViewBox()
        vb.setDefaultPadding(0)
        vb.setBorder(None)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Bottom-right of the waterfall: covers only the oldest history, never the labels.
        self.controls.move(self.width() - self.controls.width() - 4,
                           self.height() - _WF_AXIS_PX - self.controls.height() - 4)
        self.controls.raise_()

    # ------------------------------------------------------------------ styling
    def retheme(self) -> None:
        """Re-read colours from :mod:`style` after a theme or palette change."""
        self.glw.setBackground(style.BG)
        stops = style.palette_stops(self.state.wf_palette)
        self.wf_img.setLookupTable(_colormap(stops).getLookupTable(nPts=256))
        self._apply_trace_style()
        self.peak_curve.setPen(pg.mkPen(style.TEXT_DIM, width=1))
        self.vfo_line.setPen(pg.mkPen(style.ACCENT, width=2))
        self.wf_vfo_line.setPen(pg.mkPen(style.ACCENT, width=1))
        accent = QtGui.QColor(style.ACCENT)
        accent.setAlpha(45)
        self.passband.setBrush(pg.mkBrush(accent))
        self.sig_marks.setBrush(pg.mkBrush(style.TEXT))
        for item in self.sig_texts:
            item.setColor(style.TEXT)
        self.pb_text.setColor(style.ACCENT)
        dim = QtGui.QColor(style.TEXT_DIM)
        dim.setAlpha(110)
        self.tag_ticks.setPen(pg.mkPen(dim, width=1, style=Qt.PenStyle.DashLine))
        axis_pen = pg.mkPen(style.TEXT_DIM)
        for plot in (self.spec_plot, self.wf_plot):
            for name in ("left", "bottom"):
                ax = plot.getAxis(name)
                ax.setPen(axis_pen)
                ax.setTextPen(axis_pen)
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(self._lvl_min, self._lvl_max))
        self._layout_labels()

    def _apply_trace_style(self) -> None:
        stops = style.palette_stops(self.state.wf_palette)
        if self.state.spectrum_style == "FILLED":
            r, g, b = stops[-1][1] if len(stops) > 1 else (255, 255, 255)
            self.spec_curve.setPen(pg.mkPen(QtGui.QColor(r, g, b, 230), width=1))
            self.spec_curve.setFillLevel(_FILL_FLOOR_DB)
            self._update_fill()
        else:
            self.spec_curve.setPen(pg.mkPen(style.ACCENT_2, width=1))
            self.spec_curve.setFillLevel(None)
            self.spec_curve.setBrush(None)

    def _update_fill(self) -> None:
        """Vertical gradient in dB: the waterfall palette, fading out towards the noise floor."""
        if self.state.spectrum_style != "FILLED":
            return
        cmap = _colormap(style.palette_stops(self.state.wf_palette))
        grad = QtGui.QLinearGradient(0.0, self._lvl_min, 0.0, self._lvl_max)
        for pos in np.linspace(0.0, 1.0, 6):
            # Skip the darkest quarter of the palette so the noise floor fill stays visible.
            color = cmap.mapToQColor(0.25 + 0.75 * pos)
            color.setAlpha(int(110 + 120 * pos))
            grad.setColorAt(float(pos), color)
        self.spec_curve.setBrush(QtGui.QBrush(grad))

    # ------------------------------------------------------------------ public API
    def set_center(self, center_hz: float, span_hz: float) -> None:
        self.center_hz = center_hz
        self.span_hz = span_hz
        lo = (center_hz - span_hz / 2) / 1e6
        hi = (center_hz + span_hz / 2) / 1e6
        self.spec_plot.setXRange(lo, hi, padding=0)
        self.wf_img.setRect(QtCore.QRectF(lo, 0, hi - lo, self._rows))
        self._wf[:] = _WF_EMPTY_DB
        self._avg = None
        self._peak = None
        self._pending = None
        self._signals = []
        self._x_mhz = np.linspace(lo, hi, config.FFT_SIZE)
        self.controls.level.setText(f"x{self.state.spectrum_zoom}")
        self._update_markers()
        self._update_signal_marks()
        self._layout_labels()

    def push_spectrum(self, db: np.ndarray) -> None:
        """Called from the GUI thread via a queued signal; keep only the latest frame."""
        if not self.isVisible():
            self._pending = None
            return
        self._pending = db

    def seek(self, direction: int) -> None:
        """Tune the next detected signal above (+1) or below (-1) the VFO."""
        found = signals.seek(self._signals, self.state.vfo_hz, direction, self.state.mode,
                             self.state.step_hz, self._bin_hz())
        if found is None:
            self.state.message.emit("No signal " + ("above" if direction > 0 else "below") + " in view")
            return
        self._tune_signal(*found)

    # ------------------------------------------------------------------ frames
    def _flush_pending(self) -> None:
        db = self._pending
        if db is None or not self.isVisible():
            return
        self._pending = None
        self._draw_frame(db)

    def _draw_frame(self, db: np.ndarray) -> None:
        alpha = config.SPECTRUM_AVG_ALPHA[self.state.spectrum_avg % len(config.SPECTRUM_AVG_ALPHA)]
        if self._avg is None or self._avg.shape != db.shape or alpha >= 1.0:
            self._avg = db.astype(np.float32, copy=True)
        else:
            self._avg *= 1.0 - alpha
            self._avg += alpha * db

        if self._x_mhz is None or len(self._x_mhz) != len(self._avg):
            lo = (self.center_hz - self.span_hz / 2) / 1e6
            hi = (self.center_hz + self.span_hz / 2) / 1e6
            self._x_mhz = np.linspace(lo, hi, len(self._avg))
        self.spec_curve.setData(self._x_mhz, self._avg)
        if self.state.peak_hold:
            if self._peak is None or self._peak.shape != self._avg.shape:
                self._peak = self._avg.copy()
            else:
                np.maximum(self._peak, self._avg, out=self._peak)
            self.peak_curve.setData(self._x_mhz, self._peak)

        self._push_waterfall_row(db)

        self._frame_i += 1
        if self._frame_i % config.SIGNAL_DETECT_EVERY == 0:
            self._detect()
        # setYRange forces a relayout; do it sparingly.
        if self._frame_i % 8 == 0:
            self.spec_plot.setYRange(self._lvl_min - 6.0, self._lvl_max + self._headroom_db(), padding=0)
            self._update_fill()
            self._layout_labels()

    def _push_waterfall_row(self, db: np.ndarray) -> None:
        speed = max(1, int(self.state.waterfall_speed))
        row = db[::2] if len(db) == self._wf_cols * 2 else db[: self._wf_cols]
        # Shift older rows down without allocating a full rolled copy.
        self._wf[speed:] = self._wf[:-speed]
        self._wf[:speed] = row
        floor = float(np.percentile(db, 30))
        peak = float(np.percentile(db, 99.5))
        self._lvl_min += (floor - 4.0 - self._lvl_min) * 0.05
        self._lvl_max += (max(peak + 6.0, self._lvl_min + 35.0) - self._lvl_max) * 0.05
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(self._lvl_min, self._lvl_max))

    def _headroom_db(self) -> float:
        """dB above the strongest level that keeps peaks and their markers under the tag rows."""
        if self.state.spectrum_labels == "OFF":
            return 10.0
        reserve_px = 2 * _TAG_ROW_PX + 16
        plot_px = max(self.spec_plot.getViewBox().height(), reserve_px + 40)
        span_db = self._lvl_max - self._lvl_min + 6.0
        return max(10.0, span_db * reserve_px / (plot_px - reserve_px))

    # ------------------------------------------------------------------ signals
    def _bin_hz(self) -> float:
        n = len(self._avg) if self._avg is not None else config.FFT_SIZE
        return self.span_hz / max(1, n)

    def _detect(self) -> None:
        if self._avg is None:
            return
        start = self.center_hz - self.span_hz / 2
        self._signals = signals.detect(self._avg, start, self._bin_hz())
        self._update_signal_marks()

    def _update_signal_marks(self) -> None:
        show = self.state.spectrum_labels in ("BOTH", "SIGNALS")
        shown = self._signals[: config.SIGNAL_MAX_MARKERS] if show else []
        lift = (self._lvl_max - self._lvl_min) * 0.04
        xs = [s.peak_hz / 1e6 for s in shown]
        ys = [s.peak_db + lift for s in shown]
        self.sig_marks.setData(x=xs, y=ys)
        for i, item in enumerate(self.sig_texts):
            if i < len(shown):
                text = f"{shown[i].snr_db:.0f}"
                if item.textItem.toPlainText() != text:
                    item.setText(text)
                item.setPos(xs[i], ys[i] + lift * 0.5)
                item.setVisible(True)
            else:
                item.setVisible(False)

    def _tune_signal(self, sig: signals.Signal, target: float | None) -> None:
        if target is None:
            target = round(sig.centroid_hz / self.state.step_hz) * self.state.step_hz
        self.tuned.emit(float(target))
        if self.state.snap_mode == "TUNE+FILTER":
            width = signals.fit_width(sig, self.state.mode, self._bin_hz())
            if width is not None:
                self.state.fit_filter(width)

    # ------------------------------------------------------------------ tags
    def _layout_labels(self) -> None:
        """Place tag labels along the top in up to two rows, skipping ones that would overlap."""
        vb = self.spec_plot.getViewBox()
        width_px, height_px = vb.width(), vb.height()
        show = self.state.spectrum_labels in ("BOTH", "TAGS")
        if width_px < 50 or height_px < 30:
            return
        (x_lo, x_hi), (y_lo, y_hi) = vb.viewRange()
        mhz_per_px = (x_hi - x_lo) / width_px
        db_per_px = (y_hi - y_lo) / height_px
        pb_hz = self._passband_center()
        pb_tol = max(self.state.mode_spec.bandwidth_hz / 2, self._bin_hz())
        pb_label = self.tags.label_near(pb_hz, pb_tol) if show else ""
        busy: list[list[tuple[float, float]]] = [[], []]
        if pb_label:
            x = pb_hz / 1e6
            w = (len(pb_label) * 7.5 + 8) * mhz_per_px
            busy[0].append((x - w / 2, x + w / 2))
            self.pb_text.setText(pb_label)
            self.pb_text.setPos(x, y_hi)
        self.pb_text.setVisible(bool(pb_label))

        placed: list[tuple[float, float, str, str]] = []
        tags = self.tags.tags_between(x_lo * 1e6, x_hi * 1e6) if show else []
        for tag in tags:
            if pb_label and abs(tag.hz - pb_hz) <= pb_tol:
                continue
            x = tag.hz / 1e6
            w = (len(tag.label) * 6.2 + 8) * mhz_per_px
            for row in (0, 1):
                if all(x + w / 2 < a or x - w / 2 > b for a, b in busy[row]):
                    busy[row].append((x - w / 2, x + w / 2))
                    placed.append((x, y_hi - row * _TAG_ROW_PX * db_per_px, tag.label, tag.source))
                    break
            if len(placed) >= len(self.tag_texts):
                break

        colors = {"memory": style.ACCENT_2, "rds": style.TEXT}
        tick_x, tick_y = [], []
        for i, item in enumerate(self.tag_texts):
            if i < len(placed):
                x, y, label, source = placed[i]
                if item.textItem.toPlainText() != label:
                    item.setText(label)
                item.setColor(colors.get(source, style.TEXT_DIM))
                item.setPos(x, y)
                item.setVisible(True)
                tick_x += [x, x]
                tick_y += [y_lo, y - _TAG_ROW_PX * db_per_px]
            else:
                item.setVisible(False)
        self.tag_ticks.setData(tick_x, tick_y)

    def _passband_center(self) -> float:
        spec = self.state.mode_spec
        return self.state.vfo_hz + spec.offset_hz

    # ------------------------------------------------------------------ internals
    def _on_state_changed(self, field: str) -> None:
        if field in ("vfo_hz", "mode", "filter_index"):
            self._update_markers()
            self._layout_labels()
        elif field == "peak_hold":
            self._peak = None
            self.peak_curve.setVisible(self.state.peak_hold)
            if not self.state.peak_hold:
                self.peak_curve.setData([], [])
        elif field in ("spectrum_style", "wf_palette"):
            self.retheme()
        elif field == "spectrum_labels":
            self._update_signal_marks()
            self._layout_labels()
        elif field == "spectrum_zoom":
            self.controls.level.setText(f"x{self.state.spectrum_zoom}")

    def _update_markers(self) -> None:
        vfo = self.state.vfo_hz
        spec = self.state.mode_spec
        self.vfo_line.setPos(vfo / 1e6)
        self.wf_vfo_line.setPos(vfo / 1e6)
        lo = (vfo + spec.offset_hz - spec.bandwidth_hz / 2) / 1e6
        hi = (vfo + spec.offset_hz + spec.bandwidth_hz / 2) / 1e6
        self.passband.setRegion((lo, hi))

    def _on_click(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        for plot in (self.spec_plot, self.wf_plot):
            vb = plot.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                hz = vb.mapSceneToView(pos).x() * 1e6
                event.accept()
                if not self._snap(hz, vb):
                    step = self.state.step_hz
                    self.tuned.emit(round(hz / step) * step)
                return

    def _snap(self, hz: float, vb) -> bool:
        if self.state.snap_mode == "OFF" or not self._signals or vb.width() <= 0:
            return False
        tolerance = config.SIGNAL_SNAP_PX * self.span_hz / vb.width()
        sig = signals.signal_at(self._signals, hz, tolerance)
        if sig is None:
            return False
        target = signals.tune_target(sig, self.state.mode, self.state.step_hz, self._bin_hz())
        if target is None:
            return False     # e.g. SSB on a coarse view: tune where tapped
        self._tune_signal(sig, target)
        return True
