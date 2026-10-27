#!/usr/bin/env python3
"""HF-RX 04: GPIO tuning controller for a Gqrx SDR on Raspberry Pi 4.

Gqrx: enable Tools -> Remote control -> Start server; restrict to 127.0.0.1:7356.
GPIO numbering is BCM, not physical pin numbers. Inputs use internal pulls.
RTL-SDR Blog V4: updated V4-compatible drivers; direct sampling OFF; LO offset 0.
EC11 bare encoder common -> GND, A -> GPIO17, B -> GPIO27.
EC11 integrated push -> GPIO22 / GND. Other buttons -> GPIO / GND.
"""
import socket
import threading
from signal import pause
from gpiozero import RotaryEncoder, Button

HOST = "127.0.0.1"
PORT = 7356
# Hz and default modulation. These are listening starting points, not transmit permissions.
PRESETS = [
    (3_900_000, "LSB", "80 m SSB"),
    (7_200_000, "LSB", "40 m SSB"),
    (10_000_000, "AM", "WWV 10 MHz"),
    (14_200_000, "USB", "20 m SSB"),
    (18_130_000, "USB", "17 m SSB"),
]
STEPS_HZ = (10, 100, 1000, 10000)
MODES = ("LSB", "USB", "AM", "CW")
state = {"preset": 0, "step": 1, "record": False}
lock = threading.RLock()


def cmd(command):
    """Send one line to Gqrx, return its single-line answer."""
    with socket.create_connection((HOST, PORT), timeout=1.5) as sock:
        sock.settimeout(1.5)
        sock.sendall((command + "\n").encode("ascii"))
        # All commands used here have one line of output.
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = sock.recv(256)
            if not chunk:
                raise RuntimeError("Gqrx closed the connection")
            response += chunk
            if len(response) > 1024:
                raise RuntimeError("Unexpected Gqrx response length")
        reply = response.decode("ascii", errors="replace").strip()
        return reply


def send_set(command):
    result = cmd(command)
    if result != "RPRT 0":
        raise RuntimeError(f"Gqrx rejected {command!r}: {result}")


def safe(task):
    try:
        with lock:
            task()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Gqrx not responding: {exc} — check that remote server is started", flush=True)


def tune(direction):
    def action():
        old_hz = int(cmd("f"))
        new_hz = max(500_000, min(30_000_000, old_hz + direction * STEPS_HZ[state['step']]))
        send_set(f"F {new_hz}")
        print(f"Tuned {new_hz / 1e6:.6f} MHz", flush=True)
    safe(action)


def band_next():
    def action():
        index = (state["preset"] + 1) % len(PRESETS)
        hz, mode, label = PRESETS[index]
        send_set(f"F {hz}")
        send_set(f"M {mode}")
        state["preset"] = index
        print(f"Preset: {label}: {hz / 1e6:.6f} MHz {mode}", flush=True)
    safe(action)


def mode_next():
    def action():
        current = cmd("m").split()[0].upper()
        index = MODES.index(current) if current in MODES else -1
        mode = MODES[(index + 1) % len(MODES)]
        send_set(f"M {mode}")
        print(f"Mode: {mode}", flush=True)
    safe(action)


def step_next():
    state["step"] = (state["step"] + 1) % len(STEPS_HZ)
    print(f"Tuning step: {STEPS_HZ[state['step']]} Hz", flush=True)


def toggle_record():
    def action():
        # Gqrx records the demodulated audio; configure the recording path in Gqrx.
        current = cmd("u RECORD")
        if current not in ("0", "1"):
            raise RuntimeError(f"Unexpected recorder state from Gqrx: {current!r}")
        proposed = current == "0"
        send_set(f"U RECORD {1 if proposed else 0}")
        state["record"] = proposed
        print(f"Audio recording: {'ON' if proposed else 'OFF'}", flush=True)
    safe(action)


def main():
    # Hardware inputs use GPIO's 3.3V pullups; common terminals -> GND.
    dial = RotaryEncoder(17, 27, max_steps=0)
    dial.when_rotated_clockwise = lambda: tune(+1)
    dial.when_rotated_counter_clockwise = lambda: tune(-1)
    encoder_push = Button(22, pull_up=True, bounce_time=0.06)
    band = Button(23, pull_up=True, bounce_time=0.06)
    mode = Button(24, pull_up=True, bounce_time=0.06)
    step = Button(25, pull_up=True, bounce_time=0.06)
    rec = Button(5, pull_up=True, bounce_time=0.06)
    encoder_push.when_pressed = step_next
    band.when_pressed = band_next
    mode.when_pressed = mode_next
    step.when_pressed = step_next
    rec.when_pressed = toggle_record
    print("HF-RX 04 controller ready. Start Gqrx remote server at localhost:7356.", flush=True)
    print("Preset bands: 80m/40m/WWV/20m/17m. Buttons: BAND, MODE, STEP, REC.", flush=True)
    pause()


if __name__ == "__main__":
    main()
