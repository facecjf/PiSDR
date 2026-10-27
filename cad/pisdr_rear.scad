// PiSDR rear shell: four side walls + back wall, open towards the faceplate.
// Modelled in the assembled frame; the last line stands it on its back wall for printing.
include <pisdr_params.scad>

shell_depth = interior_depth + rear_t;      // from the faceplate rear to the outside of the back

module rear_shell() {
    difference() {
        union() {
            // walls + back
            into(face_t) difference() {
                linear_extrude(shell_depth) rounded_rect([outer_w, outer_h], 4);
                translate([0, 0, -eps]) linear_extrude(interior_depth + eps)
                    offset(delta = -wall) square([outer_w, outer_h]);
            }
            // corner screw pads on the inside of the back wall (thicker seat for the countersink)
            for (p = boss_xy) into(z_back_inner - 1.5) translate([p[0], p[1], 0])
                cylinder(d = boss_d + 2, h = 1.5 + eps);
            display_posts();
            v4_cradle();
        }
        // countersunk M3 holes through the back wall into the faceplate bosses
        for (p = boss_xy) {
            into(z_back_inner - 2) translate([p[0], p[1], 0]) cylinder(d = m3_clear_d, h = rear_t + 3);
            into(depth_total - 1.8) translate([p[0], p[1], 0])
                cylinder(d1 = m3_clear_d, d2 = m3_csk_d + 0.4, h = 1.8 + eps);
        }
        // SMA bulkhead on the right wall
        into(sma_pos[1]) translate([outer_w - wall - 1, wall + sma_pos[0], 0]) rotate([0, 90, 0])
            d_hole(sma_hole_d, wall + 2, sma_flat);
        // bottom-wall connectors
        if (usbc_panel) into(panel_conn_z) translate([usbc_x, -1, 0]) rotate([-90, 0, 0]) {
            hull() for (sx = [-1, 1]) translate([sx * (usbc_opening[0] - usbc_opening[1]) / 2, 0, 0])
                cylinder(d = usbc_opening[1], h = wall + 2);
            for (sx = [-1, 1]) translate([sx * usbc_screw_spacing / 2, 0, 0])
                cylinder(d = usbc_screw_d, h = wall + 2);
        }
        if (audio_jack) into(panel_conn_z) translate([audio_x, -1, 0]) rotate([-90, 0, 0])
            cylinder(d = audio_hole_d, h = wall + 2);
        // backlight button access
        if (len(bl_button_pos) == 2)
            into(z_back_inner - 1) translate([display_x + bl_button_pos[0], display_y + bl_button_pos[1], 0])
                cylinder(d = bl_button_d, h = rear_t + 2);
        // vents in the back wall over the Pi
        vents_back();
        // vents in the top wall
        for (i = [0:vent_cols - 1])
            into(face_t + interior_depth / 2 - vent_slot[1] / 4)
                translate([display_x + 30 + i * vent_pitch - vent_slot[0] / 2, outer_h - wall - 1, 0])
                    cube([vent_slot[0], wall + 2, vent_slot[1] / 2]);
    }
}

module d_hole(d, h, flat) {
    // cylinder along +Z with an optional D flat for anti-rotation bulkheads
    difference() {
        cylinder(d = d, h = h);
        if (flat) translate([d / 2 - 0.6, -d, -1]) cube([d, 2 * d, h + 2]);
    }
}

module display_posts() {
    // Press the display module forward against the faceplate lip. Tips get a dab of
    // silicone or a felt pad; the length is derived so they just touch the module rear.
    post_len = z_back_inner - post_tip_z;
    for (p = post_xy) into(post_tip_z) translate([p[0], p[1], 0]) {
        cylinder(d = post_d, h = post_len + eps);
        cylinder(d1 = 4, d2 = post_d, h = 1.5);
    }
}

module v4_cradle() {
    // Two saddles hold the dongle against the back wall; a zip tie goes through the slots.
    inner_w = v4_size[0] + 2 * v4_clear;
    saddle_h = v4_size[2] * 0.75;
    for (yy = [v4_y0_abs + 12, v4_y0_abs + v4_size[1] - 12])
        into(z_back_inner - saddle_h) translate([knob_cx, yy, 0])
            difference() {
                translate([-(inner_w / 2 + 3), -3, 0]) cube([inner_w + 6, 6, saddle_h + eps]);
                translate([-inner_w / 2, -4, -eps]) cube([inner_w, 8, saddle_h + 1]);
                // zip-tie slots through both side walls
                for (sx = [-1, 1]) translate([sx * (inner_w / 2 + 1.5), 0, saddle_h / 2])
                    cube([1.6, 8, 3.5], center = true);
            }
    // end stop so the dongle cannot slide towards the bottom wall
    into(z_back_inner - saddle_h) translate([knob_cx - inner_w / 2, v4_y0_abs - 3, 0])
        cube([inner_w, 3, saddle_h + eps]);
}

module vents_back() {
    // slot array over the Pi footprint, avoiding the display posts
    cx = pi_x0 + pi_w / 2;
    cy = pi_y0 + pi_h / 2;
    for (i = [0:vent_cols - 1])
        into(z_back_inner - 1) translate([cx - (vent_cols - 1) * vent_pitch / 2 + i * vent_pitch, cy, 0])
            linear_extrude(rear_t + 2) square([vent_slot[0], vent_slot[1]], center = true);
}

echo_summary();
// Print orientation: back wall on the bed, opening up.
translate([0, 0, depth_total]) rear_shell();
