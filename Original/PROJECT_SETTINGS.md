# Pi SDR — active project configuration (HF-RX 04)

## Confirmed user-owned major hardware
| Component | Selection |
|---|---|
| Single-board computer | Raspberry Pi 4 Model B |
| Receiver | RTL-SDR Blog V4 — RTL2832U / R828D / 1 ppm TCXO / SMA / software-controllable 4.5 V bias tee |
| Screen | Hosyond 7 inch IPS capacitive MIPI DSI touchscreen, 800 × 480 resolution |
| 3D printer | Maximum print area 220 × 220 mm |
| Alternate receiver | Nooelec NESDR SMArt XTR, not used for this build |

## Design (planned, not fit-verified)
- One-piece faceplate **208 × 188 × 3.2 mm**; rear shell 208 × 188 × 78 mm deep.
- Mechanical EC11 quadrature tuning encoder with push switch; one 45mm 3D-printed knob.
- Four normally-open pushbuttons (BAND, MODE, STEP, REC).
- Prototyped Hosyond display geometry; adjust retaining clips to actual display before full print.
- RTL-SDR V4 on short shielded USB extension, physically separated from Pi/display noise.
- RX-only antenna input: HF receiving wire -> appropriate 9:1 UNUN -> protected coax/feedline choke -> SMA bulkhead -> V4.

## Raspberry Pi operating system and radio configuration
- Raspberry Pi OS **64-bit Desktop** (planned, not yet flashed).
- Application: **Gqrx SDR** (`gqrx-sdr`) with its built-in waterfall and demodulator modes.
- Gqrx input device: RTL-SDR V4, **device string `rtl=0`** (confirm index if more SDRs attached).
- **Direct sampling: DISABLED** — the V4 has an internal HF converter.
- **LNB/LO frequency offset: 0 Hz** (not -125 MHz; that was for obsolete XTR+Ham It Up plan).
- **Bias tee: OFF** for passive wire antenna/UNUN.
- Starting sample rate: **1.024 MS/s**; adjust once reception is verified.
- Initial test signals: WWV 10.000 MHz AM (when propagation allows), 7.200 MHz LSB, 14.200 MHz USB.
- Enable Gqrx Tools -> Remote control -> start TCP server; restrict to **127.0.0.1:7356**, no authentication.
- GPIO Python controller should run while Gqrx is running, after enabling remote control.
- Install/update V4-capable RTL-SDR libraries as needed: https://www.rtl-sdr.com/v4/ . Confirm no duplicated old librtlsdr library used by Gqrx.

## Controller wiring (Raspberry Pi 4 physical 40-pin header)
Inputs are active-low; use 3.3V internal pullups. All switches/encoder contacts connect only between GPIO and GND.

| Control | BCM GPIO | Physical Pi pin | Return |
|---|---:|---:|---|
| Encoder A | 17 | 11 | Encoder common to GND physical 14 |
| Encoder B | 27 | 13 | Encoder common to GND physical 14 |
| Encoder push | 22 | 15 | GND physical 9 |
| BAND | 23 | 16 | GND physical 20 |
| MODE | 24 | 18 | GND physical 20 |
| STEP | 25 | 22 | GND physical 25 |
| REC | 5 | 29 | GND physical 25 |

For a **bare EC11 encoder**. Check pinout if substituting a breakout board. **Never apply 5V to GPIO inputs.**

## Hosyond DSI display
- Connect screen's **15-pin DSI ribbon** to the Pi 4 DSI connector in correct orientation.
- Screen power uses manufacturer's Pi-mounted contacts **or** its specified +5V/GND cable, never both simultaneously.
- For the documented separate-power method, verify screen revision: Pi physical pin 4 (+5V) and pin 6 (GND) only when the manufacturer specifies those connections.
- Do not assume DSI video ribbon provides display power.
- Screen's 800×480 resolution requires fitting waterfall, demodulator controls, and touch targets; use software scaling as needed.

## Pi power and audio
- Quality **5.1V / 3A USB-C Pi 4** supply (verify budget including SDR and DSI screen).
- Audio via Pi 3.5mm socket or USB sound device to *powered* speaker / headphones; confirm output device selected in Raspberry Pi OS.

## Controller preset table
| Preset | Frequency | Mode |
|---|---:|---|
| 80 m SSB | 3.900 MHz | LSB |
| 40 m SSB | 7.200 MHz | LSB |
| WWV | 10.000 MHz | AM |
| 20 m SSB | 14.200 MHz | USB |
| 17 m SSB | 18.130 MHz | USB |

Steps: 10 / 100 / 1,000 / 10,000 Hz. Modes: LSB, USB, AM, CW. BAND cycles presets; tuning button and STEP cycle step; REC toggles Gqrx demodulated audio recorder.

## Sources
- RTL-SDR Blog V4: https://www.rtl-sdr.com/v4/
- Gqrx TCP control: https://www.gqrx.dk/doc/remote-control
- Hosyond screen user manual (part-specific revision check required): https://device.report/m/7cdabf87d5cff62cb64a706062ce4e709f13cfa753a8ec80abfa2704a1775aad
