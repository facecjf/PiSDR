"""Find individual signals in a spectrum frame and work out where to tune for each.

Used by the spectrum display for the signal markers, snap-to-signal on tap and the
seek buttons. Pure numpy so it can be tested without Qt or GNU Radio.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config

_FLOOR_SEGMENTS = 32     # local noise floor is estimated per segment so slopes don't fool it


@dataclass(frozen=True)
class Signal:
    lo_hz: float          # lower edge (SIGNAL_EDGE_DB above the floor)
    hi_hz: float          # upper edge
    peak_hz: float        # strongest point, interpolated between bins
    centroid_hz: float    # power-weighted centre
    peak_db: float
    snr_db: float
    occupied_hz: float = 0.0   # bandwidth holding 99 % of the power above the floor

    @property
    def width_hz(self) -> float:
        return self.hi_hz - self.lo_hz


def noise_floor(db: np.ndarray) -> np.ndarray:
    """Per-bin noise floor: 20th percentile of each segment, interpolated across the frame."""
    n = len(db)
    segments = min(_FLOOR_SEGMENTS, max(1, n // 16))
    edges = np.linspace(0, n, segments + 1).astype(int)
    levels = np.array([np.percentile(db[a:b], 20) for a, b in zip(edges[:-1], edges[1:])])
    centres = (edges[:-1] + edges[1:]) / 2.0
    # A segment filled by one wide signal (WFM) reads high. Fit the overall tilt with those
    # segments rejected, then never let a segment sit more than 3 dB above the fit.
    if segments >= 4:
        keep = np.ones(segments, dtype=bool)
        for _ in range(3):
            fit = np.polyval(np.polyfit(centres[keep], levels[keep], 1), centres)
            new_keep = levels <= fit + 3.0
            if new_keep.sum() < segments // 2 or np.array_equal(new_keep, keep):
                break
            keep = new_keep
        levels = np.minimum(levels, fit + 3.0)
    return np.interp(np.arange(n), centres, levels)


def detect(db: np.ndarray, start_hz: float, bin_hz: float,
           min_snr_db: float = config.SIGNAL_MIN_SNR_DB,
           edge_db: float = config.SIGNAL_EDGE_DB) -> list[Signal]:
    """Signals in *db* (bin i is centred on ``start_hz + (i + 0.5) * bin_hz``), strongest first."""
    db = np.asarray(db, dtype=np.float64)
    if len(db) < 8:
        return []
    floor = noise_floor(db)
    above = db > floor + edge_db
    # Runs of bins above the edge level.
    padded = np.concatenate(([False], above, [False]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    starts, stops = changes[::2], changes[1::2]
    signals: list[Signal] = []
    for a, b in zip(starts, stops):
        seg = db[a:b]
        k = int(np.argmax(seg))
        snr = float(seg[k] - floor[a + k])
        if snr < min_snr_db:
            continue
        i = a + k
        power = _excess_power(db, floor, a, b)
        signals.append(Signal(
            lo_hz=start_hz + a * bin_hz,
            hi_hz=start_hz + b * bin_hz,
            peak_hz=start_hz + (i + 0.5 + _parabolic_offset(db, i)) * bin_hz,
            centroid_hz=_centroid(power, a, start_hz, bin_hz),
            peak_db=float(seg[k]),
            snr_db=snr,
            occupied_hz=_occupied(power, bin_hz),
        ))
    signals.sort(key=lambda s: s.snr_db, reverse=True)
    return signals


def _parabolic_offset(db: np.ndarray, i: int) -> float:
    """Sub-bin position of the peak at bin *i* from a parabola through its neighbours."""
    if i <= 0 or i >= len(db) - 1:
        return 0.0
    a, b, c = db[i - 1], db[i], db[i + 1]
    denom = a - 2.0 * b + c
    return float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5)) if denom < 0 else 0.0


def _excess_power(db: np.ndarray, floor: np.ndarray, a: int, b: int) -> np.ndarray:
    """Linear power above the noise floor for bins a..b-1."""
    return np.maximum(10.0 ** (db[a:b] / 10.0) - 10.0 ** (floor[a:b] / 10.0), 0.0)


def _centroid(power: np.ndarray, a: int, start_hz: float, bin_hz: float) -> float:
    freqs = start_hz + (np.arange(a, a + len(power)) + 0.5) * bin_hz
    total = float(power.sum())
    return float((power * freqs).sum() / total) if total > 0 else float(freqs.mean())


def _occupied(power: np.ndarray, bin_hz: float, fraction: float = 0.99) -> float:
    """Width between the 0.5 % and 99.5 % points of the cumulative power."""
    total = float(power.sum())
    if total <= 0:
        return len(power) * bin_hz
    cum = np.cumsum(power) / total
    tail = (1.0 - fraction) / 2.0
    lo = int(np.searchsorted(cum, tail))
    hi = int(np.searchsorted(cum, 1.0 - tail))
    return (hi - lo + 1) * bin_hz


def tune_target(sig: Signal, mode: str, step_hz: int, bin_hz: float) -> float | None:
    """VFO frequency that puts *sig* in the passband for *mode*, or None if the view is too coarse."""
    if mode in ("USB", "LSB"):
        if bin_hz > config.SSB_SNAP_MAX_BIN_HZ:
            return None
        # Voice starts ~300 Hz from the suppressed carrier; the measured edge is about there.
        hz = sig.lo_hz - config.SSB_LOW_CUT_HZ if mode == "USB" else sig.hi_hz + config.SSB_LOW_CUT_HZ
        return round(hz / 10.0) * 10.0
    if mode == "CW":
        return round(sig.peak_hz / 10.0) * 10.0     # the carrier; the demod adds the pitch
    # AM: the carrier is the peak. FM: the centre of the occupied band.
    hz = sig.peak_hz if mode == "AM" else sig.centroid_hz
    on_grid = round(hz / step_hz) * step_hz
    if abs(on_grid - hz) <= max(bin_hz, sig.width_hz / 8.0):
        return float(on_grid)
    return round(hz / 10.0) * 10.0


def fit_width(sig: Signal, mode: str, bin_hz: float) -> float | None:
    """Occupied bandwidth to fit the filter to, or None when it can't be measured well enough."""
    if mode == "WFM" or sig.occupied_hz < 6 * bin_hz:
        return None
    return sig.occupied_hz


