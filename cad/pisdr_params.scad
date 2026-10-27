// PiSDR enclosure - every dimension in one place. Units: mm.
//
// Coordinate system (as assembled, looking at the front of the radio):
//   X to the right, Y up, Z towards the viewer. The faceplate front surface is z = 0 and
//   the box extends to negative z. "Depth" values below are positive distances measured
//   from the faceplate front INTO the box; the into(d) helper converts them.
//   Origin = outer bottom-left corner of the faceplate.
//
// Lines tagged  // MEASURE  depend on your physical parts. Fill in docs/03_enclosure.md
// (fit sheet) with calipers, update the values here, then print the coupon
// (pisdr_coupon.scad) before the full enclosure.

$fn = 48;
eps = 0.01;

// --------------------------------------------------------------------------- printer / fit
wall           = 2.5;    // side walls of the rear shell
rear_t         = 2.5;    // back wall of the rear shell
face_t         = 3.0;    // faceplate thickness
fit_clear      = 0.25;   // clearance for parts that slide together (rim in shell)
rf_lining_t    = 0.3;    // room left for optional foil tape inside the shell

// --------------------------------------------------------------------------- Hosyond 7" DSI display (B0D3QB7X4Z)
display_w      = 164.89; // MEASURE  module width  (manual: 164.89)
display_h      = 102.00; // MEASURE  module height (manual: 102.00)
display_t      = 12.25;  // MEASURE  glass front to rearmost point of the driver PCB (manual: 12.25)
display_glass_t = 0.0;   // MEASURE  0 if the touch glass is the full module size (usual). If the glass is
                         //          smaller than a surrounding frame and stands proud of it, enter how far,
                         //          and raise face_t so the window lip stays >= 2 mm thick.
active_w       = 154.68; // active area (manual)
active_h       = 87.02;  // active area (manual)
active_off_left   = (display_w - active_w) / 2;  // MEASURE  left module edge -> left edge of picture
active_off_bottom = (display_h - active_h) / 2;  // MEASURE  bottom module edge -> bottom edge of picture
window_margin  = 2.0;    // window is the active area plus this on each side
display_clear  = 0.3;    // clearance around the module in its pocket
bl_button_pos  = [152, 8];  // MEASURE  backlight button on the driver PCB, from display bottom-left; [] = no hole
bl_button_d    = 6.0;

// --------------------------------------------------------------------------- Raspberry Pi 4 on the display standoffs
pi_w = 85.0;  pi_h = 56.0;  pi_pcb_t = 1.4;
pi_conn_h      = 16.0;   // tallest connector (USB-A stack) above the PCB
pi_standoff_h  = 4.0;    // MEASURE  supplied copper pillar body length (4+4 mm pillar -> 4)
pi_pos         = [40, 23];  // MEASURE  Pi PCB lower-left corner relative to display lower-left (front view)
pi_rotation    = 0;      // MEASURE  0 = USB/Ethernet edge faces +X (right), 180 = faces -X (left)
rear_clear     = 3.0;    // air gap between the Pi connectors and the back wall

// --------------------------------------------------------------------------- EC11 encoder module (KY-040 style)
enc_bushing_d  = 7.2;    // MEASURE  threaded bushing is M7 -> 7.2 hole
enc_bushing_len = 6.0;   // MEASURE  threaded length; faceplate is thinned locally if face_t > this - 2
enc_pcb        = [19, 32, 1.6]; // MEASURE  module PCB X x Y x t, mounted with the pin header pointing down
enc_pcb_offset = [0, -4.0];     // MEASURE  PCB centre relative to the shaft axis (X, Y)
enc_body_h     = 7.0;    // MEASURE  PCB top surface -> shoulder that the panel rests on
enc_pcb_clear  = 0.4;
enc_shaft_d    = 6.0;    // MEASURE  shaft diameter
enc_shaft_flat = 4.5;    // MEASURE  D-shaft: across the flat (= shaft_d for a round knurled shaft)
enc_shaft_len  = 15.0;   // MEASURE  shaft length beyond the bushing end
knob_d         = 30.0;
knob_h         = 18.0;
knob_skirt_gap = 1.0;    // knob bottom to faceplate front

