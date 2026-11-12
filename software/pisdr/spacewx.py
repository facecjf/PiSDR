"""Solar-terrestrial indices and local weather for the HF conditions panel.

Three small feeds, all free and key-less:

* HamQSL (N0NBH) solar XML - SFI, sunspots, A/K, X-ray class, solar wind, Bz, geomagnetic
  field, noise floor and the HF band-condition table. Polled hourly.
* NOAA SWPC planetary K-index JSON - last 24 h of 3-hourly Kp for the bar strip.
* Open-Meteo current weather for the Pi's location (lat/lon from ``settings.json`` or, if
  unset, a one-off IP geolocation).

Fetching runs in a background thread; results come back on the Qt thread through a
signal and are cached to ``spacewx.json`` so the panel has data at boot even offline.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config, storage, zipcodes
from .qt import QObject, QTimer, Signal

log = logging.getLogger(__name__)

BAND_GROUPS = ("80m-40m", "30m-20m", "17m-15m", "12m-10m")

_WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
}


_GEOMAG = {"VR QUIET": "Very quiet", "UNSETTLD": "Unsettled", "MIN STORM": "Minor storm",
           "MOD STORM": "Mod. storm", "STR STORM": "Strong storm", "SEV STORM": "Severe storm",
           "EXT STORM": "Extreme storm", "NO REPORT": "--"}


def wmo_description(code: int | None) -> str:
    return _WMO.get(code if code is not None else -1, "--")


@dataclass
class SpaceWx:
    """Everything the panel shows. ``*_at`` are epoch seconds of the last good fetch (0 = never)."""

    sfi: int | None = None
    ssn: int | None = None
    a_index: int | None = None
    k_index: int | None = None
    xray: str = ""
    solar_wind: float | None = None
    bz: float | None = None
    geomag: str = ""
    noise: str = ""
    aurora: int | None = None
    muf: str = ""
    bands: dict[str, list[str]] = field(default_factory=dict)   # "30m-20m" -> ["Good", "Fair"]
    solar_updated: str = ""
    solar_at: float = 0.0
    kp_history: list[float] = field(default_factory=list)       # oldest first, 3-hourly
    kp_at: float = 0.0
    wx: dict[str, Any] = field(default_factory=dict)
    wx_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> "SpaceWx":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a space weather cache")
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known).cleaned()

    def cleaned(self) -> "SpaceWx":
        """Force every field to the type the panel formats it as.

        The values come from three web services and a cache file. A feed that starts sending
        a string where a number used to be must blank one reading, not stop the radio.
        """
        for name in ("sfi", "ssn", "a_index", "k_index", "aurora"):
            setattr(self, name, _number(getattr(self, name), int))
        for name in ("solar_wind", "bz"):
            setattr(self, name, _number(getattr(self, name)))
        for name in ("xray", "geomag", "noise", "muf", "solar_updated"):
            value = getattr(self, name)
            setattr(self, name, value[:40] if isinstance(value, str) else "")
        for name in ("solar_at", "kp_at", "wx_at"):
            setattr(self, name, _number(getattr(self, name)) or 0.0)
        bands = self.bands if isinstance(self.bands, dict) else {}
        self.bands = {name: [str(text)[:8] for text in pair] for name, pair in bands.items()
                      if name in BAND_GROUPS and isinstance(pair, (list, tuple)) and len(pair) == 2}
        history = self.kp_history if isinstance(self.kp_history, list) else []
        self.kp_history = [kp for kp in (_number(v) for v in history) if kp is not None and 0.0 <= kp <= 9.0]
        wx = self.wx if isinstance(self.wx, dict) else {}
        clean: dict[str, Any] = {}
        for key, value in wx.items():
            if key in ("temp", "humidity", "pressure", "wind", "wind_dir", "lat", "lon"):
                clean[key] = _number(value)
            elif key == "code":
                clean[key] = _number(value, int)
            elif key in ("temp_unit", "wind_unit", "desc", "city", "geo"):
                clean[key] = value[:40] if isinstance(value, str) else ""
        self.wx = clean
        return self


def _number(value, cast=float):
    """*value* as a finite number, or None for anything else (strings, lists, NaN, booleans)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or abs(value) > 1e12:
        return None
    return cast(value)


