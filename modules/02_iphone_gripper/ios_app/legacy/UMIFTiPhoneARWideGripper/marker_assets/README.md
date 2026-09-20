# WedgeTag v0 Marker Assets

This folder contains a first-pass custom fiducial design for the ARKit-wide gripper app.

The design is not a standard ArUco marker. It is a shape-based marker:

- black vertical trapezoid on white backing
- top width: 2 mm
- bottom width: 8 mm
- trapezoid height: 30 mm
- recommended backing: 12 mm x 34 mm

Use:

- Print at 100% scale.
- Keep the white backing around the black wedge.
- Mount vertically.
- Wide end should point toward the gripper tip / bottom of the camera image.
- Left and right variants differ by the internal white cutouts.

Files:

- `wedgetag_v0_left.svg`: left gripper variant.
- `wedgetag_v0_right.svg`: right gripper variant.
- `wedgetag_v0_sheet.svg`: both variants on one small printable sheet.

Detection plan:

1. Threshold the lower image ROI for dark connected components.
2. Fit a tapered vertical contour.
3. Use the wide end to determine orientation.
4. Use internal white cutouts as a secondary identity check.
5. Apply side ROI and calibrated 1D path constraints before trusting the marker.

