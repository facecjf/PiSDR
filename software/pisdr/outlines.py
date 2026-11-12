"""Coastlines, lakes and borders for the ADS-B map.

``config/outlines.npz`` holds Natural Earth (public domain) lines as integer hundredths of
a degree; ``tools/build_outlines.py`` makes it. The file is optional: without it the map
simply has no outlines.

Arrays in the file:
    lat, lon   int16   every point of every line, one after another
    start      int32   index of each line's first point, plus one final entry (= len(lat))
    kind       uint8   COAST / LAKE / BORDER / STATE for each line
    box        int16   south, west, north, east of each line
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np

from . import config
from .adsb import project

log = logging.getLogger(__name__)

COAST, LAKE, BORDER, STATE = range(4)
KIND_NAMES = ("coast", "lake", "border", "state")
SCALE = 100.0                # stored units per degree
NM_PER_DEGREE = 60.0

_cache: dict[str, dict | None] = {}


def load(path: Path | None = None) -> dict | None:
    """The arrays of the outline file (read once), or None if there is no usable file."""
    path = Path(path or config.OUTLINES_FILE)
    key = str(path)
    if key not in _cache:
        _cache[key] = _read(path)
    return _cache[key]


def _read(path: Path) -> dict | None:
    try:
        with np.load(path) as data:
            arrays = {name: data[name] for name in ("lat", "lon", "start", "kind", "box")}
    except FileNotFoundError:
        log.info("no map outlines (%s is missing)", path)
        return None
    except (OSError, KeyError, ValueError) as exc:
        log.warning("map outlines unreadable (%s): %s", path, exc)
        return None
    lines = len(arrays["kind"])
    if (len(arrays["lat"]) != len(arrays["lon"]) or len(arrays["start"]) != lines + 1
            or arrays["box"].shape != (lines, 4) or int(arrays["start"][-1]) != len(arrays["lat"])):
        log.warning("map outlines unreadable (%s): arrays do not fit together", path)
        return None
    return arrays


def near(lat0: float, lon0: float, radius_nm: float, path: Path | None = None
         ) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """Pieces of outline within *radius_nm* of a place.

    Returns ``(kind, east_nm, north_nm)`` runs of at least two points, relative to the place
    and in the same projection as the aircraft. Runs may poke a little outside the radius.
    """
    data = load(path)
    if data is None:
        return []
    reach = radius_nm / NM_PER_DEGREE
    reach_lon = min(180.0, reach / max(0.05, math.cos(math.radians(lat0))))
    box = data["box"].astype(np.float64) / SCALE
    in_lat = (box[:, 2] >= lat0 - reach) & (box[:, 0] <= lat0 + reach)
    in_lon = np.zeros(len(box), dtype=bool)
    for turn in (-360.0, 0.0, 360.0):              # the view may straddle the date line
        in_lon |= (box[:, 3] + turn >= lon0 - reach_lon) & (box[:, 1] + turn <= lon0 + reach_lon)

    runs: list[tuple[int, np.ndarray, np.ndarray]] = []
    start = data["start"]
    for index in np.flatnonzero(in_lat & in_lon):
        a, b = int(start[index]), int(start[index + 1])
        east, north = project(lat0, lon0, data["lat"][a:b] / SCALE, data["lon"][a:b] / SCALE)
        inside = np.hypot(east, north) <= radius_nm
        if not inside.any():
            continue
        # Keep one point either side of each inside stretch so lines reach the edge of the view.
        keep = inside.copy()
        keep[:-1] |= inside[1:]
        keep[1:] |= inside[:-1]
        edges = np.flatnonzero(np.diff(np.concatenate(([False], keep, [False])).astype(np.int8)))
        for first, last in zip(edges[::2], edges[1::2]):
            if last - first >= 2:
                runs.append((int(data["kind"][index]), east[first:last], north[first:last]))
    return runs