# --------------------------------------------------------------------------- parsers
def _num(text: str | None, cast=float):
    try:
        return cast(float((text or "").strip()))
    except ValueError:
        return None


def parse_solar_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    sd = root.find("solardata")
    if sd is None:
        raise ValueError("no <solardata>")

    def txt(tag: str) -> str:
        el = sd.find(tag)
        return (el.text or "").strip() if el is not None else ""

    bands: dict[str, list[str]] = {name: ["--", "--"] for name in BAND_GROUPS}
    for band in sd.iterfind("calculatedconditions/band"):
        name = band.get("name", "")
        if name in bands:
            bands[name][0 if band.get("time") == "day" else 1] = (band.text or "--").strip()
    return {
        "sfi": _num(txt("solarflux"), int),
        "ssn": _num(txt("sunspots"), int),
        "a_index": _num(txt("aindex"), int),
        "k_index": _num(txt("kindex"), int),
        "xray": txt("xray"),
        "solar_wind": _num(txt("solarwind")),
        "bz": _num(txt("magneticfield")),
        "geomag": _GEOMAG.get(txt("geomagfield").upper(), txt("geomagfield").title()),
        "noise": txt("signalnoise"),
        "aurora": _num(txt("aurora"), int),
        "muf": txt("muf"),
        "bands": bands,
        "solar_updated": txt("updated"),
    }


