# 01 - Hardware

## 1. Bill of materials

| # | Part | Qty | Notes |
|---|---|---|---|
| 1 | Raspberry Pi 4 Model B | 1 | 2 GB is enough. 85 x 56 mm, holes 58 x 49 mm on 3.5 mm insets |
| 2 | Power supply 5.1 V / 3 A USB-C (official Pi 4 PSU) | 1 | Cheaper supplies sag under the Pi + screen + SDR load |
| 3 | microSD 16 GB or larger, A1/A2 class | 1 | |
| 4 | RTL-SDR Blog V4 | 1 | R828D tuner, 1 ppm TCXO, SMA female, 4.5 V software bias tee. Aluminium case about 70 x 27 x 12 mm (**measure yours**) |
| 5 | USB-A male to USB-A female extension, 15-30 cm, shielded, with ferrite | 1 | Keeps the V4 off the Pi's USB stack and away from the display driver |
| 6 | Hosyond 7" IPS capacitive DSI touchscreen 800x480 (Amazon B0D3QB7X4Z) | 1 | Module 164.89 x 102.00 x 12.25 mm, active area 154.68 x 87.02 mm, 5 V 510 mA max |
| 7 | EC11 rotary encoder module, 5 pins (GND, +, SW, DT, CLK) | 1 | Also sold as "KY-040". PCB about 32 x 19 mm, 20-step, D-shaft or knurled 6 mm shaft (**measure**) |
| 8 | Tactile switch 12 x 12 x 7.3 mm, 4 pins | 4 | Round plunger about 3.5 mm diameter. If yours really are SMD, solder short wire leads to them |
| 9 | Perfboard, 2.54 mm pitch, about 20 x 160 mm strip | 1 | Carrier for the four keys; cut from a 7 x 9 cm board |
| 10 | SMA female-to-female bulkhead adapter | 1 | Panel hole 6.5 mm, D-flat optional |
| 11 | SMA male to SMA male pigtail, RG316, 10-15 cm | 1 | Bulkhead to V4 |
| 12 | M3 heat-set inserts (OD 4.0-4.6 mm, length 4-5 mm) | 4 | Rear shell to faceplate |
| 13 | M3 x 10 countersunk screws | 4 | |
| 14 | M2.5 x 12 pan-head screws | 2 | Perfboard + carrier to the faceplate (self-tapping into plastic) |
| 14a | M3 x 4 grub screw (optional) | 1 | Knob to shaft |
| 14b | Panel-mount USB-C extension, female panel to male, 20-30 cm | 1 | Power inlet in the bottom wall; flange with two M3 holes |
| 14c | Panel-mount 3.5 mm stereo extension, 20-30 cm | 1 | Headphone/line out in the bottom wall |
| 15 | Dupont jumper wires, female-to-female, 20 cm | 10 | Encoder and key wiring to the GPIO header |
| 16 | 1 kOhm resistors (optional, series protection on GPIO lines) | 7 | Cheap insurance against a miswired `+` lead |
| 17 | Copper or aluminium foil tape with conductive adhesive (optional) | 1 roll | RF lining, see section 7 |
| 18 | 30 x 30 x 7 mm or 40 x 40 x 10 mm 5 V fan (optional) | 1 | Only if the thermal test in doc 04 demands it |
| 19 | Powered speaker or headphones | 1 | The Pi's 3.5 mm jack cannot drive a bare speaker |

Supplied with the Hosyond display and used in this build: 4 x M2.5 x 4 screws, 4 x M2.5
4+4 mm copper standoffs, 15-pin 1.0 mm FFC (5 cm and 10 cm, reverse type).

## 2. The display + Pi stack

The display's driver board carries four M2.5 standoff positions matching the Pi's hole
pattern and two **pogo pins** that press against the underside of the Pi's 5 V and GND
header pins. The Pi therefore mounts on the back of the display and needs no separate
power wire to the screen.

```text
front                                                            rear
|<- 3.0 ->|<------------ 12.25 ------------>|<- 4 ->|<-1.4->|<------- ~16 ------->|
faceplate   Hosyond glass + LCD + driver PCB   standoff  Pi PCB    Pi connectors / header
                                                                                  ~37 mm total
```