// --------------------------------------------------------------------------- 12 x 12 x 7.3 tactile keys
key_body       = 12.0;   // MEASURE  switch body side
key_body_h     = 4.3;    // MEASURE  PCB top -> top of the body (excluding plunger)
key_total_h    = 7.3;    // MEASURE  PCB top -> top of plunger
key_plunger_d  = 3.5;    // MEASURE  plunger diameter
key_travel     = 0.3;    // MEASURE  actuation travel
key_pcb_t      = 1.6;    // perfboard thickness
keycap_top     = 12.4;   // keycap square (visible part)
keycap_proud   = 1.0;    // keycap stands this far in front of the faceplate
keycap_clear   = 0.3;    // per side, keycap in its faceplate hole
keycap_flange  = 15.0;   // retention flange behind the faceplate
keycap_flange_t = 1.2;
keycap_rest_gap = 0.4;   // flange to faceplate rear when not pressed
keycap_socket_depth = 2.5;
key_row_h      = 22.0;   // height of the control row below the screen
carrier_len    = 150.0;  // key carrier / perfboard length
carrier_w      = 20.0;
carrier_screw_d = 2.2;   // M2.5 self-tapping into the faceplate
carrier_screw_clear = 2.7;

// --------------------------------------------------------------------------- RTL-SDR Blog V4
v4_size        = [27.0, 70.0, 12.0]; // MEASURE  width (X) x length (Y) x thickness (Z), case only
v4_clear       = 0.4;
v4_y0          = 8.0;    // bottom of the V4 above the inside of the bottom wall
sma_hole_d     = 6.5;    // MEASURE  bulkhead adapter thread
sma_pos        = [40, 20];   // [Y, Z] on the right wall
sma_flat       = true;   // D-flat on the hole (most bulkheads have one)

// --------------------------------------------------------------------------- panel-mount cables (bottom wall)
usbc_panel     = true;
usbc_opening   = [11.0, 7.0];  // MEASURE  opening for the panel-mount USB-C extension body
usbc_screw_spacing = 20.0;     // MEASURE  flange screw spacing
usbc_screw_d   = 3.2;
usbc_x         = 60;
audio_jack     = true;
audio_hole_d   = 6.5;    // MEASURE  3.5 mm panel-mount extension thread
audio_x        = 120;
panel_conn_z   = 27.0;   // centre height of the bottom-wall connectors from the faceplate front

// --------------------------------------------------------------------------- screws
boss_d         = 7.0;
insert_hole_d  = 4.0;    // MEASURE  for your M3 heat-set insert (4.0 for OD 4.0-4.2, 4.6 for OD 4.6-5)
insert_depth   = 6.0;
m3_clear_d     = 3.4;
m3_csk_d       = 6.5;

// --------------------------------------------------------------------------- vents / label
vent_slot      = [2.0, 30.0];
vent_pitch     = 5.0;
vent_cols      = 10;
label_text     = "PiSDR";
label_size     = 4.5;
label_depth    = 0.6;

// =========================================================================== derived layout
left_margin    = 2.0;                  // wall -> display, left
right_margin   = 2.0;                  // display -> knob column
top_margin     = boss_d + 1.0;         // room for the top corner bosses
knob_col_w     = 36.0;

display_x      = wall + left_margin;
display_y      = wall + key_row_h + 2.0;
outer_w        = display_x + display_w + right_margin + knob_col_w + wall;
outer_h        = display_y + display_h + top_margin + wall;

interior_depth = display_t + pi_standoff_h + pi_pcb_t + pi_conn_h + rear_clear;
depth_total    = face_t + interior_depth + rear_t;     // outer depth of the assembled box
z_back_inner   = face_t + interior_depth;              // inside face of the back wall