def parse_kp_json(text: str, hours: int = 24) -> list[float]:
    rows = json.loads(text)
    values: list[float] = []
    for row in rows:
        if isinstance(row, dict):
            kp = row.get("Kp")
        elif isinstance(row, list) and len(row) > 1 and row[0] != "time_tag":
            kp = row[1]
        else:
            continue
        try:
            values.append(float(kp))
        except (TypeError, ValueError):
            continue
    return values[-(hours // 3):]


def parse_open_meteo(text: str, city: str = "") -> dict[str, Any]:
    data = json.loads(text)
    cur = data.get("current", {})
    units = data.get("current_units", {})
    code = cur.get("weather_code")
    return {
        "temp": cur.get("temperature_2m"),
        "temp_unit": units.get("temperature_2m", "°"),
        "humidity": cur.get("relative_humidity_2m"),
        "pressure": cur.get("pressure_msl"),
        "wind": cur.get("wind_speed_10m"),
        "wind_unit": {"mp/h": "mph", "km/h": "km/h"}.get(units.get("wind_speed_10m", ""), units.get("wind_speed_10m", "")),
        "wind_dir": cur.get("wind_direction_10m"),
        "code": code,
        "desc": wmo_description(code),
        "city": city,
        "lat": data.get("latitude"),
        "lon": data.get("longitude"),
    }


def compass(deg: float | None) -> str:
    if deg is None:
        return ""
    names = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return names[int((deg + 22.5) // 45) % 8]


# --------------------------------------------------------------------------- fetching
def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": config.SPACEWX_USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.SPACEWX_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_cached(path=None) -> SpaceWx:
    try:
        with open(path or config.SPACEWX_FILE, "r", encoding="utf-8") as fh:
            return SpaceWx.from_json(fh.read())
    except (OSError, ValueError, TypeError, AttributeError):
        return SpaceWx()


def save_cached(data: SpaceWx, path=None) -> None:
    try:
        storage.write_text_atomic(path or config.SPACEWX_FILE, data.to_json(), durable=False)
    except OSError as exc:
        log.debug("Could not cache space weather: %s", exc)


class SpaceWxFetcher(QObject):
    """Polls the feeds on their own schedules and pushes a :class:`SpaceWx` into the state.

    One worker thread at a time; failures back off for :data:`config.SPACEWX_RETRY_S`.
    """

    _result = Signal(str, object, str)   # key, payload-or-None, error text
    _finished = Signal()

    def __init__(self, state, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.data = load_cached()
        self._busy = False
        self._next_try = {key: 0.0 for key in config.SPACEWX_PERIOD_S}
        self._result.connect(self._on_result)
        self._finished.connect(self._on_finished)
        self._timer = QTimer(self)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self._tick)
        self._moved = False              # the location changed while a fetch was under way
        state.changed.connect(self._on_state_changed)
        state.set_space_wx(self.data)

    def start(self) -> None:
        self._timer.start()
        QTimer.singleShot(1500, self._tick)

    def stop(self) -> None:
        self._timer.stop()

    def refresh_now(self) -> None:
        self._next_try = {key: 0.0 for key in self._next_try}
        self._tick()

    def _on_state_changed(self, field: str) -> None:
        if field == "location":                    # the weather is for somewhere else now
            self._next_try["wx"] = 0.0
            self._moved = self._busy
            if self._timer.isActive():
                self._tick()

    # ------------------------------------------------------------------ scheduling
    def _tick(self) -> None:
        if self._busy:
            return
        now = time.time()
        due = [key for key, when in self._next_try.items() if now >= when]
        if not due:
            return
        self._busy = True
        lat, lon = self.state.wx_lat, self.state.wx_lon
        units = self.state.wx_units
        town = zipcodes.place(self.state)
        threading.Thread(target=self._work, args=(due, lat, lon, units, town), daemon=True,
                         name="spacewx").start()

    def _work(self, keys: list[str], lat, lon, units: str, town: str = "") -> None:
        for key in keys:
            try:
                if key == "solar":
                    payload = parse_solar_xml(fetch_text(config.SPACEWX_SOLAR_URL))
                elif key == "kp":
                    payload = {"kp_history": parse_kp_json(fetch_text(config.SPACEWX_KP_URL))}
                else:
                    payload = {"wx": self._fetch_weather(lat, lon, units, town)}
                self._result.emit(key, payload, "")
            except Exception as exc:  # network down, DNS, bad payload - never crash the radio
                self._result.emit(key, None, f"{type(exc).__name__}: {exc}")
        self._finished.emit()

    def _fetch_weather(self, lat, lon, units: str, town: str = "") -> dict[str, Any]:
        city = self.data.wx.get("city", "") if self.data.wx else ""
        if lat is None or lon is None:
            cached = self.data.wx or {}
            if cached.get("lat") is not None and cached.get("geo") == "ip":
                lat, lon = cached["lat"], cached["lon"]
            else:
                geo = json.loads(fetch_text(config.SPACEWX_GEOIP_URL))
                if geo.get("status") != "success":
                    raise RuntimeError("IP geolocation failed")
                lat, lon, city = geo["lat"], geo["lon"], geo.get("city", "")
            source = "ip"
        else:
            source = "settings"
            city = town
        imperial = units == "imperial"
        url = config.SPACEWX_WX_URL.format(
            lat=lat, lon=lon,
            temp_unit="fahrenheit" if imperial else "celsius",
            wind_unit="mph" if imperial else "kmh")
        wx = parse_open_meteo(fetch_text(url), city)
        wx["lat"], wx["lon"], wx["geo"] = lat, lon, source
        return wx

    # ------------------------------------------------------------------ results (Qt thread)
    def _on_result(self, key: str, payload, error: str) -> None:
        now = time.time()
        if payload is None:
            self._next_try[key] = now + config.SPACEWX_RETRY_S
            log.info("Space weather %s fetch failed: %s", key, error)
            return
        for name, value in payload.items():
            setattr(self.data, name, value)
        setattr(self.data, f"{key}_at", now)
        self.data.cleaned()
        self._next_try[key] = now + config.SPACEWX_PERIOD_S[key]
        save_cached(self.data)
        self.state.set_space_wx(self.data)

    def _on_finished(self) -> None:
        self._busy = False
        if self._moved:                            # what just arrived was for the old location
            self._moved = False
            self._next_try["wx"] = 0.0
            if self._timer.isActive():
                self._tick()
