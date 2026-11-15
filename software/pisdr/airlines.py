"""Who is flying: the operator of an aircraft, worked out from its callsign.

An airline's callsign is its three-letter ICAO designator followed by the flight number
("UAL1482"); ``software/config/airlines.json`` names the common ones and
``~/.config/pisdr/airlines.json`` (same form) adds your own or renames them. Light aircraft
usually send their registration instead ("N512PS").
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

UNKNOWN = "Unknown"
PRIVATE_US = "Private (US reg.)"
PRIVATE = "Private / local"

_FLIGHT = re.compile(r"^([A-Z]{3})\d[A-Z0-9]{0,4}$")
_US_REGISTRATION = re.compile(r"^N[1-9](\d{0,4}|\d{0,3}[A-Z]|\d{0,2}[A-Z]{2})$")

_tables: dict[tuple, dict[str, str]] = {}


def _read(path: Path) -> dict[str, str]:
    """The operators in one file; nothing (and a line in the log) if it cannot be used."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Ignoring %s: %s", path, exc)
        return {}
    operators = data.get("operators") if isinstance(data, dict) else None
    if not isinstance(operators, dict):
        log.warning("Ignoring %s: no \"operators\" table", path)
        return {}
    return {code.upper(): name.strip() for code, name in operators.items()
            if isinstance(code, str) and isinstance(name, str) and re.fullmatch(r"[A-Za-z]{3}", code) and name.strip()}


def table() -> dict[str, str]:
    """Designator -> operator: the built-in list with the user's file on top. Read once per pair of files."""
    paths = (config.AIRLINES_FILE, config.USER_AIRLINES_FILE)
    if paths not in _tables:
        _tables[paths] = {**_read(paths[0]), **_read(paths[1])}
    return _tables[paths]


def reload() -> None:
    """Forget what was read, so an edited file is picked up (the map does this when it opens)."""
    _tables.clear()


def operator(callsign: str) -> str:
    """Operator for a callsign: an airline, a kind of private flight, "Unknown", or "" for no callsign."""
    if not callsign:
        return ""
    if _US_REGISTRATION.match(callsign):
        return PRIVATE_US
    flight = _FLIGHT.match(callsign)
    if flight:
        return table().get(flight.group(1), UNKNOWN)
    return PRIVATE
