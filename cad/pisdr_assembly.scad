// PiSDR assembly preview (not for printing). Open in OpenSCAD, press F5.
// Set explode > 0 to pull the parts apart along Z; toggle the show_* flags to look inside.
include <pisdr_params.scad>
use <pisdr_front.scad>
use <pisdr_rear.scad>
use <pisdr_knob.scad>
use <pisdr_keycaps.scad>
use <pisdr_carrier.scad>

explode      = 0;        // mm
show_front   = true;
show_rear    = true;
show_display = true;
show_pi      = true;
show_v4      = true;
show_knob    = true;
show_keys    = true;
show_carrier = true;

if (show_front) color("DimGray") faceplate();

if (show_rear) color("SlateGray", 0.6) translate([0, 0, -explode * 2]) rear_shell();

if (show_display) color("Black", 0.8) into(face_t) translate([display_x, display_y, 0])
    cube([display_w, display_h, display_t]);
if (show_display) color("DarkSlateBlue", 0.9) into(face_t - 0.05) translate([active_x0, active_y0, 0])
    cube([active_w, active_h, 0.1]);

if (show_pi) color("ForestGreen") into(pi_z0) translate([pi_x0, pi_y0, 0])
    cube([pi_w, pi_h, pi_pcb_t]);
if (show_pi) color("Silver") into(pi_z0 + pi_pcb_t) {
    // USB-A / Ethernet stack on the connector edge
    translate([pi_rotation == 0 ? pi_x0 + pi_w - 17 : pi_x0, pi_y0 + 2, 0]) cube([17, pi_h - 4, pi_conn_h]);
}

if (show_v4) color("Gray") translate([0, 0, -explode]) into(v4_z0) translate([v4_x0, v4_y0_abs, 0]) cube(v4_size);

if (show_knob) color("Orange") translate([knob_cx, knob_cy, knob_skirt_gap + explode]) knob();

if (show_keys) color("Orange") for (x = key_centres_x)
    translate([x, key_y, keycap_proud + explode]) mirror([0, 0, 1]) keycap();

if (show_carrier) color("Tan") into(face_t) translate([carrier_x0, key_y - carrier_w / 2, 0]) carrier();
if (show_carrier) color("SaddleBrown") into(face_t + carrier_t) translate([carrier_x0 + 5, key_y - 9, 0])
    cube([carrier_len - 10, 18, key_pcb_t]);

echo_summary();
