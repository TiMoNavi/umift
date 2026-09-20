"""Gripper value normalization for UMI-FT replay-buffer export."""

from __future__ import annotations

import math
from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import numeric
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.constants import DEFAULT_GRIPPER_MAX_WIDTH_M


def gripper_open_fraction(value: Any) -> float:
    gripper = value if isinstance(value, dict) else {}
    open_percent = numeric(gripper.get("open_percent"), 0.0)
    if open_percent > 1.0:
        return max(0.0, min(1.0, open_percent / 100.0))
    return max(0.0, min(1.0, open_percent))


def export_gripper_value(open_fraction: float, width_m: float, config: dict[str, Any]) -> float:
    output = str(config.get("output", "width_m_from_open_fraction_linear"))
    if output == "open_fraction_0_1":
        return open_fraction
    return width_m


def gripper_width_m(value: Any, open_fraction: float, config: dict[str, Any]) -> float:
    gripper = value if isinstance(value, dict) else {}
    for key in ("width_m", "gripper_width_m"):
        width = numeric(gripper.get(key), math.nan)
        if math.isfinite(width) and width >= 0.0:
            return width
    min_width = numeric(config.get("fallback_min_width_m"), 0.0)
    max_width = numeric(config.get("fallback_max_width_m"), DEFAULT_GRIPPER_MAX_WIDTH_M)
    if max_width < min_width:
        min_width, max_width = max_width, min_width
    return min_width + max(0.0, min(1.0, open_fraction)) * (max_width - min_width)