active_x0      = display_x + active_off_left;
active_y0      = display_y + active_off_bottom;
window_w       = active_w + 2 * window_margin;
window_h       = active_h + 2 * window_margin;
window_x0      = active_x0 - window_margin;
window_y0      = active_y0 - window_margin;

// Soft-key cells are 1/4 of the active width; physical keys sit under their centres.
key_centres_x  = [for (i = [0:3]) active_x0 + (i + 0.5) * active_w / 4];
key_y          = wall + key_row_h / 2;
keycap_hole    = keycap_top + 2 * keycap_clear;
// faceplate rear -> perfboard top surface (the carrier plate is exactly this thick).
// At rest the plunger top touches the bottom of the keycap socket and the flange floats
// keycap_rest_gap behind the faceplate; pressing closes that gap by key_travel.
carrier_t      = key_total_h + keycap_rest_gap + keycap_flange_t - keycap_socket_depth;
carrier_x0     = (key_centres_x[0] + key_centres_x[3]) / 2 - carrier_len / 2;
carrier_screw_x = [carrier_x0 + 5, carrier_x0 + carrier_len - 5];

knob_col_x0    = display_x + display_w + right_margin;
knob_cx        = knob_col_x0 + knob_col_w / 2;
knob_cy        = outer_h - wall - 4.0 - knob_d / 2;

boss_inset     = wall + boss_d / 2 + 0.3;
boss_xy        = [[boss_inset, boss_inset], [outer_w - boss_inset, boss_inset],
                  [boss_inset, outer_h - boss_inset], [outer_w - boss_inset, outer_h - boss_inset]];

// display retention posts (from the back wall, press on the module's rear corners)
post_inset     = 6.0;    // MEASURE  must land on the metal frame, not on a component
post_d         = 6.0;
post_xy        = [[display_x + post_inset, display_y + post_inset],
                  [display_x + display_w - post_inset, display_y + post_inset],
                  [display_x + post_inset, display_y + display_h - post_inset],
                  [display_x + display_w - post_inset, display_y + display_h - post_inset]];
post_tip_z     = face_t + display_t + 0.2;

// Pi footprint in enclosure coordinates (for vents and the preview)
pi_x0 = display_x + pi_pos[0];
pi_y0 = display_y + pi_pos[1];
pi_z0 = face_t + display_t + pi_standoff_h;

// V4 cradle in the knob column, lying against the back wall
v4_x0 = knob_cx - v4_size[0] / 2;
v4_y0_abs = wall + v4_y0;
v4_z0 = z_back_inner - v4_size[2];

// --------------------------------------------------------------------------- helpers
module rounded_rect(size, r) {
    hull() for (x = [r, size[0] - r], y = [r, size[1] - r]) translate([x, y]) circle(r = r);
}

// Children modelled with z in [0, h] end up spanning depth d .. d + h into the box.
module into(d) { translate([0, 0, -d]) mirror([0, 0, 1]) children(); }

// Pi connector edges in enclosure coordinates, honouring pi_rotation.
function pi_usb_edge_x() = pi_rotation == 0 ? pi_x0 + pi_w : pi_x0;   // USB-A / Ethernet edge

module echo_summary() {
    echo(str("outer footprint: ", outer_w, " x ", outer_h, " mm, depth ", depth_total, " mm"));
    echo(str("window: ", window_w, " x ", window_h, " at ", window_x0, ", ", window_y0));
    echo(str("key centres X: ", key_centres_x, "  Y: ", key_y));
    echo(str("knob centre: ", knob_cx, ", ", knob_cy));
    echo(str("carrier thickness: ", carrier_t));
    assert(outer_w <= 220 && outer_h <= 220, "Faceplate exceeds a 220 x 220 bed");
    assert(enc_bushing_len - 2 >= min(face_t, 2.0), "Encoder bushing too short for the panel");
}