def signal_at(signals: list[Signal], hz: float, tolerance_hz: float) -> Signal | None:
    """The signal under (or within *tolerance_hz* of) *hz*; the closest one if several."""
    near = [s for s in signals if s.lo_hz - tolerance_hz <= hz <= s.hi_hz + tolerance_hz]
    if not near:
        return None
    return min(near, key=lambda s: 0.0 if s.lo_hz <= hz <= s.hi_hz else
               min(abs(hz - s.lo_hz), abs(hz - s.hi_hz)))


def seek(signals: list[Signal], vfo_hz: float, direction: int, mode: str, step_hz: int,
         bin_hz: float) -> tuple[Signal, float] | None:
    """Next signal above (+1) or below (-1) the VFO and the frequency to tune it."""
    guard = max(2.0 * bin_hz, 50.0)
    best: tuple[Signal, float] | None = None
    for sig in signals:
        target = tune_target(sig, mode, step_hz, bin_hz)
        if target is None:
            target = sig.centroid_hz
        if sig.lo_hz <= vfo_hz <= sig.hi_hz and abs(target - vfo_hz) < sig.width_hz / 2:
            continue      # the one we're on
        if direction > 0 and target > vfo_hz + guard:
            if best is None or target < best[1]:
                best = (sig, target)
        elif direction < 0 and target < vfo_hz - guard:
            if best is None or target > best[1]:
                best = (sig, target)
    return best
