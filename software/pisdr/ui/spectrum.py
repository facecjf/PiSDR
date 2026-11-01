"""Spectrum + waterfall display built on pyqtgraph. Tap anywhere to tune there.

Spectrum frames from the radio thread are coalesced onto a timer so the Qt event
queue never fills with stale FFT updates (that was starving ALSA on the Pi when the
waterfall was visible). Updates are skipped entirely while this widget is hidden.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from .. import config
from ..qt import Qt, QtCore, QtGui, QtWidgets, Signal
from ..state import RadioState
from . import style

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False, background=style.BG, foreground=style.TEXT_DIM)


def _waterfall_colormap() -> pg.ColorMap:
    stops = style.WATERFALL
    pos = np.array([p for p, _ in stops])
    colors = np.array([[r, g, b, 255] for _, (r, g, b) in stops], dtype=np.ubyte)
    return pg.ColorMap(pos, colors)


class SpectrumWaterfall(QtWidgets.QWidget):
    """Two linked plots: spectrum on top, scrolling waterfall below.

    Frequencies are plotted in MHz. The widget keeps its own smoothed level range so the
    waterfall colours stay useful as gain or band changes.
    """

    tuned = Signal(float)   # Hz, emitted when the user taps a frequency

    def __init__(self, state: RadioState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.center_hz = 0.0
        self.span_hz = float(config.SAMPLE_RATE)
        self._rows = config.WATERFALL_ROWS
        # Waterfall is stored at half FFT width to cut ImageItem upload cost on the Pi.
        self._wf_cols = config.FFT_SIZE // 2
        self._wf = np.full((self._rows, self._wf_cols), -120.0, dtype=np.float32)
        self._lvl_min = -110.0
        self._lvl_max = -40.0
        self._avg: np.ndarray | None = None
        self._pending: np.ndarray | None = None
        self._frame_i = 0
        self._x_mhz: np.ndarray | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.glw.ci.layout.setSpacing(0)
        layout.addWidget(self.glw)

        # Spectrum plot
        self.spec_plot = self.glw.addPlot(row=0, col=0)
        self._style_plot(self.spec_plot)
        self.spec_plot.setYRange(-120, -20, padding=0)
        self.spec_plot.getAxis("left").setWidth(34)
        self.spec_plot.getAxis("left").setStyle(tickFont=self._small_font(), tickLength=-4)
        self.spec_plot.hideAxis("bottom")
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen(style.ACCENT_2, width=1))
        try:
            self.spec_curve.setDownsampling(auto=True, method="peak")
            self.spec_curve.setClipToView(True)
        except Exception:
            pass
        self.passband = pg.LinearRegionItem(values=(0, 1), movable=False,
                                            brush=pg.mkBrush(255, 176, 0, 45), pen=pg.mkPen(None))
        self.spec_plot.addItem(self.passband)
        self.vfo_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(style.ACCENT, width=2))
        self.spec_plot.addItem(self.vfo_line)

        # Waterfall plot
        self.wf_plot = self.glw.addPlot(row=1, col=0)
        self._style_plot(self.wf_plot)
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_plot.getAxis("left").setWidth(34)
        self.wf_plot.getAxis("left").setStyle(showValues=False, tickLength=0)
        self.wf_plot.getAxis("bottom").setStyle(tickFont=self._small_font(), tickLength=-4)
        self.wf_plot.getAxis("bottom").setHeight(20)
        self.wf_plot.setYRange(0, self._rows, padding=0)
        self.wf_plot.invertY(True)
        self.wf_img = pg.ImageItem(self._wf)
        self.wf_img.setLookupTable(_waterfall_colormap().getLookupTable(nPts=256))
        self.wf_plot.addItem(self.wf_img)
        self.wf_vfo_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(style.ACCENT, width=1))
        self.wf_plot.addItem(self.wf_vfo_line)

        self.glw.ci.layout.setRowStretchFactor(0, 45)
        self.glw.ci.layout.setRowStretchFactor(1, 55)

        self.glw.scene().sigMouseClicked.connect(self._on_click)
        state.changed.connect(self._on_state_changed)
        self.set_center(0.0, self.span_hz)

        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setInterval(config.SPECTRUM_UI_MS)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()
        self.retheme()

    def retheme(self) -> None:
        """Re-read colours from :mod:`style` after a theme change."""
        self.glw.setBackground(style.BG)
        self.spec_curve.setPen(pg.mkPen(style.ACCENT_2, width=1))
        self.vfo_line.setPen(pg.mkPen(style.ACCENT, width=2))
        self.wf_vfo_line.setPen(pg.mkPen(style.ACCENT, width=1))
        self.wf_img.setLookupTable(_waterfall_colormap().getLookupTable(nPts=256))
        axis_pen = pg.mkPen(style.TEXT_DIM)
        for plot in (self.spec_plot, self.wf_plot):
            for name in ("left", "bottom"):
                ax = plot.getAxis(name)
                ax.setPen(axis_pen)
                ax.setTextPen(axis_pen)
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(self._lvl_min, self._lvl_max))

    # ------------------------------------------------------------------ setup helpers
    @staticmethod
    def _small_font() -> QtGui.QFont:
        f = QtGui.QFont()
        f.setPixelSize(11)
        return f

    @staticmethod
    def _style_plot(plot: pg.PlotItem) -> None:
        plot.setMenuEnabled(False)
        plot.hideButtons()
        plot.setMouseEnabled(x=False, y=False)
        plot.showGrid(x=True, y=False, alpha=0.15)
        vb = plot.getViewBox()
        vb.setDefaultPadding(0)
        vb.setBorder(None)

    # ------------------------------------------------------------------ public API
    def set_center(self, center_hz: float, span_hz: float) -> None:
        self.center_hz = center_hz
        self.span_hz = span_hz
        lo = (center_hz - span_hz / 2) / 1e6
        hi = (center_hz + span_hz / 2) / 1e6
        self.spec_plot.setXRange(lo, hi, padding=0)
        self.wf_img.setRect(QtCore.QRectF(lo, 0, hi - lo, self._rows))
        self._wf[:] = self._lvl_min
        self._avg = None
        self._pending = None
        self._x_mhz = np.linspace(lo, hi, config.FFT_SIZE)
        self._update_markers()

    def push_spectrum(self, db: np.ndarray) -> None:
        """Called from the GUI thread via a queued signal; keep only the latest frame."""
        if not self.isVisible():
            self._pending = None
            return
        self._pending = db

    def _flush_pending(self) -> None:
        db = self._pending
        if db is None or not self.isVisible():
            return
        self._pending = None
        self._draw_frame(db)

    def _draw_frame(self, db: np.ndarray) -> None:
        if self._avg is None or self._avg.shape != db.shape:
            self._avg = db.astype(np.float32, copy=True)
        else:
            self._avg *= 0.6
            self._avg += 0.4 * db

        if self._x_mhz is None or len(self._x_mhz) != len(self._avg):
            lo = (self.center_hz - self.span_hz / 2) / 1e6
            hi = (self.center_hz + self.span_hz / 2) / 1e6
            self._x_mhz = np.linspace(lo, hi, len(self._avg))
        self.spec_curve.setData(self._x_mhz, self._avg)

        speed = max(1, int(self.state.waterfall_speed))
        # Shift older rows down without allocating a full rolled copy when speed==1.
        if speed == 1:
            self._wf[1:] = self._wf[:-1]
            self._wf[0] = db[::2] if len(db) == self._wf_cols * 2 else db[: self._wf_cols]
        else:
            self._wf[speed:] = self._wf[:-speed]
            row = db[::2] if len(db) == self._wf_cols * 2 else db[: self._wf_cols]
            self._wf[:speed] = row

        floor = float(np.percentile(db, 30))
        peak = float(np.percentile(db, 99.5))
        self._lvl_min += (floor - 4.0 - self._lvl_min) * 0.05
        self._lvl_max += (max(peak + 6.0, self._lvl_min + 35.0) - self._lvl_max) * 0.05
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(self._lvl_min, self._lvl_max))

        self._frame_i += 1
        # setYRange forces a relayout; do it sparingly.
        if self._frame_i % 8 == 0:
            self.spec_plot.setYRange(self._lvl_min - 6.0, self._lvl_max + 10.0, padding=0)

    # ------------------------------------------------------------------ internals
    def _on_state_changed(self, field: str) -> None:
        if field in ("vfo_hz", "mode"):
            self._update_markers()

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
                mhz = vb.mapSceneToView(pos).x()
                hz = mhz * 1e6
                step = self.state.step_hz
                self.tuned.emit(round(hz / step) * step)
                event.accept()
                return
