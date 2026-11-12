"""ADS-B aircraft: reading a decoder's ``aircraft.json``, map geometry and simulated traffic.

The RTL-SDR cannot demodulate 1090 MHz Mode S and the voice receiver at the same time, so in
ADS-B mode the dongle is lent to a dump1090-family decoder (see :mod:`pisdr.radio.adsb_decoder`).
That program does the radio work and writes the list of aircraft it is tracking to a JSON
file once a second; this module turns that file into :class:`Aircraft` objects and does the
arithmetic the map needs. Nothing here touches Qt or the radio.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass

import numpy as np

from . import config

EARTH_RADIUS_NM = 3440.065
_HEX = re.compile(r"^~?[0-9a-fA-F]{6}$")
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass
class Aircraft:
    """One aircraft as the decoder last reported it. Anything not received yet is None / empty."""

    icao: str                              # 24-bit address, 6 hex digits ("~" in front: not an ICAO address)
    callsign: str = ""
    lat: float | None = None
    lon: float | None = None
    altitude_ft: int | None = None
    on_ground: bool = False
    speed_kt: float | None = None
    track_deg: float | None = None
    vert_rate_fpm: int | None = None
    squawk: str = ""
    seen_s: float = 0.0                    # seconds since any message from it
    seen_pos_s: float = 0.0                # seconds since its last position
    rssi_db: float | None = None
    messages: int = 0
    distance_nm: float | None = None       # from the map centre; filled in by locate()
    bearing_deg: float | None = None

    @property
    def name(self) -> str:
        return self.callsign or self.icao

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


# --------------------------------------------------------------------------- aircraft.json
def _number(value, lo: float, hi: float) -> float | None:
    """*value* as a finite number inside lo..hi, else None (strings, booleans, NaN, lists)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if lo <= value <= hi else None      # NaN fails both comparisons


