# 01 - Hardware

## 1. Bill of materials

| # | Part | Qty | Notes |
|---|---|---|---|
| 1 | Raspberry Pi 4 Model B | 1 | 2 GB is enough. 85 x 56 mm, holes 58 x 49 mm on 3.5 mm insets |
| 2 | Power supply 5.1 V / 3 A USB-C (official Pi 4 PSU) | 1 | Cheaper supplies sag under the Pi + screen + two SDRs |
| 3 | microSD 16 GB or larger, A1/A2 class | 1 | |
| 4 | RTL-SDR Blog V3 | 1 | **Main tuner.** R820T2 tuner 24-1766 MHz, 500 kHz-24 MHz by direct sampling, 1 ppm TCXO, SMA female, 4.5 V software bias tee. Aluminium case about 70 x 27 x 12 mm (**measure yours**) |
| 4a | Nooelec NESDR SMArt XTR | 1 | **ADS-B on 1090 MHz**, and reception above 1766 MHz. Elonics E4000, about 65-2200 MHz with a gap around 1100-1275 MHz, 0.5 ppm TCXO, SMA female, no bias tee. Aluminium case (**measure yours**) |
| 5 | USB-A male to USB-A female extension, 15-30 cm, shielded, with ferrite | 2 | One per dongle. Keeps them off the Pi's USB stack, away from the display driver and apart from each other (side by side in the Pi's ports they do not fit, and they heat each other) |
| 6 | Hosyond 7" IPS capacitive DSI touchscreen 800x480 (Amazon B0D3QB7X4Z) | 1 | Module 164.89 x 102.00 x 12.25 mm, active area 154.68 x 87.02 mm, 5 V 510 mA max |
| 7 | EC11 rotary encoder module, 5 pins (GND, +, SW, DT, CLK) | 1 | Also sold as "KY-040". PCB about 32 x 19 mm, 20-step, D-shaft or knurled 6 mm shaft (**measure**) |
| 8 | Tactile switch 12 x 12 x 7.3 mm, 4 pins | 4 | Round plunger about 3.5 mm diameter. If yours really are SMD, solder short wire leads to them |
| 9 | Perfboard, 2.54 mm pitch, about 20 x 160 mm strip | 1 | Carrier for the four keys; cut from a 7 x 9 cm board |
| 10 | SMA female-to-female bulkhead adapter | 2 | One per dongle. Panel hole 6.5 mm, D-flat optional. The enclosure in `cad/` has one hole: see doc 03 |
| 11 | SMA male to SMA male pigtail, RG316, 10-15 cm | 2 | Bulkhead to dongle |
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
| K3 | under soft-key 3 (KNOB) | GPIO25 | 22 | GND, pin 25 |
| K4 | under soft-key 4 (FNC: next row of keys / hold for the menu) | GPIO5 | 29 | GND, pin 25 |

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

## 4. The two dongles: jobs, placement and antenna inputs

| | RTL-SDR Blog V3 | NESDR SMArt XTR |
|---|---|---|
| Job | main tuner: listening and scanning, 500 kHz to 1766 MHz | ADS-B on 1090 MHz; the receiver's tuner above 1766 MHz |
| Tuner chip | Rafael Micro R820T2 | Elonics E4000 |
| Tuner range | 24-1766 MHz | about 65-2200 MHz, gap around 1100-1275 MHz |
| Below 24 MHz | 500 kHz-24 MHz by direct sampling (Q branch) | nothing |
| RF gain | 0-49.6 dB (no effect in direct sampling) | 0-42 dB |
| Bias tee | 4.5 V, switched in software | none |
| Antenna | the general-coverage antenna; an HF antenna for HF and CB | the 1090 MHz antenna |

- The application tells the two apart by their tuner chips and decides which one receives
  from the frequency (doc 02 section 5g). **Which USB port each is in does not matter.**
- The E4000's range varies a little from unit to unit; the edges of the gap in particular.
  `XTR_RANGES_HZ` in `software/pisdr/config.py` holds the numbers the application goes by,
  taken from what `rtl_test -t` measured on the XTR in this radio (52-2205 MHz, gap
  1103-1271 MHz). If yours measures differently, change them there (doc 02 section 2).
- **The antennas follow the jobs.** The 1090 MHz antenna goes on the **XTR's** socket and
  the antenna you listen with on the **V3's**. If the dongles had the jobs the other way
  round before, swap the two antenna leads.
