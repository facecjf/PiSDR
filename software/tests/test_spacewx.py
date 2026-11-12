"""HF conditions: feed parsers, the cache, and the panel drawing whatever it is given."""
from __future__ import annotations

import json

import pytest

from conftest import pump
from pisdr import config, spacewx
from pisdr.spacewx import SpaceWx
from pisdr.ui.hf_panel import HfPanel

SOLAR_XML = """<?xml version="1.0"?>
<solar><solardata>
 <source url="http://www.hamqsl.com/solar.html">N0NBH</source>
 <updated> 15 Jan 2025 1200 GMT</updated>
 <solarflux>168</solarflux><aindex>7</aindex><kindex>2</kindex><kindexnt>No Report</kindexnt>
 <xray>C1.4</xray><sunspots>133</sunspots><heliumline>140.2</heliumline>
 <protonflux>3</protonflux><electonflux>1140</electonflux><aurora>2</aurora>
 <normalization>1.99</normalization><latdegree>67.5</latdegree>
 <solarwind>402.7</solarwind><magneticfield> -1.2</magneticfield>
 <calculatedconditions>
  <band name="80m-40m" time="day">Fair</band><band name="30m-20m" time="day">Good</band>
  <band name="17m-15m" time="day">Good</band><band name="12m-10m" time="day">Fair</band>
  <band name="80m-40m" time="night">Good</band><band name="30m-20m" time="night">Good</band>
  <band name="17m-15m" time="night">Fair</band><band name="12m-10m" time="night">Poor</band>
 </calculatedconditions>
 <geomagfield>VR QUIET</geomagfield><signalnoise>S1-S2</signalnoise><muf>21.3</muf>
</solardata></solar>"""


def test_solar_feed_is_parsed():
    data = spacewx.parse_solar_xml(SOLAR_XML)
    assert (data["sfi"], data["ssn"], data["a_index"], data["k_index"]) == (168, 133, 7, 2)
    assert data["bands"]["30m-20m"] == ["Good", "Good"] and data["bands"]["12m-10m"] == ["Fair", "Poor"]
    assert (data["geomag"], data["noise"], data["xray"]) == ("Very quiet", "S1-S2", "C1.4")
    assert data["solar_wind"] == pytest.approx(402.7) and data["bz"] == pytest.approx(-1.2)


def test_solar_feed_with_gaps_gives_blanks_not_errors():
    data = spacewx.parse_solar_xml("<solar><solardata><solarflux>n/a</solarflux></solardata></solar>")
    assert data["sfi"] is None and data["k_index"] is None
    assert data["bands"] == {name: ["--", "--"] for name in spacewx.BAND_GROUPS}
    for bad in ("<html>503</html>", "", "not xml"):
        with pytest.raises(Exception):                 # reported as a failed fetch, retried later
            spacewx.parse_solar_xml(bad)


def test_kp_feed_old_and_new_layouts():
    table = [["time_tag", "Kp", "a_running", "station_count"]] + [[f"2025-01-15 {h:02d}:00:00", str(k), "5", "8"]
                                                                  for h, k in zip(range(0, 24, 3), (1, 2, 3.33, 4, 5, 4, 3, 2))]
    assert spacewx.parse_kp_json(json.dumps(table)) == [1, 2, 3.33, 4, 5, 4, 3, 2]
    objects = [{"time_tag": "x", "Kp": k} for k in (1.0, None, "bad", 2.67)]
    assert spacewx.parse_kp_json(json.dumps(objects)) == [1.0, 2.67]
    assert spacewx.parse_kp_json("[]") == []


def test_weather_feed_is_parsed():
    payload = {"latitude": 43.4, "longitude": -88.2,
               "current_units": {"temperature_2m": "°F", "wind_speed_10m": "mp/h"},
               "current": {"temperature_2m": 21.4, "relative_humidity_2m": 71, "weather_code": 73,
                           "pressure_msl": 1021.3, "wind_speed_10m": 9.2, "wind_direction_10m": 300}}
    wx = spacewx.parse_open_meteo(json.dumps(payload), "West Bend")
    assert (wx["temp"], wx["temp_unit"], wx["wind_unit"], wx["desc"], wx["city"]) == (21.4, "°F", "mph", "Snow", "West Bend")
    assert spacewx.parse_open_meteo("{}")["desc"] == "--"
    assert [spacewx.compass(d) for d in (0, 44, 90, 200, 359, None)] == ["N", "NE", "E", "S", "N", ""]


