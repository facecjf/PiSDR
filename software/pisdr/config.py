"""Static configuration for PiSDR.

Everything a builder is likely to change lives here: GPIO pins, tuning steps, demodulator
bandwidths, sample rates and file locations. Runtime state (current frequency, mode, ...)
lives in :mod:`pisdr.state`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# ----------------------------------------------------------------------------- paths
PACKAGE_DIR = Path(__file__).resolve().parent
SOFTWARE_DIR = PACKAGE_DIR.parent
BANDS_FILE = SOFTWARE_DIR / "config" / "bands.json"
PRESET_MEMORIES_FILE = SOFTWARE_DIR / "config" / "presets_numbers_stations.json"
USER_CONFIG_DIR = Path(os.environ.get("PISDR_CONFIG_DIR", Path.home() / ".config" / "pisdr"))
SETTINGS_FILE = USER_CONFIG_DIR / "settings.json"
MEMORIES_FILE = USER_CONFIG_DIR / "memories.json"
SCAN_LOG_FILE = USER_CONFIG_DIR / "scan_log.json"
LOG_FILE = USER_CONFIG_DIR / "pisdr.log"
SPACEWX_FILE = USER_CONFIG_DIR / "spacewx.json"   # last good solar / weather data
RECORDINGS_DIR = Path(os.environ.get("PISDR_RECORDINGS_DIR", Path.home() / "Recordings"))

# ----------------------------------------------------------------------------- screen
SCREEN_W = 800
SCREEN_H = 480
THEME_NAMES = ("Amber", "Night", "Ice", "Daylight", "Inferno", "Green")   # must match ui/style.py THEMES
DEFAULT_THEME = "Amber"

# ----------------------------------------------------------------------------- GPIO (BCM)
# Swap ENCODER_A_GPIO / ENCODER_B_GPIO if the knob tunes backwards.
ENCODER_A_GPIO = 27      # EC11 module DT,  physical pin 13
ENCODER_B_GPIO = 17      # EC11 module CLK, physical pin 11
ENCODER_SW_GPIO = 22     # EC11 module SW,  physical pin 15
KEY_GPIOS = (23, 24, 25, 5)   # K1..K4, physical pins 16, 18, 22, 29
KEY_BOUNCE_S = 0.03
KEY_HOLD_S = 0.8         # long press threshold

# ----------------------------------------------------------------------------- radio
SAMPLE_RATE = 1_920_000      # RTL-SDR sample rate; 1.92 MS/s divides cleanly to 240 k and 48 k
IF_RATE = 240_000            # after the translating filter (SAMPLE_RATE / 8)
AUDIO_RATE = 48_000          # IF_RATE / 5
FREQ_MIN = 500_000           # RTL-SDR Blog V4 lower limit without direct sampling
FREQ_MAX = 1_766_000_000
DEFAULT_GAIN_DB = 30.0
GAIN_MIN_DB = 0.0
GAIN_MAX_DB = 49.6           # R828D range reported by librtlsdr
GAIN_STEP_DB = 1.0
SQUELCH_MIN_DB = -100.0
SQUELCH_MAX_DB = -20.0
SQUELCH_STEP_DB = 1.0
VOLUME_STEP = 2              # percent per encoder detent
FFT_SIZE = 1024
FFT_FPS = 20
FFT_FPS_MIN, FFT_FPS_MAX = 5, 40
WATERFALL_ROWS = 170
WATERFALL_SPEED_MAX = 4      # rows added per frame
SPECTRUM_UI_MS = 50          # coalesce GUI redraws (~20 fps max), drop intermediate frames
FUNC_RETURN_S = 4.0          # knob returns to TUNE after this many idle seconds
RTL_DEVICE_ARGS = "rtl=0"    # append ",bias=1" for the bias tee (handled in the backend)
PPM_CORRECTION = 0           # default for Settings > RADIO > PPM; the V4 TCXO is ~1 ppm
PPM_MIN, PPM_MAX = -100, 100
SMETER_CAL_DB = -60.0        # default offset from IF power (dBFS) to the S-meter reading
SMETER_CAL_MIN_DB, SMETER_CAL_MAX_DB = -100.0, -20.0
SIGNAL_POLL_MS = 100

# Scanning: dwell on a quiet channel, pause when S-meter rises, resume when it falls.
# DWELL / RESUME / FLOOR are defaults; Settings > SCAN changes them at run time.
SCAN_TICK_MS = 80
SCAN_DWELL_MS = 220          # quiet time on a channel before stepping
SCAN_DWELL_CHOICES_MS = (80, 120, 160, 220, 300, 500, 1000)
SCAN_HOLD_MS = 120           # signal must stay above threshold this long to pause
SCAN_RESUME_MS = 2500        # quiet time after a hit before scanning continues
SCAN_RESUME_CHOICES_MS = (1000, 2500, 5000, 10000, 0)   # 0 = stay until SKIP / RESUME
SCAN_FLOOR_DB = -75.0        # used when squelch is fully open, so band scan still finds signals
SCAN_FLOOR_MIN_DB, SCAN_FLOOR_MAX_DB = -110.0, -30.0
SCAN_LOG_MAX = 300           # hits kept in scan_log.json (newest first)
MAX_LOCKOUTS = 500
PRIORITY_INTERVAL_CHOICES_S = (2, 3, 5, 10)
PRIORITY_LOOK_MS = 350       # how long the scanner listens on the priority channel each visit
PRIORITY_SETTLE_MS = 120     # ignore the S-meter this long after jumping (stale level)
MAX_MEMORIES = 100
MEMORY_GROUP_ALL = "ALL"

# Recording: 16-bit mono at AUDIO_RATE is about 5.8 MB a minute. The recorder keeps clear
# of a full card, which on a Pi means a system that no longer boots cleanly.
REC_BYTES_PER_S = AUDIO_RATE * 2
REC_MIN_FREE_MB = 200        # refuse to start a recording with less free space than this
REC_STOP_FREE_MB = 100       # stop a running recording when free space falls to this
REC_SPACE_CHECK_MS = 10_000

# Record timer (UTC, because the schedules it is for are published in UTC).
REC_TIMER_LENGTH_CHOICES_MIN = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
REC_TIMER_CHECK_MS = 5_000

# Screen. The Hosyond / official 7" DSI panels expose /sys/class/backlight/*; HDMI does not.
BACKLIGHT_DIR = Path("/sys/class/backlight")
BRIGHTNESS_MIN_PCT = 10
DIM_AFTER_CHOICES_S = (0, 30, 60, 120, 300, 600)   # 0 = never dim
DIM_LEVEL_PCT = 10

# Knob acceleration: detents per second -> step multiplier while tuning.
KNOB_ACCEL_WINDOW_S = 0.25
KNOB_ACCEL_STAGES = ((30.0, 10), (14.0, 4))

# Spectrum view. Zoom is done by a bigger FFT cropped around the VFO, so it adds real detail.
SPECTRUM_ZOOMS = (1, 2, 4, 8, 16, 32, 64)   # x64 = 30 kHz view, 65536-point FFT, ~29 Hz bins
SPECTRUM_AVG_NAMES = ("OFF", "LOW", "MID", "HIGH")
SPECTRUM_AVG_ALPHA = (1.0, 0.6, 0.4, 0.2)   # weight of the newest frame
SPECTRUM_STYLES = ("FILLED", "LINE")
WATERFALL_PALETTES = ("INFERNO", "THEME", "CLASSIC", "GRAY")   # THEME = the colour theme's own
SPECTRUM_LABELS = ("BOTH", "SIGNALS", "TAGS", "OFF")
SNAP_MODES = ("TUNE", "TUNE+FILTER", "OFF")   # what a tap near a detected signal does

# Signal detection on the spectrum (markers, snap-to-signal, seek)
SIGNAL_MIN_SNR_DB = 8.0        # peak must be this far above the local noise floor
SIGNAL_EDGE_DB = 4.0           # a signal's extent: bins this far above the floor
SIGNAL_MAX_MARKERS = 10
SIGNAL_DETECT_EVERY = 4        # frames between detection runs
SIGNAL_SNAP_PX = 22            # a tap this close (screen pixels) to a signal snaps to it
SSB_SNAP_MAX_BIN_HZ = 250.0    # SSB edges are only meaningful when zoomed this far

# Frequency tags drawn on the spectrum
TAGS_FILE = SOFTWARE_DIR / "config" / "tags.json"   # built-in list of well-known frequencies
USER_TAGS_FILE = USER_CONFIG_DIR / "tags.json"      # your own + FM names learned from RDS
RDS_TAG_STABLE_S = 5.0         # RDS station name must stay unchanged this long to be learned
MAX_TAG_LABELS = 18
DEFAULT_SPECTRUM_AVG = 2

# Data decoders: demodulated audio -> 22.05 kHz s16 -> multimon-ng. Most expect NFM audio.
DECODERS: dict[str, tuple[str, ...]] = {
    "OFF": (),
    "APRS": ("-a", "AFSK1200"),
    "POCSAG": ("-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400"),
    "FLEX": ("-a", "FLEX"),
    "EAS": ("-a", "EAS"),
    "DTMF": ("-a", "DTMF"),
}
DECODER_RATE = 22_050
DECODER_TEXT_MAX = 40        # decoded lines kept for the strip on the main page

# Network mode: hand the dongle to rtl_tcp so SDR++ / SDR# / GQRX can use it over Wi-Fi.
RTL_TCP_PORT = 1234

# ADS-B map: the dongle is lent to a dump1090-family decoder, which writes the aircraft it
# is tracking to aircraft.json once a second. First program found on PATH is used.
ADSB_FREQ_HZ = 1_090_000_000
ADSB_PROGRAMS = ("dump1090-fa", "dump1090-mutability", "readsb", "dump1090")
ADSB_POLL_MS = 1000
ADSB_MAX_AGE_S = 60            # an aircraft not heard for this long leaves the list
ADSB_POSITION_MAX_AGE_S = 60   # ... and a position this old leaves the map
ADSB_STALE_S = 15              # drawn dimmed once its position is older than this
ADSB_RANGES_NM = ((10, 5), (25, 5), (50, 10), (100, 25), (150, 50), (250, 50))   # (range, ring spacing)
ADSB_DEFAULT_RANGE_NM = 100
ADSB_LABELS = ("FULL", "NAME", "OFF")   # callsign + flight level / callsign / selected aircraft only
ADSB_TRAIL_POINTS = 180        # positions remembered per aircraft for its trail (about 3 minutes)
ADSB_PANEL_W = 268             # aircraft list beside the map
ADSB_SIM_HOME = (43.04, -87.91)   # where the simulator's traffic flies if no location is set
OUTLINES_FILE = SOFTWARE_DIR / "config" / "outlines.npz"   # coastlines and borders for the map
ZIPCODES_FILE = SOFTWARE_DIR / "config" / "zipcodes.npz"   # US ZIP code -> location (Settings > Location)

# Post-demod audio DSP. HPF/LPF/notch are IIR biquads (C++); NR/NB are Python blocks that
# are only spliced into the audio path while their level is > 0.
DSP_HPF_HZ_DEFAULT = 250
DSP_HPF_HZ_MIN = 50
DSP_HPF_HZ_MAX = 500
DSP_LPF_HZ_DEFAULT = 4_000
DSP_LPF_HZ_MIN = 1_500
DSP_LPF_HZ_MAX = 8_000
DSP_NOTCH_HZ_DEFAULT = 1_000
DSP_NOTCH_HZ_MIN = 300
DSP_NOTCH_HZ_MAX = 3_500
DSP_NOTCH_WIDTH_HZ = 80
DSP_LEVEL_MAX = 10
DSP_NR_FFT = 512
DSP_NR_HOP = 256

# CW decoder: audio tone at CW_PITCH_HZ -> band-pass -> magnitude -> LPF -> decimate.
CW_ENVELOPE_DECIM = 48       # 48 kHz / 48 = 1 kHz envelope into the Python decoder
CW_ENVELOPE_RATE = AUDIO_RATE // CW_ENVELOPE_DECIM
CW_DECODE_BW_HZ = 200        # band-pass width around the pitch for detection
CW_TEXT_MAX = 400            # characters kept in the decoded-text buffer

# HF conditions panel (solar-terrestrial + local weather). All feeds are free, no API key.
HF_PANEL_MODES = ("OFF", "ON", "AUTO")   # AUTO = shown while tuned below HF_AUTO_MAX_HZ
HF_AUTO_MAX_HZ = 30_000_000
HF_PANEL_W = 300
SPACEWX_SOLAR_URL = "https://www.hamqsl.com/solarxml.php"            # N0NBH: SFI/A/K/bands
SPACEWX_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SPACEWX_GEOIP_URL = "http://ip-api.com/json/?fields=status,lat,lon,city"   # only if no wx_lat/lon
SPACEWX_WX_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                  "&current=temperature_2m,relative_humidity_2m,weather_code,pressure_msl,"
                  "wind_speed_10m,wind_direction_10m&temperature_unit={temp_unit}"
                  "&wind_speed_unit={wind_unit}&timezone=auto")
SPACEWX_PERIOD_S = {"solar": 3600, "kp": 600, "wx": 900}   # polite polling intervals
SPACEWX_RETRY_S = 120
SPACEWX_TIMEOUT_S = 10
SPACEWX_USER_AGENT = "PiSDR/1.0 (+https://github.com/facecjf/PiSDR)"

# Tuning steps cycled by the encoder push button (Hz).
STEPS_HZ = (10, 100, 1_000, 5_000, 10_000, 12_500, 25_000, 100_000, 1_000_000)
DEFAULT_STEP_INDEX = 2

MODES = ("AM", "NFM", "WFM", "USB", "LSB", "CW")


@dataclass(frozen=True)
class ModeSpec:
    name: str
    bandwidth_hz: float      # passband width drawn on the spectrum and used by the demod
    offset_hz: float = 0.0   # passband centre relative to the VFO (SSB/CW are one-sided)
    has_squelch: bool = False


MODE_SPECS = {
    "AM": ModeSpec("AM", 8_000),
    "NFM": ModeSpec("NFM", 12_500, has_squelch=True),
    "WFM": ModeSpec("WFM", 180_000, has_squelch=True),
    "USB": ModeSpec("USB", 2_400, offset_hz=+1_500),
    "LSB": ModeSpec("LSB", 2_400, offset_hz=-1_500),
    "CW": ModeSpec("CW", 500, offset_hz=0),
}
CW_PITCH_HZ = 700.0
CW_PITCH_MIN_HZ, CW_PITCH_MAX_HZ, CW_PITCH_STEP_HZ = 400, 1000, 50
SSB_LOW_CUT_HZ = 300.0
SSB_HIGH_CUT_HZ = 2_700.0

# Selectable filter widths per mode (Hz). The default index reproduces MODE_SPECS.
FILTER_WIDTHS: dict[str, tuple[int, ...]] = {
    "AM": (4_000, 6_000, 8_000, 10_000),
    "NFM": (7_500, 10_000, 12_500, 16_000),
    "WFM": (180_000,),
    "USB": (1_800, 2_100, 2_400, 2_700, 3_000),
    "LSB": (1_800, 2_100, 2_400, 2_700, 3_000),
    "CW": (200, 300, 500, 800, 1_200),
}
DEFAULT_FILTER_INDEX = {m: FILTER_WIDTHS[m].index(int(MODE_SPECS[m].bandwidth_hz)) for m in FILTER_WIDTHS}

KNOB_FUNCTIONS = ("TUNE", "VOLUME", "SQUELCH", "GAIN")


@dataclass(frozen=True)
class Band:
    name: str
    start_hz: int
    stop_hz: int
    default_hz: int
    mode: str
    step_hz: int

    def contains(self, hz: float) -> bool:
        return self.start_hz <= hz <= self.stop_hz


def load_bands(path: Path = BANDS_FILE) -> list[Band]:
    """Read the band table; fall back to a tiny built-in list if the file is missing."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        bands = [
            Band(b["name"], int(b["start"]), int(b["stop"]), int(b["default"]), b["mode"], int(b["step"]))
            for b in data["bands"]
        ]
        if bands:
            return bands
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return [
        Band("FM bcast", 87_500_000, 108_000_000, 100_100_000, "WFM", 100_000),
        Band("2 m", 144_000_000, 148_000_000, 146_520_000, "NFM", 5_000),
    ]


def step_index_for(step_hz: int) -> int:
    """Index in STEPS_HZ closest to *step_hz*."""
    return min(range(len(STEPS_HZ)), key=lambda i: abs(STEPS_HZ[i] - step_hz))
