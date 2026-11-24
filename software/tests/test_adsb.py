"""ADS-B map: the aircraft.json reader, map geometry, outlines, the decoder process, the
dongle hand-over on both back ends, and the page itself.

The decoder tests use a small shell script standing in for dump1090. The last test runs a
real dump1090 on a synthetic recording (``adsb_signal.py``) when one is installed, or when
``PISDR_TEST_DUMP1090`` names one or more binaries (separated by ``:``).
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import stat
import time
import types
from pathlib import Path

import numpy as np
import pytest

import adsb_signal
from conftest import needs_fake_gr, pump
from pisdr import adsb, airlines, config, outlines
from pisdr.adsb import Aircraft
from pisdr.qt import QtCore, QtGui
from pisdr.radio import adsb_decoder
from pisdr.spacewx import SpaceWx
from pisdr.state import RadioState
from pisdr.ui import adsb_page, style
from test_radio import open_sources, radio, sim, sim_one, write_wav  # noqa: F401  (radio, sim, sim_one: fixtures)
from test_scan_and_timer import arm, timer, utc  # noqa: F401  (timer is a fixture)
from test_state import assert_sane
from test_ui import image_stats, press, rows, tap

HOME = (43.04, -87.91)

# What dump1090-fa / readsb write ...
FA_JSON = {"now": 1_700_000_000.0, "messages": 4521, "aircraft": [
    {"hex": "a1b2c3", "flight": "UAL123  ", "alt_baro": 35000, "alt_geom": 35600, "gs": 451.6, "track": 273.0,
     "baro_rate": -1216, "squawk": "1200", "lat": 43.2, "lon": -88.05, "seen_pos": 0.4, "messages": 468,
     "seen": 0.1, "rssi": -7.2},
    {"hex": "40621d", "flight": "KLM1023 ", "alt_baro": "ground", "gs": 12.0, "track": 45.0, "lat": 43.85,
     "lon": -87.1, "seen_pos": 2.0, "seen": 0.5, "rssi": -20.1, "messages": 40},
    {"hex": "abcdef", "seen": 3.2, "messages": 5, "rssi": -30.0},
    {"hex": "~c0ffee", "alt_baro": 4000, "seen": 1.0},
]}
# ... and what dump1090-mutability writes for the same sky.
MUTABILITY_JSON = {"now": 1_700_000_000.0, "messages": 4519, "aircraft": [
    {"hex": "a1b2c3", "flight": "UAL123  ", "altitude": 35000, "vert_rate": -1216, "speed": 451, "track": 274,
     "squawk": "1200", "lat": 43.2, "lon": -88.05, "seen_pos": 0.4, "messages": 467, "seen": 0.1, "rssi": -7.1},
    {"hex": "40621d", "flight": "KLM1023 ", "altitude": "ground", "speed": 12, "track": 45, "lat": 43.85,
     "lon": -87.1, "seen_pos": 2.0, "seen": 0.5, "rssi": -20.1, "messages": 40},
    {"hex": "abcdef", "seen": 3.2, "messages": 5, "rssi": -30.0},
]}

FA_HELP = ("--device-type <type>  Select SDR type\n--device <index|serial>\n--ppm <correction>\n"
           "--lat <latitude>\n--lon <longitude>\n--write-json <dir>\n--write-json-every <t>\n--quiet\n")
MUTABILITY_HELP = "--device-index <index>\n--ppm <error>\n--lat <latitude>\n--lon <longitude>\n" \
                  "--write-json <dir>\n--write-json-every <t>\n--quiet\n"
READSB_HELP = FA_HELP + "--enable-biastee  Set bias tee supply on (default: off)\n"


# --------------------------------------------------------------------------- aircraft.json
@pytest.mark.parametrize("data", (FA_JSON, MUTABILITY_JSON), ids=("fa", "mutability"))
def test_both_json_dialects_give_the_same_aircraft(data):
    aircraft, messages = adsb.parse_aircraft_json(json.dumps(data))
    assert messages == data["messages"]
    united, klm, quiet = aircraft[:3]
    assert (united.icao, united.callsign, united.name) == ("A1B2C3", "UAL123", "UAL123")
    assert (united.lat, united.lon, united.altitude_ft, united.vert_rate_fpm) == (43.2, -88.05, 35000, -1216)
    assert united.speed_kt == pytest.approx(451, abs=1) and united.track_deg == pytest.approx(273.5, abs=1)
    assert united.squawk == "1200" and united.has_position and not united.on_ground
    assert klm.on_ground and klm.altitude_ft is None and klm.has_position
    assert (quiet.name, quiet.callsign, quiet.has_position, quiet.altitude_ft) == ("ABCDEF", "", False, None)
    assert quiet.speed_kt is None and quiet.track_deg is None and quiet.squawk == ""


def test_category_says_what_kind_of_aircraft_it_is():
    entries = [{"hex": f"{0xA00000 + i:06x}", "seen": 1.0, "category": category}
               for i, category in enumerate(("A1", "A3", "A5", "A7", "A0", "D7", "a3", 3, None, "A33"))]
    aircraft, _ = adsb.parse_aircraft_json(json.dumps({"aircraft": entries}))
    assert [ac.category for ac in aircraft] == ["A1", "A3", "A5", "A7", "", "", "", "", "", ""]
    assert [ac.kind for ac in aircraft[:4]] == ["light", "jet", "heavy", "heli"]
    # Until the category has been heard: slow and low is drawn as a light aircraft.
    assert Aircraft("A", speed_kt=110.0, altitude_ft=3000).kind == "light"
    assert Aircraft("A", speed_kt=250.0, altitude_ft=3000).kind == "jet"
    assert Aircraft("A", speed_kt=110.0).kind == Aircraft("A").kind == "jet"
    assert set(adsb._KINDS) <= set(adsb.CATEGORIES)
    assert [Aircraft("A", squawk=code).emergency for code in ("7500", "7600", "7700", "1200", "")] == \
        [True, True, True, False, False]


def test_non_icao_addresses_keep_their_marker():
    aircraft, _ = adsb.parse_aircraft_json(json.dumps(FA_JSON))
    assert aircraft[3].icao == "~C0FFEE" and aircraft[3].altitude_ft == 4000


def test_damaged_entries_are_dropped_one_by_one():
    entries = [
        {"hex": "111111", "seen": 1, "lat": "43.1", "lon": None, "alt_baro": 1e9, "gs": -5, "track": 720,
         "squawk": "9999", "flight": 17, "baro_rate": [1], "rssi": "loud", "messages": -4},
        {"hex": "222222", "lat": 10.0, "lon": 20.0, "seen_pos": config.ADSB_POSITION_MAX_AGE_S + 1, "seen": 2.0},
        {"hex": "333333", "seen": config.ADSB_MAX_AGE_S + 1},          # not heard for too long
        {"hex": "444444", "seen": True},
        {"hex": "zzzzzz"}, {"hex": "12345"}, {"hex": 0x123456}, {"flight": "NOHEX"}, "junk", None, 7, [],
        {"hex": "555555", "lat": float("nan"), "lon": 181.0, "flight": "  n1 2-3\u00e9 "},
    ]
    aircraft, messages = adsb.parse_aircraft_json(json.dumps({"aircraft": entries, "messages": "many"}))
    assert [ac.icao for ac in aircraft] == ["111111", "222222", "555555"] and messages == 0
    bad = aircraft[0]
    assert not bad.has_position and bad.callsign == "" and bad.squawk == "" and bad.messages == 0
    assert bad.altitude_ft is bad.speed_kt is bad.track_deg is bad.vert_rate_fpm is bad.rssi_db is None
    assert not aircraft[1].has_position, "a position a minute old is no longer where the aircraft is"
    assert aircraft[2].callsign == "N123" and not aircraft[2].has_position


@pytest.mark.parametrize("text", ("", "[]", "null", '{"aircraft": 5}', '{"now": 1}', "{truncated"))
def test_a_file_that_is_not_an_aircraft_list_is_refused(text):
    with pytest.raises(ValueError):
        adsb.parse_aircraft_json(text)


# --------------------------------------------------------------------------- airlines
def test_operator_from_the_callsign():
    assert airlines.operator("UAL1482") == "United Airlines"
    assert airlines.operator("FDX1403") == "FedEx" and airlines.operator("RCH871") == "US Air Force (Reach)"
    assert airlines.operator("N512PS") == airlines.operator("N1") == airlines.operator("N12345") == airlines.PRIVATE_US
    assert airlines.operator("NKS123") == "Spirit Airlines", "an airline starting with N is not a registration"
    assert airlines.operator("ZZZ99") == airlines.UNKNOWN
    assert airlines.operator("CGABC") == airlines.operator("TEAL71") == airlines.PRIVATE
    assert airlines.operator("") == ""
    table = airlines.table()
    assert len(table) > 150 and all(len(code) == 3 and code.isupper() and name for code, name in table.items())
    assert max(len(name) for name in table.values()) <= 24, "too long for the flight data panel"
    for sim_callsign, *_rest in adsb.SimTraffic._FLIGHTS:
        assert airlines.operator(sim_callsign) != airlines.UNKNOWN


def test_your_own_airlines_file_adds_and_renames(isolated_files):
    config.USER_AIRLINES_FILE.write_text(json.dumps(
        {"operators": {"zzz": "My Flying Club", "UAL": "United", "TOOLONG": "x", "AB1": "x", "QQQ": 5, "WWW": " "}}))
    airlines.reload()
    assert airlines.operator("ZZZ99") == "My Flying Club" and airlines.operator("UAL1482") == "United"
    assert airlines.operator("DAL2210") == "Delta Air Lines"
    assert airlines.operator("QQQ1") == airlines.operator("WWW1") == airlines.UNKNOWN
    for damaged in ("{not json", "[]", '{"operators": []}', ""):
        config.USER_AIRLINES_FILE.write_text(damaged)
        airlines.reload()
        assert airlines.operator("UAL1482") == "United Airlines" and airlines.operator("ZZZ99") == airlines.UNKNOWN
    config.USER_AIRLINES_FILE.unlink()
    airlines.reload()
    assert airlines.operator("UAL1482") == "United Airlines"


# --------------------------------------------------------------------------- geometry
@pytest.mark.parametrize("a, b, distance, bearing", (
    ((43.04, -87.91), (41.9786, -87.9048), 63.7, 179.8),          # Milwaukee -> Chicago O'Hare
    ((51.4775, -0.4614), (40.6413, -73.7781), 2991.0, 287.9),     # Heathrow -> JFK
    ((0.0, 179.5), (0.0, -179.5), 60.04, 90.0),                   # across the date line
    ((-33.9, 151.2), (-33.9, 151.2), 0.0, 0.0),
))
def test_distance_and_bearing(a, b, distance, bearing):
    d, brg = adsb.distance_bearing(*a, *b)
    assert d == pytest.approx(distance, rel=2e-3, abs=0.01)
    assert d == 0 or brg == pytest.approx(bearing, abs=0.2)


def test_projection_keeps_distance_and_direction_from_the_centre():
    lats = np.array([43.04, 44.04, 43.04, 40.0])
    lons = np.array([-87.91, -87.91, -86.91, -95.0])
    east, north = adsb.project(*HOME, lats, lons)
    assert east[0] == pytest.approx(0, abs=1e-6) and north[0] == pytest.approx(0, abs=1e-6)
    assert north[1] == pytest.approx(60.04, abs=0.05) and east[1] == pytest.approx(0, abs=1e-6)
    assert east[2] == pytest.approx(60.04 * math.cos(math.radians(43.04)), abs=0.1) and 0 < north[2] < 0.5
    for i in range(4):
        d, brg = adsb.distance_bearing(*HOME, lats[i], lons[i])
        assert math.hypot(east[i], north[i]) == pytest.approx(d, abs=1e-6)
    assert [adsb.compass_point(b) for b in (0, 22.4, 22.6, 90, 181, 270, 337.6, 359.9)] == \
        ["N", "N", "NE", "E", "S", "W", "N", "N"]


def test_sorting_and_the_middle_of_the_traffic():
    near, far, lost = Aircraft("A", lat=43.1, lon=-87.9), Aircraft("B", lat=44.5, lon=-87.9), Aircraft("C")
    fleet = [lost, far, near]
    adsb.locate(fleet, *HOME)
    assert [ac.icao for ac in adsb.by_distance(fleet)] == ["A", "B", "C"]
    assert lost.distance_nm is None and near.distance_nm == pytest.approx(3.6, abs=0.1)
    assert adsb.middle_of([lost]) is None
    lat, lon = adsb.middle_of([Aircraft("D", lat=10.0, lon=179.0), Aircraft("E", lat=12.0, lon=-179.0)])
    assert lat == pytest.approx(11.0) and abs(lon) == pytest.approx(180.0, abs=1e-6)


def test_where_the_receiver_is(state):
    assert adsb.receiver_position(state) is None
    state.space_wx = types.SimpleNamespace(wx={"lat": 51.5, "lon": -0.1})
    assert adsb.receiver_position(state) == (51.5, -0.1, "ip")
    state.space_wx = types.SimpleNamespace(wx={"lat": "north", "lon": 400})
    assert adsb.receiver_position(state) is None
    state.space_wx = types.SimpleNamespace(wx={"lat": 51.5, "lon": -0.1, "geo": "settings"})
    assert adsb.receiver_position(state) is None, "weather for a location since cleared is not an IP guess"
    state.wx_lat, state.wx_lon = HOME
    assert adsb.receiver_position(state) == (*HOME, "settings")


def test_exactly_one_shipped_band_is_the_adsb_channel(state):
    names = [band.name for band in state.bands if adsb.is_adsb_band(band)]
    assert names == ["ADS-B 1090"]
    wide = config.Band("UHF", 400_000_000, 1_700_000_000, 1_090_000_000, "AM", 25_000)
    assert not adsb.is_adsb_band(wide)


def test_simulated_traffic_moves_and_reads_like_the_real_thing():
    traffic = adsb.SimTraffic(*HOME, now=0.0)
    first, messages = traffic.aircraft(now=10.0)
    later, more = traffic.aircraft(now=70.0)
    assert len(first) == len(later) >= 10 and more > messages > 0
    assert len({ac.icao for ac in first}) == len(first)
    assert any(not ac.has_position for ac in first) and any(ac.on_ground for ac in first)
    moved = 0
    for before, after in zip(first, later):
        assert before.icao == after.icao
        if not before.has_position or before.on_ground:
            continue
        flown, heading = adsb.distance_bearing(before.lat, before.lon, after.lat, after.lon)
        if flown < 100:                                   # (one may have wrapped to the start of its line)
            assert flown == pytest.approx(before.speed_kt / 60.0, rel=0.03)
            assert abs((heading - before.track_deg + 180) % 360 - 180) < 2.0
            moved += 1
    assert moved >= 8


# --------------------------------------------------------------------------- settings
def test_map_range_and_labels_step_stop_and_persist(state):
    ranges = [nm for nm, _ring in config.ADSB_RANGES_NM]
    assert state.adsb_range_nm == config.ADSB_DEFAULT_RANGE_NM and state.adsb_labels == "FULL"
    for _ in range(10):
        state.step_adsb_range(+1)
    assert state.adsb_range_nm == ranges[-1]
    for _ in range(10):
        state.step_adsb_range(-1)
    assert state.adsb_range_nm == ranges[0]
    seen = []
    for _ in range(len(config.ADSB_LABELS)):
        state.cycle_adsb_labels()
        seen.append(state.adsb_labels)
    assert sorted(seen) == sorted(config.ADSB_LABELS) and state.adsb_labels == "FULL"
    state.cycle_adsb_labels()
    state.save()
    again = RadioState()
    again.load()
    assert (again.adsb_range_nm, again.adsb_labels) == (ranges[0], "NAME")
    assert not again.adsb_mode and again.aircraft == [], "ADS-B mode never survives a restart"


@pytest.mark.parametrize("bad", ({"adsb_range_nm": 33, "adsb_labels": "LOUD"},
                                 {"adsb_range_nm": "100", "adsb_labels": None},
                                 {"adsb_range_nm": [], "adsb_labels": 2}))
def test_bad_map_settings_fall_back_to_defaults(bad):
    config.SETTINGS_FILE.write_text(json.dumps(dict(bad, volume=41)))
    state = RadioState()
    state.load()
    assert (state.adsb_range_nm, state.adsb_labels) == (config.ADSB_DEFAULT_RANGE_NM, "FULL")
    assert state.volume == 41
    assert_sane(state)


# --------------------------------------------------------------------------- outlines
def test_bundled_outlines_cover_coasts_lakes_and_borders():
    data = outlines.load()
    assert data is not None, "config/outlines.npz is missing - run tools/build_outlines.py"
    assert len(data["kind"]) > 2000 and set(np.unique(data["kind"])) == {0, 1, 2, 3}
    assert data["lat"].dtype == np.int16 and abs(data["lat"]).max() <= 9000 and abs(data["lon"]).max() <= 18000

    def kinds(lat, lon, radius=100.0):
        runs = outlines.near(lat, lon, radius)
        for _kind, east, north in runs:
            assert len(east) == len(north) >= 2
            assert np.hypot(east, north).min() <= radius, "a run that never enters the view"
        return {kind for kind, _e, _n in runs}

    assert outlines.LAKE in kinds(*HOME) and outlines.STATE in kinds(*HOME)        # Lake Michigan, WI / IL
    assert {outlines.COAST, outlines.BORDER} <= kinds(51.0, 2.0)                  # Channel, France / Belgium
    assert outlines.COAST in kinds(-17.8, 179.9) and outlines.COAST in kinds(-16.8, -179.9)   # Fiji
    assert kinds(0.0, -140.0) == set()                                             # open Pacific
    assert outlines.COAST in kinds(78.2, 15.6) and outlines.COAST in kinds(-77.8, 166.7)      # near the poles


def test_outlines_are_placed_where_they_are():
    """The southern tip of Lake Michigan is about 85 nm south-south-east of Milwaukee."""
    runs = [(east, north) for kind, east, north in outlines.near(*HOME, 150.0) if kind == outlines.LAKE]
    east = np.concatenate([e for e, _n in runs])
    north = np.concatenate([n for _e, n in runs])
    tip = int(np.argmin(np.where(np.abs(east) < 40, north, np.inf)))
    assert -95 < north[tip] < -75 and 0 < east[tip] < 40


def test_missing_or_damaged_outlines_just_leave_the_map_bare(tmp_path):
    assert outlines.near(*HOME, 100.0, path=tmp_path / "none.npz") == []
    (tmp_path / "junk.npz").write_bytes(b"not a zip file")
    assert outlines.near(*HOME, 100.0, path=tmp_path / "junk.npz") == []
    np.savez(tmp_path / "short.npz", lat=np.zeros(3, np.int16), lon=np.zeros(2, np.int16),
             start=np.array([0, 3]), kind=np.zeros(1, np.uint8), box=np.zeros((1, 4), np.int16))
    assert outlines.near(*HOME, 100.0, path=tmp_path / "short.npz") == []


def test_outline_build_tool(tmp_path, capsys):
    tool = Path(__file__).resolve().parent.parent / "tools" / "build_outlines.py"
    spec = importlib.util.spec_from_file_location("build_outlines", tool)
    build_outlines = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_outlines)

    def geojson(geometry) -> str:
        return json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": geometry}, {"type": "Feature", "geometry": None}]})
    source = tmp_path / "ne"
    source.mkdir()
    shore = [[-88.0, 42.5], [-87.8, 43.0], [-87.8, 43.0], [-87.9, 43.5]]          # one repeated point
    (source / "ne_50m_coastline.geojson").write_text(geojson({"type": "LineString", "coordinates": shore}))
    (source / "ne_50m_lakes.geojson").write_text(geojson({"type": "MultiPolygon", "coordinates": [
        [[[-87.0, 43.0], [-86.5, 43.0], [-86.5, 43.4], [-87.0, 43.0]]]]}))
    (source / "ne_50m_admin_0_boundary_lines_land.geojson").write_text(geojson(
        {"type": "MultiLineString", "coordinates": [[[10.0, 50.0], [11.0, 51.0]], [[12.0, 50.0]]]}))
    (source / "ne_50m_admin_1_states_provinces_lines.geojson").write_text(geojson(
        {"type": "LineString", "coordinates": [[-90.0, 42.5], [-87.0, 42.5]]}))
    out = tmp_path / "out.npz"
    assert build_outlines.main(["--source", str(source), "--out", str(out)]) == 0
    assert "4 lines, 11 points" in capsys.readouterr().out
    runs = outlines.near(*HOME, 60.0, path=out)
    assert sorted(kind for kind, _e, _n in runs) == [outlines.COAST, outlines.LAKE, outlines.STATE]
    coast = next((east, north) for kind, east, north in runs if kind == outlines.COAST)
    assert len(coast[0]) == 3
    assert coast[1][1] == pytest.approx(-2.4, abs=0.1) and coast[0][1] == pytest.approx(4.8, abs=0.1)


# --------------------------------------------------------------------------- decoder process
@pytest.fixture
def decoder_env(tmp_path, monkeypatch):
    """Fresh PATH entry for stand-in programs, scratch files under tmp_path, short start-up wait."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(adsb_decoder, "_HELP", {})
    monkeypatch.setattr(adsb_decoder, "_START_WAIT_S", 0.3)
    for name in config.ADSB_PROGRAMS:
        assert shutil.which(name) in (None, str(bin_dir / name)), f"a real {name} would be started"
    return bin_dir


