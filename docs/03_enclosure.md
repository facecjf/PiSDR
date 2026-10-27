# 03 - Enclosure: spec sheet and fit sheet

All parts are generated from `cad/pisdr_params.scad`. Nothing in this document is typed by
hand twice: the numbers below are what the defaults produce (`openscad` prints them as
`ECHO` lines when you render). **Fill in the fit sheet (section 6) before printing anything
larger than the coupon.**

## 1. Concept

```text
            front view                                   side section (not to scale)
+------------------------------------+-------+       front
|  lip 2 mm over the glass border    |       |        |  faceplate 3.0
|  +------------------------------+  |  (o)  | knob   |  display 12.25   <- locator tabs, 6 mm tall
|  |   active area 154.68 x 87.02 |  |       |        |  standoffs 4.0
|  |                              |  | PiSDR |        |  Pi PCB 1.4 + connectors 16.0
|  +------------------------------+  |       |        |  air gap 3.0
|   [K1]   [K2]   [K3]   [K4]        | V4    |        |  back wall 2.5
+------------------------------------+-------+       rear  (total 42.15)
          209.89 x 139.0
```

- Two main prints: **faceplate** (flat, with tall corner bosses and locator tabs on its rear)
  and **rear shell** (five-sided box). Four M3 countersunk screws go in from the back into
  heat-set inserts in the faceplate bosses. The front shows no screws.
- The display drops into the faceplate between four L-shaped tabs and is pressed forward
  by four posts from the rear shell. The Pi rides on the display's own standoffs.
- Keys: four 12 mm tactile switches on a 140 x 18 mm perfboard strip, spaced by a printed
  **carrier** that is also the drilling template, pressed by printed square **keycaps**.
- The EC11 module is panel-mounted with its own nut through the faceplate; two printed
  pillars stop its PCB from rotating.
- The RTL-SDR V4 lies in a zip-tie **cradle** against the back wall in the knob column.
  Antenna via an SMA bulkhead in the right wall. Power via a panel-mount USB-C extension
  and audio via a panel-mount 3.5 mm extension in the bottom wall.

## 2. Parts list (printed)

| File | Part | Qty | Orientation | Approx. size | Notes |
|---|---|---|---|---|---|
| `pisdr_coupon.scad` | fit coupon | 1 | as modelled | 60 x 45 x 15 | print first |
| `pisdr_front.scad` | faceplate | 1 | front face down (file is pre-rotated) | 209.9 x 139 x 40 | 0.2 mm layers, 4 perimeters |
| `pisdr_rear.scad` | rear shell | 1 | back wall down | 209.9 x 139 x 39 | 3 perimeters, no supports needed |
| `pisdr_knob.scad` | knob | 1 | socket down | 30 dia x 18 | |
| `pisdr_keycaps.scad` | keycaps | 4 | top face down | 15 x 15 x 6 | 0.12 mm layers for a clean face |
| `pisdr_carrier.scad` | key carrier | 1 | flat | 150 x 20 x 6.4 | |

Material: PETG recommended (the back wall gets warm above the Pi). PLA works if the thermal
test in doc 04 stays under 60 C at the shell.

## 3. Derived dimensions with the default parameters

| Item | Value |
|---|---|
| Outer footprint | 209.89 x 139.00 mm |
| Total depth (faceplate front to back) | 42.15 mm |
| Window | 158.68 x 91.02 mm, lower-left at (7.61, 31.99) from the faceplate lower-left |
| Display pocket | 164.89 x 102.00 + 0.3 mm clearance, lower-left at (4.50, 26.50) |
| Key centres (X) | 28.94, 67.61, 106.28, 144.95 mm; Y = 13.5 mm |
| Keycap holes | 13.0 x 13.0 mm |
| Knob centre | (189.39, 117.50) mm; bushing hole 7.2 mm |
| Corner screw bosses | 7 mm dia, centres 6.3 mm in from the outer edges |
| Display posts | 6 mm dia at 6 mm in from each display corner, tips at depth 15.45 mm |
| Carrier thickness | 6.4 mm (= 7.3 + 0.4 + 1.2 - 2.5) |
| V4 cradle | centred X = 189.39, Y from 10.5 to 80.5 mm, against the back wall |
| SMA bulkhead | right wall, 42.5 mm up from the bottom edge, 20 mm back from the front |
| USB-C / 3.5 mm panel cutouts | bottom wall at X = 60 / 120 mm, 27 mm back from the front |

The key X positions are exactly 1/8, 3/8, 5/8 and 7/8 of the active width, matching the four
cells of the soft-key bar drawn by the software.

## 4. Why the window is this size

The lip has to hide the edge of the picture and hold the glass. With 2 mm of margin around
the active area the lip overlaps the glass border by about 3.1 mm left/right and 5.5 mm
top/bottom. If your measured `active_off_left/bottom` differ from the centred default, the
window moves with them and the lip widths change accordingly.

## 5. Changing things