def test_cache_round_trip_and_damage():
    data = SpaceWx(**spacewx.parse_solar_xml(SOLAR_XML), kp_history=[1.0, 2.0], solar_at=1.7e9)
    spacewx.save_cached(data)
    assert spacewx.load_cached() == data
    for text in ("", "{", "[1]", "null", '{"sfi": 1, "added_in_a_newer_version": 2}'):
        config.SPACEWX_FILE.write_text(text)
        assert isinstance(spacewx.load_cached(), SpaceWx)
    assert spacewx.load_cached().sfi == 1


GARBAGE_CACHES = [
    {"bands": 5, "kp_history": "high", "wx": [1, 2]},
    {"bands": {"30m-20m": "Good", "80m-40m": [1], "x": None}, "kp_history": [None, "3", 99, -4, float("nan")]},
    {"sfi": "168", "ssn": [], "k_index": 12.5, "a_index": {}, "solar_wind": "fast", "bz": None, "aurora": "x"},
    {"wx": {"temp": "warm", "humidity": None, "wind": [], "wind_dir": "N", "code": "rain", "pressure": {}}},
    {"solar_at": "yesterday", "kp_at": None, "wx_at": [], "solar_updated": 5, "xray": None, "muf": 7},
]


@pytest.mark.parametrize("cache", GARBAGE_CACHES + [None])
def test_panel_draws_whatever_is_in_the_cache(state, qapp, cache):
    """The cache is written by this program, but a bad one must not stop the radio starting."""
    if cache is not None:
        config.SPACEWX_FILE.write_text(json.dumps(cache))
    state.set_space_wx(spacewx.load_cached())
    panel = HfPanel(state)
    panel.resize(config.HF_PANEL_W, 320)
    panel.show()
    pump(qapp, 0.03)
    assert not panel.grab().isNull()
    for units in ("metric", "imperial"):
        state.wx_units = units
        state.changed.emit("wx_units")
        panel.grab()
    panel.close()


def test_panel_shows_real_data(state, qapp):
    wx = {"temp": 21.4, "temp_unit": "°F", "humidity": 71, "pressure": 1021.3, "wind": 9.2,
          "wind_unit": "mph", "wind_dir": 300, "code": 73, "desc": "Snow", "city": "West Bend"}
    state.set_space_wx(SpaceWx(**spacewx.parse_solar_xml(SOLAR_XML), kp_history=[1, 2, 3.3, 4, 5, 4, 3, 2],
                               wx=wx, solar_at=1.7e9, kp_at=1.7e9, wx_at=1.7e9))
    panel = HfPanel(state)
    panel.resize(config.HF_PANEL_W, 320)
    panel.show()
    pump(qapp, 0.03)
    image = panel.grab().toImage()
    colours = {image.pixel(x, y) for x in range(0, image.width(), 5) for y in range(0, image.height(), 5)}
    assert len(colours) > 12
    panel.close()


def test_fetcher_survives_having_no_network(state, qapp):
    fetcher = spacewx.SpaceWxFetcher(state)
    fetcher._tick()                                    # worker thread: every fetch raises OSError
    for _ in range(100):
        pump(qapp, 0.02)
        if not fetcher._busy:
            break
    assert not fetcher._busy
    assert all(when > 0 for when in fetcher._next_try.values()), "failed feeds were not scheduled for a retry"
    assert state.space_wx.sfi is None


def test_feed_sending_text_where_a_number_was_blanks_only_that_reading(state, qapp):
    fetcher = spacewx.SpaceWxFetcher(state)
    fetcher._on_result("wx", {"wx": {"temp": "21.4", "humidity": 71, "desc": "Snow", "wind_dir": None}}, "")
    fetcher._on_result("kp", {"kp_history": [1.0, "2", None, 3.0]}, "")
    wx = state.space_wx.wx
    assert wx["temp"] is None and wx["humidity"] == 71.0 and wx["desc"] == "Snow"
    assert state.space_wx.kp_history == [1.0, 3.0]
    panel = HfPanel(state)
    panel.grab()
