"""US ZIP code -> location, from a table shipped with the program (no network needed).

``config/zipcodes.npz`` is made by ``tools/build_zipcodes.py`` from the GeoNames postal code
data (geonames.org, CC BY 4.0). A ZIP code's position is the middle of the area it serves,
so it is right to within a few miles: plenty for the weather, close enough for the ADS-B
map, and ``wx_lat`` / ``wx_lon`` in settings.json remain for an exact position.

Arrays in the file, one entry per ZIP code, sorted by ZIP code:
    zip        uint32
    lat, lon   float32   degrees
    names      uint8     "Town ST" for each entry as UTF-8, joined with newlines
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

_ZIP = re.compile(r"^[0-9]{5}$")
_MATCH_DEG = 0.001           # a stored location this close to a ZIP code's is that ZIP code
_cache: dict[str, dict | None] = {}


def load(path: Path | None = None) -> dict | None:
    """The table (read once), or None if there is no usable file."""
    path = Path(path or config.ZIPCODES_FILE)
    key = str(path)
    if key not in _cache:
        _cache[key] = _read(path)
    return _cache[key]


def _read(path: Path) -> dict | None:
    try:
        with np.load(path) as data:
            table = {name: data[name] for name in ("zip", "lat", "lon")}
            names = bytes(data["names"]).decode("utf-8").split("\n")
    except FileNotFoundError:
        log.info("no ZIP code table (%s is missing)", path)
        return None
    except (OSError, KeyError, ValueError) as exc:
        log.warning("ZIP code table unreadable (%s): %s", path, exc)
        return None
    count = len(table["zip"])
    if not (len(table["lat"]) == len(table["lon"]) == len(names) == count) or count == 0:
        log.warning("ZIP code table unreadable (%s): arrays do not fit together", path)
        return None
    table["names"] = names
    return table


def available(path: Path | None = None) -> bool:
    return load(path) is not None


def lookup(zip_code, path: Path | None = None) -> tuple[float, float, str] | None:
    """(latitude, longitude, "Town ST") for a 5-digit ZIP code, or None if it is not in the table."""
    table = load(path)
    if table is None or not isinstance(zip_code, str) or not _ZIP.match(zip_code):
        return None
    number = int(zip_code)
    index = int(np.searchsorted(table["zip"], number))
    if index >= len(table["zip"]) or int(table["zip"][index]) != number:
        return None
    return round(float(table["lat"][index]), 4), round(float(table["lon"][index]), 4), table["names"][index]


def coordinates_text(lat: float, lon: float) -> str:
    return f"{abs(lat):.2f}\u00b0{'N' if lat >= 0 else 'S'} {abs(lon):.2f}\u00b0{'E' if lon >= 0 else 'W'}"


def place(state) -> str:
    """ "Town ST" if the location in the settings is the one its ZIP code gives, else "".

    Checked against the table rather than trusted, so a location edited by hand in
    settings.json is never shown under the name of the ZIP code that was there before.
    """
    if state.wx_lat is None or state.wx_lon is None:
        return ""
    found = lookup(state.wx_zip)
    if found is None or abs(found[0] - state.wx_lat) > _MATCH_DEG or abs(found[1] - state.wx_lon) > _MATCH_DEG:
        return ""
    return found[2]


def label(state) -> str:
    """The location in the settings in a few words, for the Settings row."""
    if state.wx_lat is None or state.wx_lon is None:
        return "AUTO (IP address)"
    town = place(state)
    return f"{state.wx_zip} {town}" if town else coordinates_text(state.wx_lat, state.wx_lon)