def script(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_decoder(bin_dir: Path, help_text: str = FA_HELP, data: dict | None = FA_JSON,
                 then: str = "exec sleep 30", name: str = "dump1090-fa") -> Path:
    """A stand-in decoder: answers --help, logs its arguments, writes aircraft.json, then *then*."""
    args = bin_dir.parent / f"{name}.args"
    sample = bin_dir.parent / f"{name}.json"
    sample.write_text(json.dumps(data) if data is not None else "")
    script(bin_dir, name,
           f'if [ "$1" = "--help" ]; then cat <<\'EOF\'\n{help_text}EOF\nexit 0; fi\n'
           f'echo "$@" > {args}\n'
           'while [ $# -gt 0 ]; do [ "$1" = "--write-json" ] && dir="$2"; shift; done\n'
           + (f'cp {sample} "$dir/aircraft.json"\n' if data is not None else "")
           + then + "\n")
    return args


def test_command_line_uses_only_what_the_decoder_offers(decoder_env):
    fa = str(script(decoder_env, "fa", f"cat <<'EOF'\n{FA_HELP}EOF\n"))
    old = str(script(decoder_env, "old", f"cat <<'EOF'\n{MUTABILITY_HELP}EOF\n"))
    readsb = str(script(decoder_env, "readsb", f"cat <<'EOF'\n{READSB_HELP}EOF\n"))
    common = ["--quiet", "--write-json", "/run/x", "--write-json-every", "1"]
    assert adsb_decoder.build_command(fa, "/run/x") == [fa, "--device-type", "rtlsdr", *common]
    assert adsb_decoder.build_command(old, "/run/x", ppm=-2, bias_tee=True, position=(43.04, -87.91)) == \
        [old, *common, "--ppm", "-2", "--lat", "43.0400", "--lon", "-87.9100"]
    assert adsb_decoder.build_command(readsb, "/run/x", ppm=0, bias_tee=True) == \
        [readsb, "--device-type", "rtlsdr", *common, "--enable-biastee"]
    assert "--enable-biastee" not in adsb_decoder.build_command(readsb, "/run/x", bias_tee=False)


def test_decoders_are_looked_for_in_order_and_checked(decoder_env):
    assert adsb_decoder.find_program() is None
    assert "No ADS-B decoder" in adsb_decoder.problem_with(None)
    ancient = script(decoder_env, "dump1090", 'echo "--interactive --net"\n')
    assert adsb_decoder.find_program() == str(ancient)
    assert "too old" in adsb_decoder.problem_with(str(ancient))
    broken = script(decoder_env, "readsb", "exit 127\n")
    assert adsb_decoder.find_program() == str(broken)
    assert "does not run" in adsb_decoder.problem_with(str(broken))
    good = script(decoder_env, "dump1090-fa", f"cat <<'EOF'\n{FA_HELP}EOF\n")
    assert adsb_decoder.find_program() == str(good) and adsb_decoder.problem_with(str(good)) is None


def test_decoder_runs_is_read_and_cleans_up(decoder_env, tmp_path):
    args = fake_decoder(decoder_env)
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.read() == ([], 0, "ADS-B decoder is not running")
    assert decoder.start(ppm=3, position=HOME) is None
    assert decoder.alive and decoder.program == "dump1090-fa"
    scratch = decoder._dir
    assert scratch.parent == tmp_path, "scratch folder belongs on the RAM disk when there is one"
    assert args.read_text().split() == ["--device-type", "rtlsdr", "--quiet", "--write-json", str(scratch),
                                        "--write-json-every", "1", "--ppm", "3",
                                        "--lat", "43.0400", "--lon", "-87.9100"]
    aircraft, messages, error = decoder.read()
    assert [ac.name for ac in aircraft] == ["UAL123", "KLM1023", "ABCDEF", "~C0FFEE"]
    assert (messages, error) == (4521, "")
    (scratch / "aircraft.json").write_text('{"now": 1, "messa')          # caught half-written
    assert decoder.read() == (aircraft, 4521, ""), "a bad read must keep the last good list"
    process = decoder._proc
    decoder.stop()
    assert process.poll() is not None and not scratch.exists() and not decoder.alive
    decoder.stop()                                                       # twice is harmless


def test_decoder_that_cannot_get_the_dongle_says_why(decoder_env, tmp_path):
    fake_decoder(decoder_env, data=None, then='echo "rtlsdr: no supported devices found." >&2\nexit 1')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start() == "dump1090-fa stopped: rtlsdr: no supported devices found."
    assert not decoder.alive and not list(tmp_path.glob("pisdr-adsb-*"))
    fake_decoder(decoder_env, data=None, then="exit 3")                  # says nothing at all
    assert decoder.start() == "dump1090-fa stopped: exit code 3"


def test_decoder_that_dies_later_is_noticed(decoder_env):
    fake_decoder(decoder_env, then='sleep 0.5\necho "rtlsdr: device disconnected" >&2\n'
                                   'echo "Abnormal exit." >&2\nexit 1')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start() is None
    assert decoder.read()[2] == ""
    decoder._proc.wait(timeout=5)
    aircraft, messages, error = decoder.read()
    assert aircraft == [] and messages == 4521
    assert error == "dump1090-fa stopped: rtlsdr: device disconnected"
    decoder.stop()


def test_no_decoder_installed(decoder_env):
    assert "No ADS-B decoder (dump1090) found" in adsb_decoder.AdsbDecoder().start()


def test_bias_tee_goes_through_rtl_biast_when_the_decoder_has_no_switch(decoder_env, tmp_path):
    fake_decoder(decoder_env, help_text=MUTABILITY_HELP, name="dump1090-mutability")
    log = tmp_path / "biast.args"
    script(decoder_env, "rtl_biast", f'echo "$@" > {log}\n')
    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start(bias_tee=False) is None and not log.exists()
    assert decoder.start(bias_tee=True) is None                          # (restarts the decoder)
    assert log.read_text().split() == ["-b", "1"]
    decoder.stop()
    (decoder_env / "rtl_biast").unlink()
    assert decoder.start(bias_tee=True) is None, "no rtl_biast: carry on without the bias tee"
    decoder.stop()


# --------------------------------------------------------------------------- back ends
def test_simulator_lends_the_dongle_and_takes_it_back(sim_one, state, qapp, tmp_path):   # noqa: F811
    sim = sim_one                                                        # one dongle: the map takes it
    state.set_recording(True)
    assert sim.start_adsb_mode() is None
    assert state.adsb_mode and state.dongle_lent and not state.recording and not sim.running
    assert state.aircraft == [] and sim.start_adsb_mode() is None
    sim.poll_adsb()
    assert len(state.aircraft) >= 10 and state.adsb_messages > 0 and state.adsb_error == ""
    near = [ac for ac in state.aircraft if ac.has_position]
    adsb.locate(near, *config.ADSB_SIM_HOME)
    assert max(ac.distance_nm for ac in near) < 400, "traffic belongs around the simulator's home"
    # Nothing else may use the receiver meanwhile.
    state.set_recording(True)
    assert not state.recording
    assert sim.play_file(write_wav(tmp_path / "ok.wav")).startswith("Leave network mode / the ADS-B map")
    assert sim.start_network_mode() == "Leave the ADS-B map first" and not state.network_mode
    sim.frames.clear()
    pump(qapp, 0.25)
    assert sim.frames == [], "the spectrum kept running without a dongle"
    assert sim.stop_adsb_mode() is None
    assert not state.adsb_mode and state.aircraft == [] and sim.running
    pump(qapp, 0.3)
    assert sim.frames, "spectrum did not come back"
    sim.poll_adsb()                                                      # a late timer tick is ignored
    assert state.aircraft == []


def test_simulated_traffic_flies_around_the_set_location(sim, state):   # noqa: F811
    state.wx_lat, state.wx_lon = 51.5, -0.1
    sim.start_adsb_mode()
    sim.poll_adsb()
    placed = [ac for ac in state.aircraft if ac.has_position]
    adsb.locate(placed, 51.5, -0.1)
    assert min(ac.distance_nm for ac in placed) < 60
    sim.stop()
    assert not sim._adsb_timer.isActive()


def test_network_mode_keeps_the_map_out(sim, state):   # noqa: F811
    assert sim.start_network_mode() is None
    assert sim.start_adsb_mode() == "Leave network mode first" and not state.adsb_mode
    assert sim.stop_network_mode() is None


def test_record_timer_waits_while_the_map_has_the_dongle(sim_one, state, timer, clock):   # noqa: F811
    sim = sim_one
    arm(state, 20 * 60, length_min=30)
    sim.start_adsb_mode()
    clock.wall = utc(20, 0)
    timer.check(clock.wall)
    assert not state.recording and state.adsb_mode
    sim.stop_adsb_mode()
    clock.wall = utc(20, 5)
    timer.check(clock.wall)                            # the rest of the window is still recorded
    assert state.recording and state.vfo_hz == 6_840_000
    state.set_recording(False)


@needs_fake_gr
def test_adsb_mode_hands_the_dongle_over_and_takes_it_back(radio, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.set_ppm(-3)
    state.set_dsp_nr_level(3)
    state.wx_lat, state.wx_lon = HOME
    state.set_recording(True)
    assert radio.start_adsb_mode() is None
    assert state.adsb_mode and not state.recording
    assert open_sources() == 0, "dongle still open: the decoder could not have claimed it"
    assert radio.adsb.alive and radio._adsb_timer.isActive()
    argv = args.read_text().split()
    assert argv[argv.index("--ppm") + 1] == "-3" and argv[argv.index("--lat") + 1] == "43.0400"
    radio.poll_adsb()
    assert [ac.name for ac in state.aircraft][:2] == ["UAL123", "KLM1023"] and state.adsb_messages == 4521
    state.set_vfo(7_000_000)                    # ignored while the dongle is away; must not raise
    state.set_volume(20)
    assert radio.start_network_mode() == "Leave the ADS-B map first"

    process, scratch = radio.adsb._proc, radio.adsb._dir
    assert radio.stop_adsb_mode() is None
    assert not state.adsb_mode and state.aircraft == [] and not radio._adsb_timer.isActive()
    assert process.poll() is not None and not scratch.exists()
    assert open_sources() == 1 and radio.tb.running
    assert radio.volume.last("set_k") == (0.2 ** 2,)
    assert radio.src.last("set_freq_corr") == (-3, 0)
    assert radio.src.last("set_center_freq") is not None


@needs_fake_gr
def test_an_ip_guess_of_the_location_is_not_given_to_the_decoder(radio, state, decoder_env):   # noqa: F811
    args = fake_decoder(decoder_env)
    state.space_wx = types.SimpleNamespace(wx={"lat": 43.0, "lon": -88.0})
    assert radio.start_adsb_mode() is None
    assert "--lat" not in args.read_text().split()
    radio.stop_adsb_mode()


@needs_fake_gr
def test_adsb_mode_falls_back_when_the_decoder_cannot_start(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env, data=None, then='echo "usb_claim_interface error -6" >&2\nexit 1')
    assert radio.start_adsb_mode() == "dump1090-fa stopped: usb_claim_interface error -6"
    assert not state.adsb_mode and not radio.radio_down and not radio._adsb_timer.isActive()
    assert open_sources() == 1 and radio.tb.running
    state.set_vfo(7_000_000)
    assert radio.src.last("set_center_freq") is not None


@needs_fake_gr
def test_adsb_mode_without_a_decoder_leaves_the_radio_alone(radio, state, decoder_env):   # noqa: F811
    src = radio.src
    assert "No ADS-B decoder" in radio.start_adsb_mode()
    assert radio.src is src and radio.tb.running and not state.adsb_mode


@needs_fake_gr
def test_decoder_dying_is_reported_and_back_still_returns_the_radio(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env, then='sleep 0.5\necho "rtlsdr: device disconnected" >&2\nexit 1')
    assert radio.start_adsb_mode() is None
    radio.adsb._proc.wait(timeout=5)
    radio.poll_adsb()
    assert state.adsb_mode and state.adsb_error == "dump1090-fa stopped: rtlsdr: device disconnected"
    assert radio.stop_adsb_mode() is None
    assert open_sources() == 1 and radio.tb.running and state.adsb_error == ""


@needs_fake_gr
def test_quitting_from_the_map_stops_the_decoder(radio, state, decoder_env):   # noqa: F811
    fake_decoder(decoder_env)
    assert radio.start_adsb_mode() is None
    process = radio.adsb._proc
    radio.stop()
    assert process.poll() is not None and not radio._adsb_timer.isActive()


# --------------------------------------------------------------------------- the page
def adsb_tile(window) -> int:
    return next(i for i, band in enumerate(window.state.bands) if adsb.is_adsb_band(band))


@pytest.fixture
def chart(window, qapp):
    """The map open on the simulator with the receiver's location set, one poll in."""
    window.state.wx_lat, window.state.wx_lon = HOME
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    return window.adsb_page


def keys(window) -> list[str]:
    return [button.text() for button in window.softkeys.buttons]


def pixel(widget, x: float, y: float) -> str:
    """Colour of one pixel of a widget as it is drawn, "#rrggbb"."""
    return widget.grab().toImage().pixelColor(int(x), int(y)).name()


def commonest(widget) -> str:
    """The colour most of a widget is drawn in: its background."""
    image = widget.grab().toImage()
    counts: dict[int, int] = {}
    for x in range(0, image.width(), 5):
        for y in range(0, image.height(), 5):
            counts[image.pixel(x, y)] = counts.get(image.pixel(x, y), 0) + 1
    return QtGui.QColor(max(counts, key=counts.get)).name()


@pytest.fixture
def one_dongle_chart(one_dongle_window, qapp):
    """The same on a radio with one dongle, which the map takes from the receiver."""
    window = one_dongle_window
    window.state.wx_lat, window.state.wx_lon = HOME
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    return window.adsb_page


def test_the_map_has_its_own_dongle_and_the_radio_plays_on(window, qapp):
    state = window.state
    state.tune_to(100_100_000, "WFM", 100_000)
    band = state.band.name
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    assert state.adsb_mode and window.stack.currentIndex() == window.PAGE_ADSB
    assert (state.vfo_hz, state.band.name) == (100_100_000, band), "the receiver was retuned to 1090 MHz"
    assert window.backend.running and not state.receiver_paused and state.tuner == "V3"
    top = window.topbar_left.text()
    assert "ADS-B MAP" in top and "100.1000 MHz WFM" in top and "paused" not in top
    window.key_pressed(3)                              # BACK
    assert not state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert state.vfo_hz == 100_100_000 and window.backend.running
    assert not window.toast.isVisible() or "dongle" not in window.toast.text()


def test_the_map_says_so_when_the_receiver_had_to_stop_for_it(window, qapp):
    state = window.state
    state.set_vfo(2_000_000_000)                       # above the V3: on the XTR, which the map needs
    window._enter_adsb()
    assert state.adsb_mode and state.receiver_paused and "receiver paused" in window.topbar_left.text()
    window.key_pressed(3)
    assert window.backend.running and window.toast.text() == "Radio back on the dongle"


def test_picking_the_adsb_band_opens_the_map(one_dongle_window, qapp, one_dongle_chart):
    window, chart = one_dongle_window, one_dongle_chart
    state = window.state
    assert state.adsb_mode and window.stack.currentIndex() == window.PAGE_ADSB
    assert state.band.name == "ADS-B 1090" and state.vfo_hz == config.ADSB_FREQ_HZ
    assert "receiver paused" in window.topbar_left.text()
    assert keys(window) == ["ZOOM \u2212", "ZOOM +", "LIST", "BACK"]       # what the four keys do ...
    assert not window.softkeys.isVisible(), "... though the bar is not shown: the map has its own buttons"
    assert "ADS-B MAP" in window.topbar_left.text() and "KNOB SELECT" in window.topbar_right.text()
    colours, brightness = image_stats(window)
    assert colours > 40 and brightness > 3, "map is blank"
    radar = chart.radar
    assert radar.centre == HOME and radar.centre_source == "settings"
    assert radar.total == len(chart.order) == len(state.aircraft) and radar.place.startswith("43.04")
    assert len(radar._hits) >= 4, "aircraft were not drawn"
    window.key_pressed(0)                              # ZOOM -: a wider view has more of them
    window.key_pressed(0)
    pump(qapp, 0.05)
    assert len(radar._hits) >= 8
    window.key_pressed(3)                              # BACK
    assert not state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert window.backend.running and state.vfo_hz == config.ADSB_FREQ_HZ
    assert "ADS-B MAP" not in window.topbar_left.text() and window.softkeys.isVisible()


def test_back_button_on_the_map_returns_to_the_radio(window, qapp, chart):
    radar = chart.radar
    assert radar.back_btn.isVisible() and radar.back_btn.text() == "BACK"
    box = radar.back_btn.geometry()
    assert box.left() < 10 and box.bottom() > radar.height() - 10, "BACK belongs in the bottom-left corner"
    radar.back_btn.click()
    assert not window.state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert window.softkeys.isVisible() and window.backend.running
    assert keys(window) == window.main_page.softkey_labels()
    window._enter_adsb()                               # and again, by the fourth key
    assert not window.softkeys.isVisible()
    window.key_pressed(3)
    assert not window.state.adsb_mode and window.softkeys.isVisible()


def test_a_decoder_that_does_not_start_leaves_the_keys_on_screen(window, qapp, monkeypatch):
    monkeypatch.setattr(window.backend, "_hw_adsb", lambda on: "No ADS-B decoder (dump1090) found - run install.sh")
    window._enter_adsb()
    assert window.stack.currentIndex() == window.PAGE_MAIN and window.softkeys.isVisible()


def test_settings_row_opens_the_map_without_retuning(window, qapp):
    state = window.state
    state.set_vfo(14_074_000)
    page = window.settings_page
    window.show_page(window.PAGE_SETTINGS)
    page.selected = next(i for i, r in rows(window) if r.label.startswith("ADS-B"))
    window.key_pressed(2)
    assert state.adsb_mode and window.stack.currentIndex() == window.PAGE_ADSB
    window.key_pressed(3)
    assert not state.adsb_mode and window.stack.currentIndex() == window.PAGE_MAIN
    assert state.vfo_hz == 14_074_000


def test_map_locks_the_ui_until_back(window, qapp, chart):
    for action in (window.show_main, lambda: window.key_long_pressed(0), lambda: window.key_long_pressed(2),
                   lambda: window.key_long_pressed(3), window.encoder_held, window.encoder_pushed,
                   lambda: window.knob_turned(5), lambda: press(window, QtCore.Qt.Key.Key_Escape),
                   lambda: press(window, QtCore.Qt.Key.Key_5), lambda: window.show_page(window.PAGE_SETTINGS),
                   window._enter_network, window._enter_adsb, lambda: window.key_pressed(2),
                   lambda: window.key_long_pressed(1), lambda: window.key_pressed(2)):
        vfo = window.state.vfo_hz
        action()
        assert window.stack.currentIndex() == window.PAGE_ADSB and window.state.adsb_mode
        assert window.state.vfo_hz == vfo and not window.state.network_mode
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN


def test_the_radar_is_drawn_in_the_colours_of_the_theme(window, qapp):
    window.state.wx_lat, window.state.wx_lon = HOME
    seen = set()
    for theme in config.THEME_NAMES:
        window.state.set_theme(theme)
        colours = style.THEMES[theme]
        window._enter_adsb()
        window.backend.poll_adsb()
        window.knob_turned(1)
        pump(qapp, 0.05)
        assert image_stats(window)[0] > 40
        radar, panel = window.adsb_page.radar, window.adsb_page.panel
        ring = radar._static_layer().toImage().pixelColor(      # the ring of the selected range, due east
            int(radar.width() / 2 + radar.radius()), radar.height() // 2).name()
        found = (ring, commonest(radar), pixel(panel, panel.width() - 4, 120),
                 pixel(panel, 1, 200), radar.zoom_in.grab().toImage().pixelColor(4, 16).name())
        assert found == (colours["ACCENT"], colours["BG"], colours["BG"], colours["ACCENT"], colours["PANEL_HI"]), theme
        seen.add(found)
        window.key_pressed(2)                          # the list
        pump(qapp, 0.05)
        table = window.adsb_page.flight_list
        assert pixel(table, 2, table.height() - 2) == colours["BG"]
        assert pixel(table, table.width() // 2, table.HEADER_PX - 3) == colours["ACCENT"]
        window.key_pressed(3)
    assert len(seen) == len(config.THEME_NAMES), "a theme was drawn in the colours of another"


def test_text_is_in_the_typeface_of_the_rest_of_the_program(window, qapp, chart):
    for widget in (chart.radar, chart.panel, chart.flight_list):
        font = adsb_page._font(widget, 20, True)
        assert font.family() == window.softkeys.buttons[0].font().family()
        assert font.pixelSize() == 20 and font.bold() and not adsb_page._font(widget, 11).bold()


def test_the_map_fills_its_side_of_the_screen(window, qapp, chart):
    radar, state = chart.radar, window.state
    pump(qapp, 0.05)
    by_icao = {ac.icao: ac for ac in chart.order}
    assert any(by_icao[icao].distance_nm > state.adsb_range_nm for icao, _x, _y in radar._hits), \
        "aircraft beyond the selected range belong on the map where it is wider than that"
    rings = radar.rings_nm()
    assert rings[:4] == [25, 50, 75, 100] and rings[-1] > state.adsb_range_nm, "rings go on out to the corners"
    assert rings[-1] <= radar.reach_nm() < rings[-1] + 25
    assert radar.reach_nm() * radar.scale() == pytest.approx(math.hypot(radar.width(), radar.height()) / 2)
    for _ in range(len(config.ADSB_RANGES_NM)):
        window.key_pressed(1)                          # ZOOM + to the closest view
    pump(qapp, 0.05)
    assert state.adsb_range_nm == config.ADSB_RANGES_NM[0][0] and radar.rings_nm()[:2] == [5, 10]
    assert 0 < len(radar._hits) < sum(1 for ac in chart.order if ac.has_position)
    assert radar.total == len(chart.order), "the count is of everything heard, on the map or not"
    assert all(-12 <= x <= radar.width() + 12 and -12 <= y <= radar.height() + 12 for _icao, x, y in radar._hits)
    assert all(by_icao[icao].distance_nm <= radar.reach_nm() * 1.05 for icao, _x, _y in radar._hits)


def test_auto_shows_each_aircraft_in_turn(window, qapp, chart):
    on_map = [ac.icao for ac in chart.order if ac.has_position]
    assert chart.selected == "" and chart.panel.auto and chart._auto_timer.isActive()
    assert chart._auto_timer.interval() == config.ADSB_AUTO_S * 1000
    assert chart.shown == on_map[0] == chart.radar.selected
    assert chart.panel.fields["callsign"] == chart.order[0].name
    seen = [chart.shown]
    for _ in range(len(on_map)):
        chart.auto_step()
        seen.append(chart.shown)
    assert seen == on_map + on_map[:1], "AUTO goes round the aircraft on the map, nearest first"
    assert chart.panel.fields["callsign"] == chart.order[0].name
    # The one being shown flies out of range: AUTO moves on by itself.
    chart.auto_step()
    gone = chart.shown
    window.state.set_aircraft([ac for ac in window.state.aircraft if ac.icao != gone], 10)
    assert chart.shown and chart.shown != gone
    window.state.set_aircraft([Aircraft("ABCDEF", callsign="N1")], 10)
    assert chart.shown == "ABCDEF", "nothing on the map: AUTO shows what has been heard"
    window.state.set_aircraft([])
    assert chart.shown == "" and chart.panel.fields["callsign"] == "------"
    pump(qapp, 0.05)
    window.key_pressed(3)
    assert not chart._auto_timer.isActive() and not chart.radar._sweep.isActive()


def test_knob_holds_then_walks_the_aircraft_nearest_first(window, qapp, chart):
    order = chart.order
    distances = [ac.distance_nm for ac in order if ac.distance_nm is not None]
    assert distances == sorted(distances) and len(distances) >= 8
    assert order[-1].distance_nm is None, "aircraft without a position go last"
    chart.auto_step()
    shown = chart.shown
    window.knob_turned(1)                              # the first movement holds what AUTO was showing
    assert chart.selected == shown == order[1].icao and not chart.panel.auto
    chart.auto_step()
    assert chart.shown == shown, "AUTO must not move on while an aircraft is held"
    window.knob_turned(3)                              # one aircraft per movement, however fast
    assert chart.selected == order[2].icao
    assert chart.panel.fields["callsign"] == order[2].name and "nm" in chart.panel.fields["distance"]
    for _ in range(len(order) - 3):
        window.knob_turned(1)
    assert chart.selected == order[-1].icao
    assert chart.panel.fields["distance"] == "no position"
    window.knob_turned(1)
    assert chart.selected == order[0].icao             # wraps
    window.knob_turned(-1)
    assert chart.selected == order[-1].icao
    window.encoder_pushed()                            # back to AUTO, carrying on from there
    assert chart.selected == "" and chart.panel.auto and chart.shown == order[-1].icao
    chart.auto_step()
    assert chart.shown == order[0].icao


def test_tapping_selects_on_the_radar(window, qapp, chart):
    radar = chart.radar
    window.key_pressed(0)
    pump(qapp, 0.05)
    icao, x, y = min(radar._hits, key=lambda hit: math.hypot(hit[1] - radar.width() / 2, hit[2] - 90))
    tap(radar, QtCore.QPoint(int(x) + 4, int(y) - 3))
    assert chart.selected == icao == radar.selected and not chart.panel.auto
    window.backend.poll_adsb()                         # the selection follows the aircraft as it moves
    pump(qapp, 0.05)
    assert chart.selected == icao
    empty = next(QtCore.QPoint(px, py) for px in range(10, radar.width(), 15) for py in range(10, radar.height(), 15)
                 if radar.childAt(QtCore.QPoint(px, py)) is None
                 and all(math.hypot(px - hx, py - hy) > adsb_page.TAP_RADIUS_PX + 2 for _i, hx, hy in radar._hits))
    tap(radar, empty)
    assert chart.selected == "" and chart.panel.auto


def test_zoom_keys_and_buttons(window, qapp, chart):
    state = window.state
    ranges = [nm for nm, _ring in config.ADSB_RANGES_NM]
    radar = chart.radar
    scale = radar.scale()
    window.key_pressed(1)                              # ZOOM +
    assert state.adsb_range_nm == ranges[ranges.index(config.ADSB_DEFAULT_RANGE_NM) - 1]
    assert radar.scale() > scale
    radar.zoom_out.click()
    assert state.adsb_range_nm == config.ADSB_DEFAULT_RANGE_NM
    radar.zoom_in.click()
    assert state.adsb_range_nm == ranges[ranges.index(config.ADSB_DEFAULT_RANGE_NM) - 1]
    for _ in range(len(ranges) + 2):
        window.key_pressed(0)                          # ZOOM - stops at the widest view
    assert state.adsb_range_nm == ranges[-1]
    pump(qapp, 0.05)
    east, north = adsb.project(*HOME, HOME[0] + ranges[-1] / 60.0, HOME[1])
    assert radar.to_screen(float(east), float(north))[1] == pytest.approx(adsb_page.EDGE_PX, abs=1.5), \
        "the selected range should be the outer ring"
    state.save()
    assert json.loads(config.SETTINGS_FILE.read_text())["adsb_range_nm"] == ranges[-1]


def test_labels_trails_and_ground_from_the_screen_and_from_held_keys(window, qapp, chart):
    state, radar = window.state, chart.radar
    for expected in ("NAME", "OFF", "FULL"):           # hold LIST
        window.key_long_pressed(2)
        assert state.adsb_labels == expected and radar.labels_btn.text() == f"LABELS\n{expected}"
        assert window.toast.text() == f"Labels {expected}" and keys(window)[2] == "LIST"
        pump(qapp, 0.02)
    radar.labels_btn.click()
    assert state.adsb_labels == "NAME"

    assert state.adsb_trails and radar.trails_btn.text() == "TRAILS\nON"
    window.key_long_pressed(1)                         # hold ZOOM +
    assert not state.adsb_trails and radar.trails_btn.text() == "TRAILS\nOFF" and radar.trails_btn.property("off")
    pump(qapp, 0.05)
    radar.trails_btn.click()
    assert state.adsb_trails and not radar.trails_btn.property("off")

    grounded = [ac.icao for ac in state.aircraft if ac.on_ground]
    assert grounded and all(icao in [ac.icao for ac in chart.order] for icao in grounded)
    total = radar.total
    window.key_long_pressed(0)                         # hold ZOOM -
    assert not state.adsb_ground and radar.ground_btn.text() == "GROUND\nOFF"
    assert "hidden" in window.toast.text()
    assert not any(ac.on_ground for ac in chart.order) and radar.total == total - len(grounded)
    pump(qapp, 0.05)
    assert not any(icao in grounded for icao, _x, _y in radar._hits)
    window.backend.poll_adsb()
    assert not any(ac.on_ground for ac in chart.order)
    radar.ground_btn.click()
    assert state.adsb_ground and radar.total == total
    state.toggle_adsb_trails()
    state.toggle_adsb_ground()
    state.save()
    saved = json.loads(config.SETTINGS_FILE.read_text())
    assert (saved["adsb_labels"], saved["adsb_trails"], saved["adsb_ground"]) == ("NAME", False, False)
    again = RadioState()
    again.load()
    assert (again.adsb_trails, again.adsb_ground) == (False, False)
    assert_sane(again)


def test_labels_follow_the_setting():
    cruise = Aircraft("A1B2C3", callsign="UAL123", altitude_ft=35000, vert_rate_fpm=-64)
    climb = Aircraft("ABCDEF", altitude_ft=2449, vert_rate_fpm=1200)
    assert adsb_page.tag_lines(cruise, "FULL", False) == ["UAL123", "350"]
    assert adsb_page.tag_lines(climb, "FULL", False) == ["ABCDEF", "024\u2191"]
    assert adsb_page.tag_lines(cruise, "NAME", False) == ["UAL123"]
    assert adsb_page.tag_lines(cruise, "OFF", False) == [] and adsb_page.tag_lines(cruise, "OFF", True) != []
    assert adsb_page.level_text(Aircraft("X", on_ground=True)) == "GND"
    assert adsb_page.level_text(Aircraft("X", altitude_ft=-200, vert_rate_fpm=-900)) == "000\u2193"
    assert adsb_page.level_text(Aircraft("X")) == ""


def test_flight_data_spells_the_aircraft_out():
    fields = adsb_page.flight_fields(Aircraft(
        "A1B2C3", callsign="UAL123", lat=43.2, lon=-88.05, altitude_ft=35000, vert_rate_fpm=-1216,
        speed_kt=451.6, track_deg=7.0, squawk="1200", rssi_db=-7.2, distance_nm=11.4, bearing_deg=327.0,
        category="A3"))
    assert fields == {"callsign": "UAL123", "type": "A1B2C3 LARGE", "airline": "United Airlines",
                      "altitude": "35,000 ft", "climb": "\u21931,216 fpm", "speed": "452 kts", "track": "007 deg",
                      "distance": "11.4 nm NW", "squawk": "1200", "signal": "SIG -7 dB"}
    bare = adsb_page.flight_fields(Aircraft("ABCDEF"))
    assert bare["callsign"] == "ABCDEF" and bare["type"] == "NO CALLSIGN YET" and bare["airline"] == "--"
    assert (bare["altitude"], bare["climb"], bare["speed"], bare["distance"]) == ("--", "--", "--", "no position")
    parked = adsb_page.flight_fields(Aircraft("ABCDEF", callsign="N512PS", on_ground=True, vert_rate_fpm=0,
                                              lat=-33.9, lon=151.2))
    assert parked["altitude"] == "GROUND" and parked["climb"] == "level" and parked["distance"] == "33.90S 151.20E"
    assert parked["airline"] == "Private (US reg.)"
    assert set(adsb_page.flight_fields(None)) == set(fields)


@pytest.mark.parametrize("theme", config.THEME_NAMES)
def test_aircraft_colours(theme):
    style.apply_theme(theme)
    try:
        bottom, middle, top = style.ALTITUDE
        assert len({bottom, middle, top, style.TEXT_DIM, style.BG}) == 5, "the altitude colours must differ"
        low, high = config.ADSB_ALT_BANDS_FT
        colour = adsb_page.aircraft_colour
        assert colour(Aircraft("A", altitude_ft=low - 1), False) == bottom
        assert colour(Aircraft("A", altitude_ft=low), False) == middle
        assert colour(Aircraft("A", altitude_ft=high), False) == top
        assert colour(Aircraft("A", on_ground=True), False) == colour(Aircraft("A"), False) == style.TEXT_DIM
        assert colour(Aircraft("A", altitude_ft=30000, squawk="7700"), False) == style.REC
        assert colour(Aircraft("A", altitude_ft=30000, lat=1.0, lon=1.0, seen_pos_s=config.ADSB_STALE_S + 1),
                      False) == style.TEXT_DIM
        assert colour(Aircraft("A", altitude_ft=30000, squawk="7700"), True) == style.TEXT
    finally:
        style.apply_theme(style.DEFAULT_THEME)


def test_every_kind_of_aircraft_can_be_drawn(window, qapp, chart):
    fleet = [Aircraft(f"{0xA00000 + i:06X}", callsign=f"TST{i}", lat=HOME[0] + 0.05 * i, lon=HOME[1] - 0.05 * i,
                      altitude_ft=3000 * i, speed_kt=200.0, track_deg=None if i == 3 else 40.0 * i,
                      category=category, on_ground=i == 5, squawk="7700" if i == 6 else "")
             for i, category in enumerate(["", *adsb.CATEGORIES])]
    assert {ac.kind for ac in fleet} == {"jet", "heavy", "small", "light", "heli", "other"}
    window.state.set_aircraft(fleet, 50)
    pump(qapp, 0.05)
    assert len(chart.radar._hits) == len(fleet)
    for _ in fleet:
        window.knob_turned(1)
        pump(qapp, 0.01)


def test_trails_grow_stay_bounded_and_break_on_a_jump(window, qapp, chart):
    traffic = window.backend._traffic
    icao = chart.order[1].icao
    for _ in range(config.ADSB_TRAIL_POINTS + 20):
        traffic._t0 -= 1.0                             # one second of flight per poll
        window.backend.poll_adsb()
    trail = chart.radar.trails[icao]
    assert len(trail) == config.ADSB_TRAIL_POINTS
    assert set(chart.radar.trails) == {ac.icao for ac in window.state.aircraft if ac.has_position}
    pump(qapp, 0.05)
    traffic._t0 -= 1500.0                              # a long silence: everyone is somewhere else
    window.backend.poll_adsb()
    assert len(chart.radar.trails[icao]) == 1
    window.state.set_aircraft([])
    assert chart.radar.trails == {} and chart.order == []
    pump(qapp, 0.05)
    assert chart.radar._overlay()[0] == "Listening on 1090 MHz..."


def test_the_sweep_copies_the_aircraft_and_only_a_change_draws_them(window, qapp, chart, monkeypatch):
    """Drawn 12 times a second, the trails kept the GUI busy enough on the Pi to break up the audio."""
    radar, state = chart.radar, window.state
    drawn: list[int] = []
    draw_aircraft, draw_trails = radar._paint_aircraft, radar._paint_trails
    monkeypatch.setattr(radar, "_paint_aircraft", lambda p: (drawn.append(1), draw_aircraft(p))[1])
    monkeypatch.setattr(radar, "_paint_trails", lambda p: (drawn.append(2), draw_trails(p))[1])

    def painted() -> list[int]:
        del drawn[:]
        radar.grab()
        return list(drawn)

    for _ in range(3):
        window.backend._traffic._t0 -= 1.0
        window.backend.poll_adsb()                     # (so that there are trails)
    assert painted() == [2, 1]
    before = radar.grab().toImage()
    for _ in range(5):
        radar._advance_sweep()
        assert painted() == [], "a sweep step drew the aircraft again"
    assert radar.grab().toImage() != before, "the sweep did not move"
    assert len(radar._hits) >= 4, "taps must still find the aircraft"
    window.backend.poll_adsb()                         # the once-a-second update
    assert painted() == [2, 1] and painted() == []
    icao, x, y = radar._hits[0]
    radar.picked.emit(icao)                            # a tap: the selection is drawn
    assert chart.selected == icao and painted() == [2, 1]
    state.cycle_adsb_labels()
    assert painted() == [2, 1]
    state.toggle_adsb_trails()
    assert painted() == [1]
    state.toggle_adsb_trails()
    state.step_adsb_range(-1)
    assert painted() == [2, 1]
    radar.resize(radar.width() - 20, radar.height())
    assert painted() == [2, 1]
    state.adsb_error = "dump1090-fa stopped: lost the dongle"      # read at every repaint, not cached
    assert painted() == [] and radar._overlay()[0].startswith("dump1090-fa stopped")


def test_map_without_a_location_centres_on_the_traffic(window, qapp):
    window._enter_adsb()
    page = window.adsb_page
    assert page.radar.centre is None and page.radar._overlay()[0].startswith("Listening")
    pump(qapp, 0.05)                                   # draws with nothing to centre on
    window.state.set_aircraft([Aircraft("ABCDEF", callsign="N1")], 10)
    assert "waiting for its position" in page.radar._overlay()[0]
    window.backend.poll_adsb()
    centre = page.radar.centre
    assert page.radar.centre_source == "traffic" and centre is not None
    assert page.radar.notes()[0] == "CENTRED ON TRAFFIC" and page.radar.place == ""
    assert adsb.distance_bearing(*centre, *config.ADSB_SIM_HOME)[0] < 150
    assert all(ac.distance_nm is None for ac in page.order), "no receiver position: no distances to show"
    assert all(page.flight_list.cells(ac)[2] == "--" for ac in page.order)
    assert page.panel.fields["distance"].endswith("W") and "nm" not in page.panel.fields["distance"]
    window.backend._traffic._t0 -= 300.0
    window.backend.poll_adsb()
    assert page.radar.centre == centre, "the map must not slide about with the traffic"
    window.key_pressed(0)
    window.key_pressed(0)
    pump(qapp, 0.05)
    assert len(page.radar._hits) >= 5
    window.key_pressed(3)


def test_a_location_guessed_from_the_ip_address_is_named_and_flagged(window, qapp):
    window.state.space_wx = SpaceWx(wx={"lat": 43.0, "lon": -88.0, "city": "Milwaukee", "geo": "ip"})
    window._enter_adsb()
    window.backend.poll_adsb()
    radar = window.adsb_page.radar
    assert radar.centre_source == "ip" and radar.place == "MILWAUKEE"
    assert radar.notes() == ["CENTRE FROM IP ADDRESS", "SET A ZIP IN SETTINGS"]
    pump(qapp, 0.05)
    window.key_pressed(3)


def test_decoder_failure_is_shown_on_the_map(window, qapp, chart, monkeypatch):
    monkeypatch.setattr(window.backend, "_hw_adsb_poll",
                        lambda: ([], 120, "dump1090-fa stopped: rtlsdr: device disconnected"))
    window.backend.poll_adsb()
    pump(qapp, 0.05)
    text = chart.radar._overlay()[0]
    assert "device disconnected" in text and "Press BACK" in text
    assert window.stack.currentIndex() == window.PAGE_ADSB and window.state.adsb_mode
    window.key_pressed(3)
    assert window.stack.currentIndex() == window.PAGE_MAIN and window.state.adsb_error == ""


def test_no_decoder_means_a_message_and_an_ordinary_band_change(one_dongle_window, qapp, monkeypatch):
    window = one_dongle_window
    monkeypatch.setattr(window.backend, "_hw_adsb", lambda on: "No ADS-B decoder (dump1090) found - run install.sh")
    window.show_page(window.PAGE_BAND)
    window.band_page.buttons[adsb_tile(window)].click()
    assert window.stack.currentIndex() == window.PAGE_MAIN and not window.state.adsb_mode
    assert window.state.band.name == "ADS-B 1090" and window.backend.running
    assert window.toast.text().startswith("No ADS-B decoder")


def test_everything_on_the_page_fits(window, qapp, chart):
    radar, panel = chart.radar, chart.panel
    bottom = chart.mapTo(window, QtCore.QPoint(0, chart.height())).y()
    assert bottom == window.height(), "the page has the soft keys' room as well"
    assert radar.width() + panel.width() == chart.width() and radar.height() == panel.height() == chart.height()
    assert radar.radius() * 2 + 2 * adsb_page.EDGE_PX == min(radar.width(), radar.height())
    buttons = [radar.zoom_in, radar.zoom_out, radar.labels_btn, radar.trails_btn, radar.ground_btn, radar.back_btn]
    for button in buttons:
        box = button.geometry()
        assert radar.rect().contains(box) and button.isVisible(), button.text()
        assert not any(box.intersects(other.geometry()) for other in buttons if other is not button)
        assert any(QtCore.QRectF(box) == kept for kept in radar._reserved()), "labels must keep clear of the buttons"
    centre = QtCore.QPoint(radar.width() // 2, radar.height() // 2)
    assert not any(button.geometry().adjusted(-40, -40, 40, 40).contains(centre) for button in buttons)
    assert panel.rect().contains(panel.list_btn.geometry())
    window.key_pressed(2)
    pump(qapp, 0.05)
    table = chart.flight_list
    assert table.size() == chart.size() and table.row_height() >= 30 and not window.softkeys.isVisible()
    header = [table.radar_btn, *table.sort_buttons.values(), table.previous, table.following]
    for button in header:
        assert table.rect().contains(button.geometry()) and button.geometry().bottom() < table.HEADER_PX
        assert not any(button.geometry().intersects(other.geometry()) for other in header if other is not button)


def test_sweep_turns_only_while_the_radar_is_on_screen(window, qapp, chart):
    radar = chart.radar
    assert radar._sweep.isActive()
    start = radar._sweep_deg
    pump(qapp, 0.4)
    turned = (radar._sweep_deg - start) % 360
    assert 0.4 * 360 / config.ADSB_SWEEP_S * 0.5 < turned < 0.4 * 360 / config.ADSB_SWEEP_S * 2.0
    window.key_pressed(2)                              # LIST
    pump(qapp, 0.02)
    assert not radar._sweep.isActive()
    window.key_pressed(2)                              # RADAR
    pump(qapp, 0.02)
    assert radar._sweep.isActive()
    window.key_pressed(3)
    pump(qapp, 0.02)
    assert not radar._sweep.isActive()


# --------------------------------------------------------------------------- the flight list
def test_list_key_shows_the_flights_as_a_table(window, qapp, chart):
    table = chart.flight_list
    window.key_pressed(2)
    pump(qapp, 0.05)
    assert chart.view == "list" and table.isVisible() and not chart.radar.isVisible()
    assert keys(window) == ["SORT DIST", "PAGE", "RADAR", "BACK"]
    assert table.flights == chart.order and len(table.flights) == 13
    assert table.pages == 2 and table.page == 0 and len(table.visible()) == config.ADSB_LIST_ROWS
    assert table.shown == chart.shown == chart.order[0].icao
    colours, _ = image_stats(window)
    assert colours > 20
    united = next(ac for ac in chart.order if ac.callsign == "UAL1482")
    assert table.cells(united) == ("UAL1482", "UNITED AIRLINES", f"{united.distance_nm:.0f}", "36000", "452")
    parked = next(ac for ac in chart.order if ac.on_ground)
    assert table.cells(parked)[3] == "GND"
    assert table.cells(Aircraft("ABCDEF")) == ("ABCDEF", "NO CALLSIGN YET", "--", "--", "--")
    window.key_pressed(1)                              # PAGE
    assert table.page == 1 and len(table.visible()) == 3
    window.key_pressed(1)
    assert table.page == 0                             # wraps
    table.following.click()
    assert table.page == 1
    table.previous.click()
    table.previous.click()
    assert table.page == 1
    pump(qapp, 0.05)
    window.key_pressed(2)                              # RADAR
    assert chart.view == "radar" and keys(window)[2] == "LIST" and chart.radar.isVisible()
    chart.panel.list_btn.click()
    assert chart.view == "list" and keys(window)[2] == "RADAR"
    chart.flight_list.radar_btn.click()                # on the screen, as there is no soft-key bar
    assert chart.view == "radar" and chart.radar.isVisible()
    chart.panel.list_btn.click()
    window.key_pressed(3)
    assert not window.state.adsb_mode
    window._enter_adsb()
    assert chart.view == "radar", "the map always opens on the radar"
    window.key_pressed(3)


def test_list_sorts_by_distance_altitude_or_airline(window, qapp, chart):
    table = chart.flight_list
    window.key_pressed(2)
    assert table.sort_buttons["DIST"].property("lit")
    window.key_pressed(0)
    assert chart.sort == "ALT" and keys(window)[0] == "SORT ALT" and table.sort_buttons["ALT"].property("lit")
    assert not table.sort_buttons["DIST"].property("lit")
    flights = table.flights
    assert flights[0].on_ground
    heights = [ac.altitude_ft for ac in flights if not ac.on_ground]
    assert heights == sorted(heights) and len(flights) == len(chart.order)
    window.key_pressed(0)
    assert chart.sort == "AIRLINE"
    names = [table.cells(ac)[1] for ac in table.flights if ac.callsign]
    assert names == sorted(names) and names[0] == "AIR CANADA"
    assert not table.flights[-1].callsign, "no callsign yet: at the end"
    table.sort_buttons["DIST"].click()
    assert chart.sort == "DIST" and table.flights == chart.order and keys(window)[0] == "SORT DIST"
    lost = [Aircraft("A00001", callsign="B"), Aircraft("A00002", callsign="A", altitude_ft=500)]
    assert [ac.icao for ac in adsb_page.sorted_flights(lost, "ALT")] == ["A00002", "A00001"]
    window.key_pressed(3)
    window._enter_adsb()
    window.key_pressed(2)
    assert chart.sort == "DIST" or keys(window)[0] == f"SORT {chart.sort}"


def test_knob_and_taps_in_the_list(window, qapp, chart):
    table = chart.flight_list
    window.key_pressed(2)
    window.key_pressed(0)                              # by altitude
    pump(qapp, 0.05)
    flights = table.flights
    window.knob_turned(1)                              # holds the one AUTO was showing
    assert chart.selected == chart.order[0].icao and table.shown == chart.selected
    index = [ac.icao for ac in flights].index(chart.selected)
    for step in range(1, len(flights)):
        window.knob_turned(1)
        expected = (index + step) % len(flights)
        assert chart.selected == flights[expected].icao
        assert table.page == expected // config.ADSB_LIST_ROWS, "the list did not turn to the selection"
    window.encoder_pushed()                            # show it on the radar
    assert chart.view == "radar" and chart.selected == flights[index - 1].icao and not chart.panel.auto
    assert chart.panel.fields["callsign"] == flights[index - 1].name

    window.key_pressed(2)                              # the list opens at the page of the selection
    pump(qapp, 0.05)
    assert table.page == 1 and len(table.visible()) == 3
    target = table.visible()[1]
    tap(table, QtCore.QPoint(200, int(table.HEADER_PX + 1.5 * table.row_height())))
    assert chart.selected == target.icao and chart.view == "radar"
    window.key_pressed(2)
    pump(qapp, 0.05)
    held = chart.selected
    assert table.page == 1
    tap(table, QtCore.QPoint(200, int(table.HEADER_PX + 6.5 * table.row_height())))
    assert chart.selected == held and chart.view == "list", "a tap below the last line does nothing"
    tap(table, QtCore.QPoint(table.width() // 2, 20))  # the heading is not a line either
    assert chart.selected == held


# --------------------------------------------------------------------------- real decoder
def _real_decoders() -> list[str]:
    named = [path for path in os.environ.get("PISDR_TEST_DUMP1090", "").split(os.pathsep) if path]
    found = adsb_decoder.find_program()
    return named or ([found] if found else [])


@pytest.mark.parametrize("exe", _real_decoders() or [None], ids=lambda exe: Path(exe).name if exe else "none")
def test_real_dump1090_decodes_a_synthetic_recording(exe, tmp_path, monkeypatch):
    """End to end: Mode S frames -> I/Q file -> a real dump1090 -> aircraft.json -> Aircraft."""
    if exe is None:
        pytest.skip("no dump1090 installed (set PISDR_TEST_DUMP1090 to the path of one)")
    frames = (adsb_signal.frames_for(0xA1B2C3, "UAL123", 43.20, -88.05, 35000, 452, 273, -1216)
              + adsb_signal.frames_for(0x40621D, "KLM1023", 43.85, -87.10, 12025, 250, 45, 640)
              + [adsb_signal.identification(0xABCDEF, "N123PI")])
    recording = tmp_path / "adsb.iq"
    adsb_signal.write_iq(recording, frames, seconds=3.0)
    # A wrapper with the decoder's own name, so the app's command line is used unchanged and
    # the recording is appended as the signal source instead of the dongle.
    name = Path(exe).name
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script(bin_dir, name, f'exec {exe} "$@" --ifile {recording} --iformat UC8 --throttle\n')
    monkeypatch.setattr(config, "ADSB_PROGRAMS", (name,))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(adsb_decoder, "_HELP", {})

    decoder = adsb_decoder.AdsbDecoder()
    assert decoder.start(ppm=1, position=HOME) is None
    try:
        found: dict[str, Aircraft] = {}
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            aircraft, messages, error = decoder.read()
            found.update({ac.icao: ac for ac in aircraft})
            complete = [ac for ac in found.values() if ac.has_position and ac.speed_kt and ac.callsign]
            if error or (len(complete) == 2 and len(found) == 3):
                break
            time.sleep(0.2)
    finally:
        decoder.stop()
    assert set(found) == {"A1B2C3", "40621D", "ABCDEF"}, f"decoded {sorted(found)} ({error or 'no error'})"
    assert messages > 100
    united, klm, quiet = found["A1B2C3"], found["40621D"], found["ABCDEF"]
    assert (united.callsign, klm.callsign, quiet.callsign) == ("UAL123", "KLM1023", "N123PI")
    assert (united.lat, united.lon) == pytest.approx((43.20, -88.05), abs=0.001)
    assert (klm.lat, klm.lon) == pytest.approx((43.85, -87.10), abs=0.001)
    assert (united.altitude_ft, klm.altitude_ft) == (35000, 12025)
    assert united.speed_kt == pytest.approx(452, abs=1.5) and united.track_deg == pytest.approx(273, abs=1.0)
    assert klm.speed_kt == pytest.approx(250, abs=1.5) and klm.track_deg == pytest.approx(45, abs=1.0)
    assert (united.vert_rate_fpm, klm.vert_rate_fpm) == (-1216, 640)
    assert not quiet.has_position and quiet.altitude_ft is None
    adsb.locate([united, klm], *HOME)
    assert united.distance_nm == pytest.approx(11.4, abs=0.1) and klm.distance_nm == pytest.approx(60.1, abs=0.1)
    assert adsb.compass_point(united.bearing_deg) == "NW" and adsb.compass_point(klm.bearing_deg) == "NE"
