# 04 - Assembly and test

Order matters: everything is proven on the bench before plastic is printed, and the coupon
is printed before the enclosure.

## Stage 1 - Bench (no enclosure)

1. Mount the Pi on the display standoffs, 5 cm FFC, pogo pins aligned. Power up, confirm
   picture and touch (doc 02 section 1).
2. Plug the V4 into the Pi via the USB extension. `rtl_test -t` must say "V4 Detected".
3. Install the app (doc 02 sections 3-4). Run `python3 -m pisdr --windowed`. You should see
   a live spectrum. Tune to a local FM station (BAND -> FM bcast) and hear audio.
4. Run the headless check as well, it isolates the radio from the UI:
   `python3 -m pisdr.radio.flowgraph --freq 100.1e6 --mode WFM --seconds 15`.
5. Wire the EC11 module and the four keys on a breadboard first (doc 01 section 3). Run
   `python3 -m pisdr.controls.gpio` and confirm every event prints. Then run the app:
   - knob tunes; if it tunes backwards swap `ENCODER_A_GPIO` / `ENCODER_B_GPIO` in `config.py`,
   - knob push cycles the step, knob hold opens the keypad,
   - K1 BAND, K2 MODE, K3 KNOB (held: Settings), K4 FNC steps through the three rows of
     keys (held: the menu).
6. Leave it running for 30 minutes. `vcgencmd measure_temp` should stay below 70 C in the
   open; if not, fix cooling (heat sink on the SoC) before the enclosure makes it worse.

Pass criteria: audio, spectrum, every control and `rtl_test` all behave, no undervoltage
warnings in `dmesg`.

## Stage 2 - Measure and print

1. Complete the fit sheet (doc 03 section 6) and edit `cad/pisdr_params.scad`.
2. Print `pisdr_coupon.scad`; work through doc 03 section 7.
3. Run `cad/export.ps1` and print the carrier and keycaps first (small, quick), then the
   faceplate, then the rear shell.
4. Clean the window edges with a deburring tool; the chamfer should be smooth to the finger.

## Stage 3 - Sub-assemblies

### Key board

1. Cut perfboard to about 140 x 18 mm (7 holes tall). Lay the printed carrier on it, pocket
   side up, and mark the four switch positions and the two end screw holes through the
   carrier's pilot dimples. Drill the two screw holes 2.7 mm.
2. Push the four switches through the carrier openings into the perfboard holes, solder on
   the back. Confirm with a meter which pin pairs are the switch contacts.
3. Wire: one contact of each switch to its GPIO, the others daisy-chained to GND
   (two switches per GND pin as in the table). Use 20 cm female Dupont leads so the board
   can be unplugged. Label K1..K4.

### Encoder

1. Remove the nut and washer from the EC11 module. Pass the bushing through the faceplate
   from behind so the PCB sits between the two anti-rotation pillars, washer and nut on
   the front. Hand tight plus a quarter turn with pliers; do not crush the plastic.
2. Wire GND, + (to 3.3 V!), SW, DT, CLK with labelled Dupont leads.
3. Fit the knob on the shaft. Align the D flat (or press firmly on a knurled shaft). If you
   printed the grub screw hole, thread an M3 x 4 grub screw against the flat.

### Keycaps

Drop a keycap into each faceplate hole from behind (flange side towards you). They stay in
place once the carrier is screwed on.

### Faceplate inserts

Set four M3 heat-set inserts into the ends of the corner bosses with a soldering iron at
about 230 C. Keep them square; the back-wall screws must find them.

## Stage 4 - Final assembly

1. Faceplate face down on a soft cloth. Screw the key carrier + perfboard to the faceplate
   with two M2.5 x 12 screws. Press each key: it must click and return.
2. Lay the display into the pocket between the tabs, glass down. Route the FFC towards the
   Pi connector. Check from the front that the picture edges will not be hidden by the lip
   (power it briefly if in doubt).
3. Plug in all Dupont leads at the GPIO header (doc 01 section 3 table). Dress the wires
   along the key row and knob column, away from the V4.
4. Fit the SMA bulkhead to the right wall, the USB-C and 3.5 mm panel connectors to the
   bottom wall, and plug their tails into the Pi. Plug the USB extension into a Pi USB 2.0
   port (the black ones; the blue USB 3.0 ports generate more interference).
5. Zip-tie the V4 into its cradle, SMA end towards the bottom. Connect the pigtail.
6. Optional foil lining now (doc 03 section 8).
7. Lower the rear shell over the faceplate; the alignment rim guides it. The four display
   posts should just touch the module's rear. Their length is derived from `display_t`: if
   they bottom out before the shell closes, `display_t` is too small; if they do not touch,
   it is too large (or add a felt pad for a fraction of a millimetre).
8. Four M3 x 10 countersunk screws from the back. Snug, not tight.

## Stage 5 - Enclosed test

| Test | Pass |
|---|---|
| Power on, app auto-starts full screen within 40 s | yes |
| Touch is accurate to the edges of the picture | yes |
| Every key lines up under its label; knob turns freely, no rub on the faceplate | yes |
| 30 min on FM broadcast with the display at full brightness: `vcgencmd measure_temp` | < 75 C |
| Shell above the Pi after 30 min | warm, not hot (< 50 C) |
| HF reception (40 m, 20 m) with the real antenna vs. the V4 on a bare laptop | noise floor within ~6 dB |
| Recording (K4) produces a playable WAV in `~/Recordings` | yes |

If HF noise is much worse enclosed than bare, go through doc 01 section 7 in order: the
display brightness and the USB cable are the usual culprits.

## Maintenance notes

- The microSD card is only reachable with the rear shell off. Use SSH/VNC for updates
  (`cd ~/PiSDR && git pull`).
- A settings reset is `rm ~/.config/pisdr/settings.json`.
- Logs: `~/.config/pisdr/pisdr.log`.
