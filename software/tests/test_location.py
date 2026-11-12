"""Entering the radio's location as a US ZIP code: the bundled table, the settings behind it,
the Location page, and what the weather and the ADS-B map then use."""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import pump
from pisdr import adsb, config, spacewx, zipcodes
from pisdr.qt import Qt, QtCore
from pisdr.state import RadioState
from test_state import assert_sane
from test_ui import image_stats, press, rows, tap

WEST_BEND = (43.4224, -88.1845)


# --------------------------------------------------------------------------- the table
@pytest.mark.parametrize("code, town, lat, lon", (
    ("53095", "West Bend WI", 43.42, -88.18),
    ("10001", "New York NY", 40.75, -74.00),
    ("00501", "Holtsville NY", 40.82, -73.05),          # lowest ZIP code: leading zeros matter
    ("99501", "Anchorage AK", 61.21, -149.88),
    ("96813", "Honolulu HI", 21.31, -157.86),
    ("00601", "Adjuntas PR", 18.16, -66.72),
    ("96910", "Hagatna GU", 13.45, 144.75),
))
def test_bundled_table_knows_where_zip_codes_are(code, town, lat, lon):
    found = zipcodes.lookup(code)
    assert found is not None, "config/zipcodes.npz is missing or short - run tools/build_zipcodes.py"
    assert found[2] == town
    assert found[:2] == pytest.approx((lat, lon), abs=0.02)


def test_bundled_table_is_sorted_and_complete():
    table = zipcodes.load()
    assert zipcodes.available() and len(table["zip"]) > 40_000
    assert np.all(np.diff(table["zip"].astype(np.int64)) > 0), "lookup is a binary search"
    assert np.abs(table["lat"]).max() <= 90 and np.abs(table["lon"]).max() <= 180
    assert all(name and len(name) <= 40 for name in table["names"])


@pytest.mark.parametrize("code", ("99999", "00000", "09001", "5309", "530955", "53o95", " 53095", "", None, 53095,
                                  "\uff15\uff13\uff10\uff19\uff15"))
def test_anything_that_is_not_a_listed_zip_code_finds_nothing(code):
    assert zipcodes.lookup(code) is None


def test_missing_or_damaged_table_means_no_lookups(tmp_path):
    assert zipcodes.lookup("53095", path=tmp_path / "none.npz") is None
    assert not zipcodes.available(tmp_path / "none.npz")
    (tmp_path / "junk.npz").write_bytes(b"not a zip file")
    assert zipcodes.lookup("53095", path=tmp_path / "junk.npz") is None
    np.savez(tmp_path / "short.npz", zip=np.array([53095, 53096], np.uint32), lat=np.zeros(2, np.float32),
             lon=np.zeros(2, np.float32), names=np.frombuffer(b"Only One WI", np.uint8))
    assert zipcodes.lookup("53095", path=tmp_path / "short.npz") is None


