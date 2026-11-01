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
LOG_FILE = USER_CONFIG_DIR / "pisdr.log"
SPACEWX_FILE = USER_CONFIG_DIR / "spacewx.json"   # last good solar / weather data
RECORDINGS_DIR = Path(os.environ.get("PISDR_RECORDINGS_DIR", Path.home() / "Recordings"))

# ----------------------------------------------------------------------------- screen
SCREEN_W = 800
SCREEN_H = 480
THEME_NAMES = ("Amber", "Night", "Ice", "Daylight")   # must match ui/style.py THEMES
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
WATERFALL_ROWS = 170
SPECTRUM_UI_MS = 50          # coalesce GUI redraws (~20 fps max), drop intermediate frames
FUNC_RETURN_S = 4.0          # knob returns to TUNE after this many idle seconds
RTL_DEVICE_ARGS = "rtl=0"    # append ",bias=1" for the bias tee (handled in the backend)
PPM_CORRECTION = 0           # the V4 has a 1 ppm TCXO; leave 0 unless measured
SMETER_CAL_DB = -60.0        # added to the IF power (dBFS) to get the S-meter reading; tune by ear
SIGNAL_POLL_MS = 100

# Scanning: dwell on a quiet channel, pause when S-meter rises, resume when it falls.
SCAN_TICK_MS = 80
SCAN_DWELL_MS = 220          # quiet time on a channel before stepping
SCAN_HOLD_MS = 120           # signal must stay above threshold this long to pause
SCAN_RESUME_MS = 2500        # quiet time after a hit before scanning continues
SCAN_FLOOR_DB = -75.0        # used when squelch is fully open, so band scan still finds signals
MAX_MEMORIES = 100

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
SSB_LOW_CUT_HZ = 300.0
SSB_HIGH_CUT_HZ = 2_700.0

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
