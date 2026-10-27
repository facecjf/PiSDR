// PiSDR test coupon - print this BEFORE the full enclosure.
// A 60 x 45 mm slice of the faceplate containing: one window corner with its lip and
// display locator tab, one keycap hole, the encoder bushing hole with its thinning, a
// corner boss with the heat-set insert hole and a carrier screw hole. Check:
//   * the display corner drops between the tabs with light friction,
//   * a keycap slides in the hole without binding,
//   * the EC11 nut tightens on the bushing through the panel,
//   * the heat-set insert seats and an M3 screw threads in.
include <pisdr_params.scad>

coupon = [60, 45];

module coupon_part() {
    difference() {
        union() {
            linear_extrude(face_t) rounded_rect(coupon, 3);
            // display corner tabs (bottom-left corner of the module placed at [8, 8])
            translate([8 - display_clear - 1.5, 8 - display_clear - 1.5, face_t - eps]) linear_extrude(6) {
                square([1.5, 14]);
                square([14, 1.5]);
            }
            // corner boss
            translate([52, 37, face_t - eps]) cylinder(d = boss_d, h = 12);
            // carrier screw boss
            translate([50, 8, face_t - eps]) cylinder(d = 6, h = 3);
        }
        // window corner: lip = window_margin inside the module edge
        translate([8 + (active_off_left - window_margin), 8 + (active_off_bottom - window_margin), -eps])
            hull() {
                translate([-1, -1, 0]) linear_extrude(eps) square([40, 30]);
                translate([0, 0, 1]) linear_extrude(face_t + 1) square([39, 29]);
            }
        // keycap hole
        translate([32, 10, -eps]) linear_extrude(face_t + 1) square(keycap_hole, center = true);
        // encoder hole + thinning
        translate([46, 22, -eps]) cylinder(d = enc_bushing_d, h = face_t + 1);
        thin_t = max(0, face_t - (enc_bushing_len - 2.5));
        if (thin_t > 0) translate([46, 22, face_t - thin_t]) cylinder(d = 14, h = thin_t + 1);
        // insert hole in the boss
        translate([52, 37, face_t + 12 - insert_depth]) cylinder(d = insert_hole_d, h = insert_depth + 1);
        // carrier screw
        translate([50, 8, 0.8]) cylinder(d = carrier_screw_d, h = face_t + 3);
    }
}

coupon_part();
