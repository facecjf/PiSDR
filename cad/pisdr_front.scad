// PiSDR faceplate.
// Modelled in the assembled frame (front face at z = 0, box towards -z). The last line
// flips it so the front face lies on the print bed with the bosses pointing up.
include <pisdr_params.scad>

module faceplate() {
    difference() {
        union() {
            // plate
            into(0) linear_extrude(face_t) rounded_rect([outer_w, outer_h], 4);
            // alignment rim that nests inside the shell walls
            into(face_t - eps) linear_extrude(3 + eps) difference() {
                offset(delta = -(wall + fit_clear)) square([outer_w, outer_h]);
                offset(delta = -(wall + fit_clear + 1.5)) square([outer_w, outer_h]);
            }
            // corner bosses, full interior depth, heat-set inserts for screws from the back
            for (p = boss_xy) into(face_t - eps) translate([p[0], p[1], 0])
                cylinder(d = boss_d, h = interior_depth + eps);
            display_tabs();
            encoder_pillars();
            // key carrier screw bosses
            for (x = carrier_screw_x) into(face_t - eps) translate([x, key_y, 0])
                cylinder(d = 6, h = 3 + eps);
        }
        // screen window with a 1 mm chamfer on the front edge
        into(-eps) translate([window_x0, window_y0, 0]) hull() {
            translate([-1, -1, 0]) linear_extrude(eps) square([window_w + 2, window_h + 2]);
            translate([0, 0, 1]) linear_extrude(face_t + 1) square([window_w, window_h]);
        }
        // glass relief: let the touch glass sit flush if it overhangs the LCD frame
        if (display_glass_t > 0)
            into(face_t - display_glass_t)
                translate([display_x - display_clear, display_y - display_clear, 0])
                    cube([display_w + 2 * display_clear, display_h + 2 * display_clear, display_glass_t + 10]);
        // heat-set insert holes in the bosses (from the back)
        for (p = boss_xy) into(face_t + interior_depth - insert_depth) translate([p[0], p[1], 0])
            cylinder(d = insert_hole_d, h = insert_depth + 1);
        // encoder bushing hole + local thinning so the nut reaches the thread
        into(-eps) translate([knob_cx, knob_cy, 0]) cylinder(d = enc_bushing_d, h = face_t + 1);
        thin_t = max(0, face_t - (enc_bushing_len - 2.5));
        if (thin_t > 0)
            into(face_t - thin_t) translate([knob_cx, knob_cy, 0]) cylinder(d = 14, h = thin_t + 1);
        // keycap holes
        for (x = key_centres_x) into(-eps) translate([x, key_y, 0])
            linear_extrude(face_t + 1) square(keycap_hole, center = true);
        // key carrier screws (self-tapping M2.5), blind from the back
        for (x = carrier_screw_x) into(0.8) translate([x, key_y, 0])
            cylinder(d = carrier_screw_d, h = face_t + 3);
        // engraved label on the knob column, below the knob
        if (label_text != "")
            into(-eps) translate([knob_cx, knob_cy - knob_d / 2 - 7, 0])
                linear_extrude(label_depth + eps)
                    text(label_text, size = label_size, halign = "center", valign = "center");
    }
}

module display_tabs() {
    // L-shaped locator tabs at the four module corners; the module drops in between them.
    tab_t = 1.5; tab_len = 14; tab_h = 6;
    x0 = display_x - display_clear - tab_t;
    x1 = display_x + display_w + display_clear;
    y0 = display_y - display_clear - tab_t;
    y1 = display_y + display_h + display_clear;
    into(face_t - eps) linear_extrude(tab_h + eps) {
        for (x = [x0, x1]) for (y = [y0, y1]) {
            translate([x, y == y0 ? y : y - tab_len + tab_t]) square([tab_t, tab_len]);
            translate([x == x0 ? x : x - tab_len + tab_t, y]) square([tab_len, tab_t]);
        }
    }
}

module encoder_pillars() {
    // Two pillars on the faceplate rear, one on each long side of the module PCB, reaching
    // past the PCB so the module cannot rotate when the knob is turned.
    pcx = knob_cx + enc_pcb_offset[0];
    pcy = knob_cy + enc_pcb_offset[1];
    half_w = enc_pcb[0] / 2 + enc_pcb_clear;
    pillar_h = enc_body_h + enc_pcb[2] + 2;
    for (sx = [-1, 1]) into(face_t - eps) translate([pcx + sx * (half_w + 1.5), pcy, 0])
        linear_extrude(pillar_h + eps) square([3, min(enc_pcb[1], 24)], center = true);
}

echo_summary();
// Print orientation: front face on the bed.
rotate([180, 0, 0]) faceplate();
