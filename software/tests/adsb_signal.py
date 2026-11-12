"""Make ADS-B frames and a 1090 MHz I/Q recording of them, for testing against a real dump1090.

Nothing here is used by the application. :func:`write_iq` produces the file a dongle would
have recorded (2.4 MS/s, unsigned 8-bit I/Q) if the aircraft passed to it had been overhead,
so a real ``dump1090 --ifile`` can be run on it and its output compared with what went in.
"""
from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 2_400_000
_OVERSAMPLE = 10                       # build the pulses at 24 MS/s: one half-bit = 12 samples
_HALF_BIT = SAMPLE_RATE * _OVERSAMPLE // 2_000_000
_CRC_POLY = 0xFFF409
_CALLSIGN_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"


def crc24(data: bytes) -> int:
    """Mode S parity of *data* (the first 11 bytes of a long frame)."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1000000 | _CRC_POLY
    return crc & 0xFFFFFF


def frame(icao: int, me: int) -> bytes:
    """A DF17 extended squitter from *icao* carrying the 56-bit message *me*."""
    body = bytes([0x8D]) + icao.to_bytes(3, "big") + me.to_bytes(7, "big")
    return body + crc24(body).to_bytes(3, "big")


def identification(icao: int, callsign: str) -> bytes:
    me = 4 << 51                                           # type code 4, category 0
    for i, ch in enumerate(callsign.upper().ljust(8)[:8]):
        me |= (_CALLSIGN_CHARS.index(ch) if ch != " " else 32) << (42 - 6 * i)
    return frame(icao, me)


def _nl(lat: float) -> int:
    """Number of longitude zones at *lat* (DO-260B)."""
    if abs(lat) >= 87.0:
        return 1
    a = 1 - math.cos(math.pi / 30)
    return int(math.floor(2 * math.pi / math.acos(1 - a / math.cos(math.radians(lat)) ** 2)))


def cpr(lat: float, lon: float, odd: bool) -> tuple[int, int]:
    """Airborne compact position reporting: the 17-bit latitude and longitude fields."""
    dlat = 360.0 / (60 - odd)
    yz = math.floor(131072 * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / 131072 + math.floor(lat / dlat))
    dlon = 360.0 / max(_nl(rlat) - odd, 1)
    xz = math.floor(131072 * (lon % dlon) / dlon + 0.5)
    return yz % 131072, xz % 131072


def position(icao: int, lat: float, lon: float, altitude_ft: int, odd: bool) -> bytes:
    n = (altitude_ft + 1000) // 25
    alt = ((n >> 4) << 5) | 0x10 | (n & 0xF)               # 25 ft steps, Q bit set
    yz, xz = cpr(lat, lon, odd)
    me = (11 << 51) | (alt << 36) | (int(odd) << 34) | (yz << 17) | xz
    return frame(icao, me)


def velocity(icao: int, speed_kt: float, track_deg: float, vert_rate_fpm: int) -> bytes:
    east = round(speed_kt * math.sin(math.radians(track_deg)))
    north = round(speed_kt * math.cos(math.radians(track_deg)))
    me = (19 << 51) | (1 << 48)                            # type code 19, subtype 1: ground speed
    me |= (int(east < 0) << 42) | ((abs(east) + 1) << 32)
    me |= (int(north < 0) << 31) | ((abs(north) + 1) << 21)
    me |= (1 << 20)                                        # vertical rate is barometric
    me |= (int(vert_rate_fpm < 0) << 19) | ((abs(vert_rate_fpm) // 64 + 1) << 10)
    return frame(icao, me)


def frames_for(icao: int, callsign: str, lat: float, lon: float, altitude_ft: int,
               speed_kt: float, track_deg: float, vert_rate_fpm: int = 0) -> list[bytes]:
    """Everything one aircraft sends in a second or so."""
    return [identification(icao, callsign), velocity(icao, speed_kt, track_deg, vert_rate_fpm),
            position(icao, lat, lon, altitude_ft, odd=False), position(icao, lat, lon, altitude_ft, odd=True)]


def _envelope(message: bytes) -> np.ndarray:
    """Pulse envelope of one frame at the oversampled rate: 8 us preamble, then 1 us per bit."""
    halves = [1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0]
    for byte in message:
        for bit in range(7, -1, -1):
            halves += [1, 0] if (byte >> bit) & 1 else [0, 1]
    return np.repeat(np.array(halves, dtype=np.float32), _HALF_BIT)


def write_iq(path, frames: list[bytes], seconds: float = 4.0, gap_s: float = 0.004, seed: int = 1) -> None:
    """Write *seconds* of I/Q with *frames* repeated in turn every *gap_s*, over light noise."""
    rng = np.random.default_rng(seed)
    total = int(seconds * SAMPLE_RATE)
    level = np.zeros(total * _OVERSAMPLE, dtype=np.float32)
    step = int(gap_s * SAMPLE_RATE) * _OVERSAMPLE
    start, i = step, 0
    while start + 2400 * _OVERSAMPLE < len(level):
        pulse = _envelope(frames[i % len(frames)])
        level[start:start + len(pulse)] = pulse
        start += step + int(rng.integers(0, 7))            # not locked to the sample clock
        i += 1
    level = level.reshape(total, _OVERSAMPLE).mean(axis=1)
    phase = rng.uniform(0, 2 * np.pi)
    iq = np.empty((total, 2), dtype=np.float32)
    iq[:, 0] = 127.5 + 90.0 * level * np.cos(phase)
    iq[:, 1] = 127.5 + 90.0 * level * np.sin(phase)
    iq += rng.normal(0.0, 2.0, iq.shape)
    np.clip(iq, 0, 255).astype(np.uint8).tofile(str(path))
