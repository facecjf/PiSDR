// PiSDR tuning knob for the EC11 module shaft (6 mm D-shaft or knurled).
// Printed upright: shaft socket down. Add a drop of hot glue or a M3 grub screw (hole provided).
include <pisdr_params.scad>

grub_screw = true;      // M3 grub screw hole into the shaft flat
grub_d     = 2.8;       // tap M3 directly into the plastic
ridges     = 24;        // finger grip ridges around the skirt
dimple     = true;      // finger dimple on top for fast spinning

socket_d     = enc_shaft_d + 0.3;
socket_depth = min(enc_shaft_len - 1.0, knob_h - 3.0);

module knob() {
    difference() {
        union() {
            // body with a slight taper and a rounded top edge
            hull() {
                cylinder(d = knob_d, h = 1);
                translate([0, 0, knob_h - 2]) rotate_extrude() translate([knob_d / 2 - 2.5, 0]) circle(r = 2);
            }
            // grip ridges
            for (i = [0:ridges - 1]) rotate(i * 360 / ridges)
                translate([knob_d / 2 - 0.2, 0, 1.5]) cylinder(d = 1.6, h = knob_h - 5, $fn = 12);
        }
        // shaft socket with D flat
        translate([0, 0, -eps]) difference() {
            cylinder(d = socket_d, h = socket_depth + eps);
            if (enc_shaft_flat < enc_shaft_d)
                translate([-socket_d, enc_shaft_flat - enc_shaft_d / 2 + 0.15, -1]) cube([2 * socket_d, socket_d, socket_depth + 2]);
        }
        // grub screw, perpendicular to the flat
        if (grub_screw) translate([0, 0, socket_depth / 2]) rotate([-90, 0, 0]) cylinder(d = grub_d, h = knob_d);
        // finger dimple
        if (dimple) translate([knob_d / 4, 0, knob_h + 2.5]) sphere(r = 4.5);
        // pointer line
        translate([knob_d / 2 - 6, -0.6, knob_h - 0.8]) cube([5, 1.2, 1]);
    }
}

knob();