- Use the **5 cm FFC**. Contacts face the board on both ends (blue stiffener away from the
  PCB), and both latches must be fully closed.
- Power only the Pi (USB-C). Do **not** also run the 2-wire jumper to pins 4/6 of the
  header while the pogo pins are in contact.
- Which direction the Pi's connectors face is fixed by the pogo-pin location on your
  particular display revision. Record it on the fit sheet in doc 03 before designing the
  port cutouts; the CAD has a `pi_rotation` switch for the two possible orientations.
- The display's backlight button (short press = brightness step, 3 s hold = backlight off)
  is on the driver board; the rear shell has a hole so it stays reachable.

## 3. Controls

Both input devices are **active-low**: every switch pulls a GPIO to ground and the Pi's
internal pull-ups (about 50 kOhm to 3.3 V) hold the line high otherwise. Nothing on the
GPIO header ever sees 5 V.

### EC11 module (KY-040 style)

| Module pin | Connect to | Pi physical pin | Notes |
|---|---|---|---|
| GND | GND | 14 | |
| + | **3.3 V** | 17 | Feeds the module's 10 kOhm pull-ups on CLK and DT. **Never 5 V** |
| SW | GPIO22 | 15 | Push switch. No pull-up on most modules; the Pi's internal one is used |
| DT | GPIO27 | 13 | Encoder channel (see config) |
| CLK | GPIO17 | 11 | Encoder channel (see config) |

Direction is set in software: `ENCODER_A_GPIO=27`, `ENCODER_B_GPIO=17`. Swap those
two values in `software/pisdr/config.py` if the knob feels reversed; no rewiring needed.

### Tactile keys

Each 12 mm tactile switch has two pairs of internally-connected pins. Pins on the **same
side** (the pair 6.5 mm apart on a 12 x 12 THT switch) are shorted together permanently;
the switch closes between the two sides. Check with a meter before soldering.

| Key | Position (left to right, below screen) | GPIO (BCM) | Pi physical pin | Return |
|---|---|---|---|---|
| K1 | under soft-key 1 (BAND) | GPIO23 | 16 | GND, pin 20 |
| K2 | under soft-key 2 (MODE) | GPIO24 | 18 | GND, pin 20 |
| K3 | under soft-key 3 (FUNC) | GPIO25 | 22 | GND, pin 25 |
| K4 | under soft-key 4 (MEM / hold for SCAN) | GPIO5 | 29 | GND, pin 25 |

The key labels are drawn by software and change with context; the hardware positions
never change. Key centres sit at 1/8, 3/8, 5/8 and 7/8 of the active-area width so they
line up under the four cells of the on-screen soft-key bar.

### Full GPIO map

| BCM | Physical | Function | BCM | Physical | Function |
|---:|---:|---|---:|---:|---|
| - | 1 | 3V3 | - | 2 | 5V (display pogo) |
| - | 3 | | - | 4 | 5V (display pogo) |
| - | 5 | | - | 6 | GND (display pogo) |
| - | 7 | | - | 8 | |
| - | 9 | GND | - | 10 | |
| 17 | 11 | **ENC CLK (A)** | - | 12 | |
| 27 | 13 | **ENC DT (B)** | - | 14 | GND (encoder GND) |
| 22 | 15 | **ENC SW** | 23 | 16 | **K1** |
| - | 17 | 3V3 (encoder +) | 24 | 18 | **K2** |
| - | 19 | | - | 20 | GND (K1, K2) |
| - | 21 | | 25 | 22 | **K3** |
| - | 23 | | - | 24 | |
| - | 25 | GND (K3, K4) | - | 26 | |
| - | 27 | ID_SD (do not use) | - | 28 | ID_SC (do not use) |
| 5 | 29 | **K4** | - | 30 | GND |
| - | 31-40 | free | | | |

The display's I2C touch controller uses the dedicated DSI/I2C0 lines, not the header;
GPIO2/3 stay free. Pins 31-40 remain available for a fan tach/PWM or future keys.