def test_table_build_tool(tmp_path, capsys):
    tool = Path(__file__).resolve().parent.parent / "tools" / "build_zipcodes.py"
    spec = importlib.util.spec_from_file_location("build_zipcodes", tool)
    build_zipcodes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_zipcodes)

    def row(country, code, town, state, lat, lon):
        return "\t".join((country, code, town, "State name", state, "County", "001", "", "", lat, lon, "4"))
    source = tmp_path / "geonames"
    source.mkdir()
    (source / "US.txt").write_text("\n".join((
        row("US", "53095", "West  Bend", "WI", "43.4224", "-88.1845"),
        row("US", "53095", "Second Listing", "WI", "1.0", "1.0"),            # first row wins
        row("US", "00501", "Holtsville", "NY", "40.8154", "-73.0451"),
        row("US", "09001", "APO AA", "", "38.1105", "15.6613"),              # military mail: no state
        row("US", "5309", "Too Short", "WI", "43.0", "-88.0"),
        row("US", "53999", "Nowhere", "WI", "north", "-88.0"),
        row("US", "53998", "Off The Map", "WI", "91.0", "-88.0"),
        "a line that is not a row",
    )) + "\n", encoding="utf-8")
    (source / "PR.txt").write_text(row("PR", "00601", "Adjuntas", "001", "18.1627", "-66.7221") + "\n")
    out = tmp_path / "zip.npz"
    assert build_zipcodes.main(["--source", str(source), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "3 ZIP codes" in printed and "GU: not found, left out" in printed
    assert zipcodes.lookup("53095", path=out) == (43.4224, -88.1845, "West Bend WI")
    assert zipcodes.lookup("00501", path=out) == (40.8154, -73.0451, "Holtsville NY")
    assert zipcodes.lookup("00601", path=out) == (18.1627, -66.7221, "Adjuntas PR")
    for code in ("09001", "53999", "53998", "05309"):
        assert zipcodes.lookup(code, path=out) is None
    (source / "US.txt").unlink()
    with pytest.raises(SystemExit):
        build_zipcodes.main(["--source", str(source), "--out", str(out)])


# --------------------------------------------------------------------------- settings
def test_location_is_set_cleared_and_saved(state):
    changes: list[str] = []
    state.changed.connect(changes.append)
    assert zipcodes.label(state) == "AUTO (IP address)" and zipcodes.place(state) == ""
    assert state.set_location(*WEST_BEND, "53095")
    assert (state.wx_lat, state.wx_lon, state.wx_zip) == (*WEST_BEND, "53095") and changes == ["location"]
    assert zipcodes.label(state) == "53095 West Bend WI" and zipcodes.place(state) == "West Bend WI"
    state.save()
    again = RadioState()
    again.load()
    assert (again.wx_lat, again.wx_lon, again.wx_zip) == (*WEST_BEND, "53095")
    assert_sane(again)
    state.clear_location()
    assert (state.wx_lat, state.wx_lon, state.wx_zip) == (None, None, "") and changes == ["location"] * 2
    state.save()
    assert json.loads(config.SETTINGS_FILE.read_text())["wx_lat"] is None


def test_impossible_locations_are_refused(state):
    state.set_location(*WEST_BEND, "53095")
    for lat, lon in ((91.0, 0.0), (0.0, 181.0), ("43", -88.0), (None, None), (float("nan"), 0.0), (True, 1.0)):
        assert not state.set_location(lat, lon, "53095")
    assert (state.wx_lat, state.wx_lon) == WEST_BEND
    assert state.set_location(-33.9, 151.2, "not a zip") and state.wx_zip == ""
    assert zipcodes.label(state) == "33.90\u00b0S 151.20\u00b0E"


def test_location_edited_by_hand_is_not_shown_under_the_old_zip_code():
    config.SETTINGS_FILE.write_text(json.dumps({"wx_lat": 43.04, "wx_lon": -87.91, "wx_zip": "53095"}))
    state = RadioState()
    state.load()
    assert zipcodes.place(state) == "" and zipcodes.label(state) == "43.04\u00b0N 87.91\u00b0W"


@pytest.mark.parametrize("saved, expected", (
    ({"wx_zip": "53095"}, (None, None, "")),                                  # a ZIP code with no location
    ({"wx_lat": 43.4224, "wx_lon": -88.1845, "wx_zip": 53095}, (*WEST_BEND, "")),
    ({"wx_lat": 43.4224, "wx_lon": -88.1845, "wx_zip": "5309"}, (*WEST_BEND, "")),
    ({"wx_lat": 43.4224, "wx_lon": -88.1845, "wx_zip": ["53095"]}, (*WEST_BEND, "")),
    ({"wx_lat": 43.4224, "wx_zip": "53095"}, (None, None, "")),
))
def test_damaged_location_settings(saved, expected):
    config.SETTINGS_FILE.write_text(json.dumps(dict(saved, volume=37)))
    state = RadioState()
    state.load()
    assert (state.wx_lat, state.wx_lon, state.wx_zip) == expected and state.volume == 37
    assert_sane(state)


# --------------------------------------------------------------------------- who uses it
def test_adsb_map_centre_follows_the_location(state):
    state.space_wx = types.SimpleNamespace(wx={"lat": 43.0, "lon": -88.0, "geo": "ip"})
    assert adsb.receiver_position(state) == (43.0, -88.0, "ip")
    state.set_location(*WEST_BEND, "53095")
    assert adsb.receiver_position(state) == (*WEST_BEND, "settings")
    # Weather fetched for the entered place is still cached when the place is cleared: that is
    # not an IP-address guess and must not be passed off as one.
    state.space_wx = types.SimpleNamespace(wx={"lat": WEST_BEND[0], "lon": WEST_BEND[1], "geo": "settings"})
    state.clear_location()
    assert adsb.receiver_position(state) is None


OPEN_METEO = json.dumps({"latitude": 43.42, "longitude": -88.18,
                         "current_units": {"temperature_2m": "\u00b0F", "wind_speed_10m": "mp/h"},
                         "current": {"temperature_2m": 41.0, "relative_humidity_2m": 60, "weather_code": 3,
                                     "pressure_msl": 1014.0, "wind_speed_10m": 8.0, "wind_direction_10m": 270}})


def test_weather_is_fetched_for_the_entered_place(state, monkeypatch):
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        if "ip-api" in url:
            return json.dumps({"status": "success", "lat": 41.88, "lon": -87.63, "city": "Chicago"})
        return OPEN_METEO
    monkeypatch.setattr(spacewx, "fetch_text", fetch)
    fetcher = spacewx.SpaceWxFetcher(state)
    wx = fetcher._fetch_weather(*WEST_BEND, "imperial", "West Bend WI")
    assert len(asked) == 1 and "latitude=43.4224&longitude=-88.1845" in asked[0]
    assert (wx["city"], wx["geo"], wx["lat"], wx["lon"]) == ("West Bend WI", "settings", *WEST_BEND)
    # Cleared again: the cached weather was for the entered place, so the IP address is asked afresh.
    fetcher.data.wx = wx
    wx = fetcher._fetch_weather(None, None, "imperial")
    assert "ip-api" in asked[1] and "latitude=41.88&longitude=-87.63" in asked[2]
    assert (wx["city"], wx["geo"]) == ("Chicago", "ip")


def test_changing_the_location_makes_the_weather_due_at_once(state, qapp):
    fetcher = spacewx.SpaceWxFetcher(state)
    fetcher._next_try = {key: 1e12 for key in fetcher._next_try}
    state.set_location(*WEST_BEND, "53095")
    assert fetcher._next_try["wx"] == 0.0 and fetcher._next_try["solar"] == 1e12
    assert not fetcher._busy, "a fetcher that was never started must not start fetching"
    # A fetch already under way was for the old place: its result must not postpone the new one.
    fetcher._next_try["wx"] = 1e12
    fetcher._busy = True
    state.clear_location()
    fetcher._on_result("wx", {"wx": {"temp": 50.0}}, "")
    assert fetcher._next_try["wx"] > 0
    fetcher._on_finished()
    assert fetcher._next_try["wx"] == 0.0 and not fetcher._busy


# --------------------------------------------------------------------------- the page
def location_row(window) -> int:
    return next(i for i, r in rows(window) if r.label == "Location")


@pytest.fixture
def entry(window, qapp):
    """The Location page, opened the way a user gets there: Settings > SYSTEM > Location."""
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.selected = location_row(window)
    page.refresh()
    assert page.rows[page.selected].get().startswith("AUTO (IP address)")
    window.key_pressed(2)
    assert window.stack.currentIndex() == window.PAGE_LOCATION
    pump(qapp, 0.03)
    return window.location_page


def type_digits(page, digits: str) -> None:
    for digit in digits:
        page.key(digit)


def test_entering_a_zip_code_sets_the_location(window, qapp, entry):
    state = window.state
    assert entry.display.text() == "_____" and "IP address" in entry.current.text()
    assert [b.text() for b in window.softkeys.buttons] == ["AUTO", "", "", "BACK"]
    type_digits(entry, "5309")
    assert entry.display.text() == "5309_" and entry.match is None
    assert window.softkeys.buttons[2].text() == ""
    type_digits(entry, "5")
    assert entry.display.text() == "53095" and "West Bend WI" in entry.result.text()
    assert window.softkeys.buttons[2].text() == "SET"
    colours, _ = image_stats(window)
    assert colours > 20
    type_digits(entry, "7")                             # a sixth digit is ignored
    assert entry.display.text() == "53095"
    window.key_pressed(2)                              # SET
    assert (state.wx_lat, state.wx_lon, state.wx_zip) == (*WEST_BEND, "53095")
    assert window.stack.currentIndex() == window.PAGE_SETTINGS
    assert window.toast.text() == "Location: 53095 West Bend WI"
    row = window.settings_page.rows[location_row(window)]
    assert row.get().startswith("53095 West Bend WI")
    assert adsb.receiver_position(state) == (*WEST_BEND, "settings")
    state.save()
    assert json.loads(config.SETTINGS_FILE.read_text())["wx_zip"] == "53095"


def test_keypad_buttons_keyboard_and_corrections(window, qapp, entry):
    buttons = {b.text(): b for b in entry.findChildren(type(window.softkeys.buttons[0])) if b.objectName() == "keypad"}
    assert sorted(buttons) == sorted(list("0123456789") + ["CLR", "<-"])
    for text in "531":
        tap(buttons[text], QtCore.QPoint(10, 10))
    assert entry.display.text() == "531__"
    tap(buttons["<-"], QtCore.QPoint(10, 10))
    assert entry.display.text() == "53___"
    for key in (Qt.Key.Key_0, Qt.Key.Key_9, Qt.Key.Key_5):
        press(window, key)                              # PC keyboard digits go to this page, not the tuner
    assert window.stack.currentIndex() == window.PAGE_LOCATION and entry.display.text() == "53095"
    assert entry.match is not None
    tap(buttons["CLR"], QtCore.QPoint(10, 10))
    assert entry.display.text() == "_____" and entry.match is None
    assert window.softkeys.buttons[2].text() == ""
    vfo = window.state.vfo_hz
    window.knob_turned(3)                               # the knob does nothing here
    assert window.state.vfo_hz == vfo


def test_unknown_zip_code_cannot_be_set(window, qapp, entry):
    type_digits(entry, "99999")
    assert "No ZIP code 99999" in entry.result.text() and window.softkeys.buttons[2].text() == ""
    window.key_pressed(2)
    window.encoder_pushed()
    assert window.state.wx_lat is None and window.stack.currentIndex() == window.PAGE_LOCATION
    assert window.toast.text() == "Enter a 5-digit ZIP code first"
    window.key_pressed(3)                              # BACK changes nothing
    assert window.stack.currentIndex() == window.PAGE_SETTINGS and window.state.wx_lat is None


def test_knob_push_sets_and_auto_goes_back_to_the_ip_address(window, qapp, entry):
    state = window.state
    type_digits(entry, "10001")
    window.encoder_pushed()
    assert state.wx_zip == "10001" and window.stack.currentIndex() == window.PAGE_SETTINGS
    window.settings_page.selected = location_row(window)
    window.key_pressed(2)
    assert "10001 New York NY" in entry.current.text() and entry.display.text() == "_____"
    window.key_pressed(0)                              # AUTO
    assert (state.wx_lat, state.wx_lon, state.wx_zip) == (None, None, "")
    assert window.stack.currentIndex() == window.PAGE_SETTINGS
    assert window.settings_page.rows[location_row(window)].get().startswith("AUTO (IP address)")


def test_page_says_so_when_the_table_is_missing(window, qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ZIPCODES_FILE", tmp_path / "none.npz")
    window.show_page(window.PAGE_LOCATION)
    page = window.location_page
    type_digits(page, "53095")
    assert "zipcodes.npz" in page.result.text() and page.match is None
    window.key_pressed(2)
    assert window.state.wx_lat is None


def test_map_opened_after_entering_a_zip_code_is_centred_there(window, qapp, entry):
    type_digits(entry, "53095")
    window.key_pressed(2)
    window.show_main()
    window._enter_adsb()
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    radar = window.adsb_page.radar
    assert radar.centre == WEST_BEND and radar.centre_source == "settings"
    assert all(ac.distance_nm is not None for ac in window.adsb_page.order if ac.has_position)
    window.key_pressed(3)