| Want | Change |
|---|---|
| Thicker / stiffer faceplate | `face_t` (then check `enc_bushing_len` still reaches the nut) |
| Deeper box (taller USB cable plug, fan) | `rear_clear` |
| Bigger knob | `knob_d` (<= 32 with the default 36 mm knob column) or `knob_col_w` |
| Keys you already own | `key_body`, `key_total_h`, `key_plunger_d`, `key_travel` |
| No panel connectors | `usbc_panel = false; audio_jack = false;` |
| Different inserts | `insert_hole_d` |
| Pi mounted the other way round | `pi_rotation = 180` |
| Flip screen orientation | software only (doc 02 section 1) - the enclosure is symmetric enough |

## 6. Fit sheet - measure before printing

Measure with calipers, write the value here, then copy it into `cad/pisdr_params.scad`.
Items marked * change the geometry noticeably; the rest are tolerance checks.

| # | Parameter | What to measure | Default | Measured |
|---|---|---|---|---|
| 1* | `display_w`, `display_h` | Outer size of the display module including the glass | 164.89 x 102.00 | |
| 2* | `display_t` | Glass front to the rearmost point of the driver PCB (excluding standoffs) | 12.25 | |
| 3* | `active_off_left` | Left module edge to the left edge of the lit picture (show a white screen) | 5.105 | |
| 4* | `active_off_bottom` | Bottom module edge to the bottom edge of the lit picture | 7.49 | |
| 5 | `display_glass_t` | 0 if the glass is the full module size; else how far the smaller glass stands proud of the frame | 0 | |
| 6* | `pi_pos` | Pi PCB lower-left corner from the display lower-left, Pi mounted on the standoffs, front view | [40, 23] | |
| 7* | `pi_rotation` | 0 if the Pi's USB/Ethernet edge faces the knob side (right), else 180 | 0 | |
| 8 | `pi_standoff_h` | Body length of the supplied copper pillar | 4.0 | |
| 9 | `bl_button_pos` | Backlight button centre from the display lower-left (rear view, mirror X!) | [152, 8] | |
| 10 | `post_inset` | Confirm that 6 mm in from each display corner on the rear is flat metal, not a component | 6 | ok / move to |
| 11* | `enc_bushing_d`, `enc_bushing_len` | Thread diameter and threaded length of the EC11 bushing | 7.2 / 6.0 | |
| 12 | `enc_pcb`, `enc_pcb_offset` | Module PCB size, and shaft axis offset from the PCB centre | [19,32,1.6] / [0,-4] | |
| 13 | `enc_body_h` | PCB top to the shoulder the panel rests on | 7.0 | |
| 14* | `enc_shaft_d`, `enc_shaft_flat`, `enc_shaft_len` | Shaft diameter, across-the-flat (if D), length past the bushing | 6.0 / 4.5 / 15 | |
| 15* | `key_body`, `key_body_h`, `key_total_h` | Tactile switch body side, body height, total height to plunger top | 12.0 / 4.3 / 7.3 | |
| 16 | `key_plunger_d`, `key_travel` | Plunger diameter; travel (datasheet, usually 0.25-0.35) | 3.5 / 0.3 | |
| 17* | `v4_size` | RTL-SDR V4 case W x L x T | [27, 70, 12] | |
| 18 | `sma_hole_d`, `sma_flat` | Bulkhead thread diameter; has a D flat? | 6.5 / yes | |
| 19 | `usbc_opening`, `usbc_screw_spacing` | Panel-mount USB-C body opening and flange hole spacing | [11, 7] / 20 | |
| 20 | `audio_hole_d` | Panel-mount 3.5 mm thread diameter | 6.5 | |
| 21 | `insert_hole_d` | Recommended hole for your M3 heat-set inserts | 4.0 | |

Measuring tip for 3 and 4: run `python3 -m pisdr --sim` (or any white screen), lay a steel
rule across the module edge and read where the lit pixels start.

## 7. Test coupon

`pisdr_coupon.scad` prints in about 25 minutes and checks the five things that can go wrong:

1. The display corner sits between the two tabs with light friction (fit items 1, 5).
2. A keycap slides freely in its hole without rattling (`keycap_clear`).
3. The EC11 bushing passes through and its nut tightens (items 11, 13). The panel is thinned
   around the hole to `enc_bushing_len - 2.5` mm automatically; if the nut still cannot
   reach the thread, enter the true `enc_bushing_len` or lower `face_t`.
4. A heat-set insert seats flush and an M3 screw threads in (item 21).
5. An M2.5 screw self-taps into the carrier boss hole.

Adjust parameters, re-render the coupon if needed, then run `cad/export.ps1` for the full set.

## 8. RF lining (optional)

Line the inside of the rear shell (back wall and side walls, not the faceplate) with foil
tape, overlapping strips by 5 mm. Cut around the display posts and the V4 cradle. Bond the
lining to the SMA bulkhead with a short wire under the bulkhead nut. `rf_lining_t` keeps
0.3 mm free for the tape thickness. See doc 01 section 7 for what this does and does not fix.