## 4. RTL-SDR V4 placement and antenna input

- Mount the V4 in the printed cradle in the knob column, as far from the display's
  driver board and the Pi's SoC as the box allows. Connect it through the USB extension
  with its ferrite close to the dongle.
- Fit the SMA bulkhead in the right-hand wall; connect the bulkhead to the V4 with the
  short pigtail. The user connects their antenna to the bulkhead, never to the dongle.
- **Bias tee stays OFF** (default in the app) unless you know the antenna needs 4.5 V.
  A passive wire, 9:1 unun or a magnetic loop with its own supply must not see bias.
- The V4 tunes 500 kHz to about 1.766 GHz without direct sampling. Leave "direct
  sampling" and any "LO offset" settings alone; those are for other dongles.

## 5. Power budget

| Load | Typical | Peak |
|---|---|---|
| Raspberry Pi 4 (GNU Radio running, 2 cores busy) | 0.9 A | 1.3 A |
| Hosyond display at full brightness | 0.4 A | 0.51 A |
| RTL-SDR V4 | 0.28 A | 0.33 A |
| Encoder module, keys | < 0.01 A | |
| Optional 5 V fan | 0.08 A | 0.15 A |
| **Total** | **1.7 A** | **2.3 A** |

A 3 A supply has margin. If the Pi logs "Undervoltage detected", suspect the cable before
the supply: use the cable that came with the PSU or a cable rated 3 A with 20 AWG power
conductors.

## 6. Audio

- Default output is the Pi's 3.5 mm jack (`Headphones` in Raspberry Pi OS). It is noisy
  and low-level; a USB audio dongle is a worthwhile upgrade and is picked up automatically
  if set as the default sink.
- The app's VOLUME control scales the audio in the flowgraph; the system mixer should be
  left near maximum.

## 7. RFI notes - read before you are disappointed by HF reception

- The Hosyond display driver board and the Pi's HDMI/USB3 circuitry radiate broadband
  noise. A 3D-printed enclosure is RF-transparent, so inside the box the V4 and its
  pigtail sit in that noise field.
- Mitigations, in order of effectiveness:
  1. Keep the antenna outdoors or at least several metres from the box.
  2. Use the shielded USB extension and the shielded pigtail; keep both short.
  3. Line the inside of the rear shell and the knob column with foil tape, overlapping
     each strip onto the next, and bond the lining to the SMA bulkhead's ground with a
     screw and solder tag. This is what `docs/03_enclosure.md` calls the "RF lining"
     and the CAD leaves 0.3 mm of clearance for it.
  4. Lower the display brightness when listening to weak HF signals; the backlight
     boost converter is often the loudest source.
  5. If the noise is still unacceptable on HF, mount the V4 externally in its own shielded
     box at the antenna feedpoint and run only USB into the enclosure.

## 8. Dimensions that drive the CAD (reference)

| Item | Value | Source |
|---|---|---|
| Display module | 164.89 x 102.00 x 12.25 mm | Hosyond manual |
| Display active area | 154.68 x 87.02 mm | Hosyond manual |
| Active area offset from module edges | **measure** (~5.1 mm left/right, ~7.5 mm top/bottom) | fit sheet |
| Pi 4 PCB | 85.0 x 56.0 x 1.4 mm, holes 58 x 49 mm | Raspberry Pi mechanical drawing |
| Pi 4 height above PCB (USB stack) | 16.0 mm | Raspberry Pi mechanical drawing |
| Standoff (display to Pi PCB) | 4.0 mm | supplied 4+4 mm copper pillar |
| EC11 module PCB | ~32 x 19 x 1.6 mm, shaft 6 mm, 15 mm long | **measure** |
| 12 mm tactile switch | 12.0 x 12.0 x 7.3 mm, plunger ~3.5 mm dia | **measure** |
| RTL-SDR V4 case | ~70 x 27 x 12 mm | **measure** |
| SMA bulkhead | 6.5 mm hole, 8 mm nut flats | **measure** |
