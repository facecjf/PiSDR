// PiSDR key carrier: a spacer plate between the faceplate and the key perfboard.
// It sets the switch-to-faceplate distance exactly, keeps the four switches on the soft-key
// centres, and doubles as the drilling/soldering template for the perfboard.
//
//   faceplate rear | carrier (carrier_t thick) | perfboard (switches soldered on its front)
//
// Print flat as modelled (faceplate side down).
include <pisdr_params.scad>

body_pocket = key_body + 0.6;          // switch body passes through
flange_pocket = keycap_flange + 0.6;   // keycap flange floats in here
flange_pocket_depth = keycap_rest_gap + keycap_flange_t + 0.4;

module carrier() {
    difference() {
        linear_extrude(carrier_t) rounded_rect([carrier_len, carrier_w], 3);
        for (x = key_centres_x) translate([x - carrier_x0, carrier_w / 2, 0]) {
            // flange pocket on the faceplate side
            translate([0, 0, -eps]) linear_extrude(flange_pocket_depth + eps) square(flange_pocket, center = true);
            // body opening right through
            translate([0, 0, -eps]) linear_extrude(carrier_t + 1) square(body_pocket, center = true);
        }
        // screws to the faceplate (M2.5 through perfboard + carrier into the faceplate)
        for (x = carrier_screw_x) translate([x - carrier_x0, carrier_w / 2, -eps])
            cylinder(d = carrier_screw_clear, h = carrier_t + 1);
        // perfboard drill template: pilot dimples at the switch pin positions (12 x 12 THT: 5.0 x 12.5 mm)
        for (x = key_centres_x) for (sx = [-1, 1], sy = [-1, 1])
            translate([x - carrier_x0 + sx * 2.5, carrier_w / 2 + sy * 6.25, carrier_t - 1])
                cylinder(d = 1.2, h = 2);
    }
}

echo(str("carrier thickness ", carrier_t, " mm; screw holes at X = ", carrier_screw_x));
carrier();
