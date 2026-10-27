// PiSDR keycaps for the four 12 mm tactile switches. Printed top-down (as modelled).
// The square skirt rides in the faceplate hole, the flange behind the faceplate stops the
// cap falling out, and the plunger of the switch sits in the socket underneath.
include <pisdr_params.scad>

skirt_len = keycap_proud + face_t;                 // from the top face to the faceplate rear
cap_total = skirt_len + keycap_rest_gap + keycap_flange_t;

module keycap(legend = "") {
    difference() {
        union() {
            // skirt + visible top (slightly rounded top edge)
            hull() {
                translate([0, 0, 0.8]) linear_extrude(skirt_len - 0.8) square(keycap_top, center = true);
                linear_extrude(0.8) square(keycap_top - 1.6, center = true);
            }
            // flange
            translate([0, 0, skirt_len + keycap_rest_gap]) linear_extrude(keycap_flange_t)
                square(keycap_flange, center = true);
            // solid between skirt and flange (fills the rest gap)
            translate([0, 0, skirt_len - eps]) linear_extrude(keycap_rest_gap + 2 * eps)
                square(keycap_top, center = true);
        }
        // plunger socket from the back
        translate([0, 0, cap_total - keycap_socket_depth]) cylinder(d = key_plunger_d + 0.3, h = keycap_socket_depth + 1);
        // optional engraved legend on the top face
        if (legend != "") translate([0, 0, -eps]) linear_extrude(0.5) mirror([1, 0, 0])
            text(legend, size = 5, halign = "center", valign = "center");
    }
}

// Four caps laid out for printing; legends are optional since the screen labels the keys.
for (i = [0:3]) translate([i * (keycap_flange + 4), 0, 0]) keycap();
