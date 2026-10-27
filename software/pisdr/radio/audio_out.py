"""Open a GNU Radio audio sink, trying every device that might actually work.

GNU Radio talks to ALSA, not to PipeWire. ``wpctl set-default`` therefore has no effect
unless the ``pipewire-alsa`` package makes ALSA's ``default`` device a PipeWire client.
On a Pi with a USB dongle and nothing in the headphone jack, ``default`` is the onboard
jack and fails with ALSA error 524. This helper falls through to the USB card directly.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)

_CARD_RE = re.compile(r"^card \d+: (\S+)")


def alsa_cards() -> list[str]:
    """Short ALSA card ids from ``aplay -l``, e.g. ``['Headphones', 'Device']``."""
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _CARD_RE.findall(out.stdout)


def candidate_devices() -> list[str]:
    """Devices to try, most desirable first. ``PISDR_AUDIO_DEVICE`` always wins."""
    forced = os.environ.get("PISDR_AUDIO_DEVICE", "").strip()
    cards = alsa_cards()
    usb = [c for c in cards if "bcm2835" not in c.lower() and "headphone" not in c.lower()]
    onboard = [c for c in cards if c not in usb]
    ordered = [forced, "default", "pipewire", "pulse"]
    ordered += [f"plughw:CARD={c},DEV=0" for c in usb + onboard]
    seen: set[str] = set()
    return [d for d in ordered if d and not (d in seen or seen.add(d))]


def open_sink(rate: int):
    """Return ``(sink, device_name)``. Raises RuntimeError listing every attempt."""
    from gnuradio import audio

    errors: list[str] = []
    for device in candidate_devices():
        try:
            sink = audio.sink(rate, device, True)
        except RuntimeError as exc:
            errors.append(f"{device}: {exc}")
            log.info("audio device %r failed: %s", device, exc)
            continue
        log.info("audio output: %s", device)
        return sink, device
    runtime = os.environ.get("XDG_RUNTIME_DIR", "(unset)")
    raise RuntimeError(
        "no ALSA playback device would open:\n  " + "\n  ".join(errors) +
        f"\nXDG_RUNTIME_DIR={runtime}. If every line is error 524 and you use PipeWire, "
        "install its ALSA bridge: sudo apt install pipewire-alsa, then retry. "
        "Over SSH also: export XDG_RUNTIME_DIR=/run/user/$(id -u)"
    )
