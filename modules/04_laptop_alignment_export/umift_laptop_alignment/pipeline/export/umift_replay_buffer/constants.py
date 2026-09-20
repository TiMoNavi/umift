"""Hardware constants used by the UMI-FT replay-buffer exporter."""

from __future__ import annotations

IPHONE_CAMERA_T_GRIPPER_CENTER_TCP = (
    (1.0, 0.0, 0.0, 0.039711),
    (0.0, -1.0, 0.0, -0.094423),
    (0.0, 0.0, -1.0, -0.257957),
    (0.0, 0.0, 0.0, 1.0),
)

COINFT_TO_GOPRO_Z_M = 0.166
COINFT_TO_GOPRO_Y_M = 0.081
DEFAULT_GRIPPER_MAX_WIDTH_M = 0.08