def _first(item: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in item:
            return item[key]
    return None


def _aircraft(item) -> Aircraft | None:
    if not isinstance(item, dict) or not isinstance(item.get("hex"), str) or not _HEX.match(item["hex"]):
        return None
    seen = _number(item.get("seen", 0.0), 0.0, 1e7)
    if seen is None or seen > config.ADSB_MAX_AGE_S:
        return None
    ac = Aircraft(icao=item["hex"].upper(), seen_s=seen)
    flight = item.get("flight")
    if isinstance(flight, str):
        ac.callsign = re.sub(r"[^A-Z0-9]", "", flight.upper())[:8]
    lat, lon = _number(item.get("lat"), -90.0, 90.0), _number(item.get("lon"), -180.0, 180.0)
    seen_pos = _number(item.get("seen_pos", 0.0), 0.0, 1e7)
    if lat is not None and lon is not None and seen_pos is not None and seen_pos <= config.ADSB_POSITION_MAX_AGE_S:
        ac.lat, ac.lon, ac.seen_pos_s = lat, lon, seen_pos
    # dump1090-fa / readsb say alt_baro, gs, baro_rate; dump1090-mutability says altitude, speed, vert_rate.
    altitude = _first(item, ("alt_baro", "altitude", "alt_geom"))
    if altitude == "ground":
        ac.on_ground = True
    else:
        altitude = _number(altitude, -2000.0, 120_000.0)
        ac.altitude_ft = None if altitude is None else int(round(altitude))
    ac.speed_kt = _number(_first(item, ("gs", "speed")), 0.0, 5000.0)
    track = _number(item.get("track"), 0.0, 360.0)
    ac.track_deg = None if track is None else track % 360.0
    rate = _number(_first(item, ("baro_rate", "vert_rate", "geom_rate")), -60_000.0, 60_000.0)
    ac.vert_rate_fpm = None if rate is None else int(round(rate))
    squawk = item.get("squawk")
    if isinstance(squawk, str) and re.fullmatch(r"[0-7]{4}", squawk):
        ac.squawk = squawk
    ac.rssi_db = _number(item.get("rssi"), -200.0, 50.0)
    ac.messages = int(_number(item.get("messages"), 0, 1e12) or 0)
    return ac


def parse_aircraft_json(text: str) -> tuple[list[Aircraft], int]:
    """Aircraft heard in the last minute, and the decoder's running message count.

    Raises ValueError if *text* is not an ``aircraft.json``; damaged entries inside a good
    file are skipped one by one.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("aircraft"), list):
        raise ValueError("not an aircraft.json")
    aircraft = [ac for ac in map(_aircraft, data["aircraft"]) if ac is not None]
    return aircraft, int(_number(data.get("messages"), 0, 1e15) or 0)


# --------------------------------------------------------------------------- geometry
def project(lat0: float, lon0: float, lat, lon):
    """(east, north) in nautical miles of *lat*, *lon* as seen from *lat0*, *lon0*.

    Azimuthal equidistant: distance and bearing from the centre are exact, which is what
    range rings need. Works on numbers and on numpy arrays.
    """
    phi0, phi = math.radians(lat0), np.radians(lat)
    dlam = np.radians(np.asarray(lon, dtype=float) - lon0)
    cos_c = np.clip(math.sin(phi0) * np.sin(phi) + math.cos(phi0) * np.cos(phi) * np.cos(dlam), -1.0, 1.0)
    c = np.arccos(cos_c)
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where(c > 1e-9, c / np.sin(c), 1.0)
    east = EARTH_RADIUS_NM * k * np.cos(phi) * np.sin(dlam)
    north = EARTH_RADIUS_NM * k * (math.cos(phi0) * np.sin(phi) - math.sin(phi0) * np.cos(phi) * np.cos(dlam))
    return east, north


def distance_bearing(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Great-circle distance (nm) and initial bearing (degrees true) from the first point to the second."""
    east, north = project(lat0, lon0, lat, lon)
    return float(np.hypot(east, north)), float(np.degrees(np.arctan2(east, north)) % 360.0)


def compass_point(bearing_deg: float) -> str:
    return _COMPASS[int((bearing_deg + 22.5) // 45) % 8]


def locate(aircraft: list[Aircraft], lat0: float, lon0: float) -> None:
    """Fill in each aircraft's distance and bearing from the map centre."""
    for ac in aircraft:
        if ac.has_position:
            ac.distance_nm, ac.bearing_deg = distance_bearing(lat0, lon0, ac.lat, ac.lon)
        else:
            ac.distance_nm = ac.bearing_deg = None


def by_distance(aircraft: list[Aircraft]) -> list[Aircraft]:
    """Nearest first; aircraft with no position yet go last, by name."""
    return sorted(aircraft, key=lambda ac: (ac.distance_nm is None, ac.distance_nm or 0.0, ac.name))


def middle_of(aircraft: list[Aircraft]) -> tuple[float, float] | None:
    """A stand-in for the receiver's location when it is not known: the middle of the traffic."""
    placed = [ac for ac in aircraft if ac.has_position]
    if not placed:
        return None
    lat = float(np.median([ac.lat for ac in placed]))
    # Average longitudes as directions so traffic either side of 180 degrees does not land on 0.
    angles = np.radians([ac.lon for ac in placed])
    lon = math.degrees(math.atan2(float(np.sin(angles).mean()), float(np.cos(angles).mean())))
    return lat, lon


def receiver_position(state) -> tuple[float, float, str] | None:
    """Where the radio is: (lat, lon, "settings") if a location was entered (Settings > SYSTEM >
    Location, or ``wx_lat`` / ``wx_lon`` in settings.json), else (lat, lon, "ip") from the
    weather feed's IP geolocation (city-level), else None."""
    if state.wx_lat is not None and state.wx_lon is not None:
        return float(state.wx_lat), float(state.wx_lon), "settings"
    wx = getattr(state.space_wx, "wx", None) or {}
    if wx.get("geo") == "settings":
        return None            # weather fetched for an entered location that has since been cleared
    lat, lon = _number(wx.get("lat"), -90.0, 90.0), _number(wx.get("lon"), -180.0, 180.0)
    if lat is not None and lon is not None:
        return lat, lon, "ip"
    return None


def is_adsb_band(band) -> bool:
    """True for a band-table entry that is the 1090 MHz ADS-B channel (not a wide band across it)."""
    return band.contains(config.ADSB_FREQ_HZ) and band.stop_hz - band.start_hz <= 10_000_000


# --------------------------------------------------------------------------- simulator
class SimTraffic:
    """Made-up aircraft flying straight lines past a point, for the simulator and screenshots."""

    _FLIGHTS = (
        # callsign, closest approach (bearing deg, nm), track deg, speed kt, altitude ft, climb fpm
        ("UAL1482", 20, 18, 275, 452, 36000, 0),
        ("DAL2210", 200, 42, 95, 438, 34000, 0),
        ("SWA318", 140, 9, 12, 262, 9800, -1216),
        ("AAL77", 310, 64, 152, 471, 38000, 0),
        ("N512PS", 250, 5, 330, 118, 3500, 320),
        ("FDX1403", 80, 95, 215, 486, 31000, -640),
        ("SKW5521", 350, 30, 190, 301, 14500, 1856),
        ("ACA846", 110, 130, 301, 463, 40000, 0),
        ("ENY3655", 280, 22, 60, 228, 7200, -960),
        ("", 45, 55, 240, 410, 28000, 0),            # callsign not received yet
    )
    _LEG_NM = 360.0                                   # each one loops along a line this long

    def __init__(self, lat: float, lon: float, now: float | None = None) -> None:
        self.lat, self.lon = lat, lon
        self._t0 = time.monotonic() if now is None else now

    def aircraft(self, now: float | None = None) -> tuple[list[Aircraft], int]:
        """The traffic at time *now* (``time.monotonic()``) and a plausible message count."""
        t = (time.monotonic() if now is None else now) - self._t0
        out: list[Aircraft] = []
        for i, (callsign, bearing, closest, track, speed, altitude, climb) in enumerate(self._FLIGHTS):
            along = ((i * 61.0 + speed * t / 3600.0 + self._LEG_NM / 2) % self._LEG_NM) - self._LEG_NM / 2
            east = closest * math.sin(math.radians(bearing)) + along * math.sin(math.radians(track))
            north = closest * math.cos(math.radians(bearing)) + along * math.cos(math.radians(track))
            out.append(Aircraft(
                icao=f"{0xA00000 + i * 0x1F3B7:06X}", callsign=callsign,
                lat=self.lat + north / 60.0,
                lon=self.lon + east / (60.0 * max(0.05, math.cos(math.radians(self.lat)))),
                altitude_ft=altitude, speed_kt=float(speed), track_deg=float(track),
                vert_rate_fpm=climb, squawk=f"{(1200 + i * 0o1421) % 0o10000:04o}",
                seen_s=(i * 0.37) % 1.0, seen_pos_s=(i * 0.53) % 2.0,
                rssi_db=-8.0 - 0.15 * math.hypot(east, north), messages=int(40 + t * (3 + i % 4))))
        out.append(Aircraft(icao="A7C0DE", callsign="N731PI", speed_kt=96.0, track_deg=88.0,
                            altitude_ft=2400, seen_s=1.4, messages=int(3 + t / 4)))     # no position yet
        out.append(Aircraft(icao="AB12F4", callsign="EJA612", lat=self.lat - 0.094, lon=self.lon + 0.021,
                            on_ground=True, speed_kt=14.0, track_deg=255.0, seen_s=0.6,
                            seen_pos_s=1.1, messages=int(20 + t)))
        return out, int(900 + 85 * t)