- **The V3's one antenna socket serves HF and VHF/UHF.** A VHF/UHF whip hears little on
  HF and an HF wire is a poor VHF antenna, so decide what stays connected, or put a
  diplexer (HF / VHF-UHF) in front of the one socket so both antennas can stay.
- **ADS-B on the E4000 is the weaker half of this arrangement.** 1090 MHz is only 10 MHz
  below the nominal start of the E4000's gap, and where the gap starts differs from unit
  to unit: if `rtl_test -t` on your XTR reports an `E4K L-band gap` that begins below about
  1095 MHz, ADS-B on it will be poor. An E4000 is also somewhat less sensitive at 1090 MHz
  than an R820T2, and the XTR cannot power an amplified 1090 MHz antenna (see the bias tee
  below). If the map shows clearly fewer aircraft than the V3 did, the jobs can be given
  back the other way without touching the hardware other than the antenna leads: doc 02
  section 2, "name the dongles".
- Above 1766 MHz the receiver listens through the XTR, and so through the 1090 MHz
  antenna, which is at least in the right part of the spectrum for it.
- Direct sampling has no tuner in front of the ADC: there is no RF gain control, strong
  medium-wave stations can overload it, and above 14.4 MHz signals from 28.8 MHz minus the
  frequency fold in on top (the V3 has no filter for that). It is good enough for
  broadcast, WWV, CB (27 MHz is above 24 MHz, so the tuner is used) and the stronger
  amateur signals; an upconverter or a Blog V4 in the V3's place does HF properly, and the
  application uses a V4 without direct sampling if it finds one.
- Mount the dongles away from the display's driver board and the Pi's SoC, each on its own
  USB extension with the ferrite close to the dongle. The enclosure in `cad/` is the
  earlier single-dongle one (one cradle, one SMA hole); see the note in doc 03.
- Each dongle gets its own SMA bulkhead and pigtail. The user connects antennas to the
  bulkheads, never to the dongles.
- **Bias tee stays OFF** (default in the app) unless you know the antenna on the **V3**
  needs 4.5 V, for example an active antenna or a preamplifier at the feed point. A passive
  wire, 9:1 unun or a magnetic loop with its own supply must not see bias. The XTR has
  none; the setting never puts voltage on its socket, so a 1090 MHz antenna with a built-in
  amplifier, or an ADS-B LNA, needs its own supply there (a bias-tee injector, or an LNA
  powered from USB).

## 5. Power budget

| Load | Typical | Peak |
|---|---|---|
| Raspberry Pi 4 (GNU Radio running, 2 cores busy; `dump1090` adds part of a third) | 0.9 A | 1.4 A |
| Hosyond display at full brightness | 0.4 A | 0.51 A |
| NESDR SMArt XTR | 0.3 A | 0.35 A |
| RTL-SDR Blog V3 (plus whatever the bias tee feeds, up to 0.18 A) | 0.28 A | 0.5 A |
| Encoder module, keys | < 0.01 A | |
| Optional 5 V fan | 0.08 A | 0.15 A |
| **Total** | **2.0 A** | **2.9 A** |

The dongle currents are typical figures for these parts, not measurements of yours. A 3 A
supply still covers it, but with little to spare: both dongles are powered whenever the Pi
is, whether or not they are receiving. The Pi 4's four USB ports share 1.2 A; two dongles
are inside that, two dongles plus a USB sound card with a speaker amplifier may not be, and
then a powered hub is the answer. If the Pi logs "Undervoltage detected", suspect the cable before
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
  noise. A 3D-printed enclosure is RF-transparent, so inside the box the dongles and
  their pigtails sit in that noise field. HF through the V3's direct sampling input is the
  most exposed to it.
- Mitigations, in order of effectiveness:
  1. Keep the antenna outdoors or at least several metres from the box.
  2. Use the shielded USB extension and the shielded pigtail; keep both short.
  3. Line the inside of the rear shell and the knob column with foil tape, overlapping
     each strip onto the next, and bond the lining to the SMA bulkhead's ground with a
     screw and solder tag. This is what `docs/03_enclosure.md` calls the "RF lining"
     and the CAD leaves 0.3 mm of clearance for it.
  4. Lower the display brightness when listening to weak HF signals; the backlight
     boost converter is often the loudest source.
  5. If the noise is still unacceptable on HF, mount the V3 externally in its own shielded
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
| RTL-SDR dongle case in the CAD (`v4_size`, the single-dongle enclosure) | ~70 x 27 x 12 mm | **measure** |
| SMA bulkhead | 6.5 mm hole, 8 mm nut flats | **measure** |
