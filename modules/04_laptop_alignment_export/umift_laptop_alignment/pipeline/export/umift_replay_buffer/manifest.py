"""Manifest construction for UMI-FT replay-buffer exports."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from umift_laptop_alignment.orchestration.run_layout import relpath, utc_now_iso
from umift_laptop_alignment.pipeline.export.base import ExportManifest


def build_export_manifest(
    *,
    exporter_name: str,
    exporter_version: str,
    paths: Any,
    zarr_path: Path,
    config_path: Path,
    required_inputs: list[str],
    effective_config: dict[str, Any],
    source_timeline_rows: int,
    exported_timeline_rows: int,
    episode_summaries: list[dict[str, Any]],
    write_global_rgb: bool,
    global_rgb_field: str,
    write_depth: bool,
) -> ExportManifest:
    pose_notes = dict(effective_config["pose"])
    extrinsic = pose_notes.get("iphone_camera_t_gripper_center_tcp")
    if isinstance(extrinsic, list) and len(extrinsic) == 4:
        translation = [float(extrinsic[index][3]) for index in range(3)]
        pose_notes["extrinsic_translation_m"] = translation
        pose_notes["extrinsic_distance_m"] = math.sqrt(sum(value * value for value in translation))
        pose_notes["composition"] = (
            "T_user_world_gripper_center_tcp = "
            "T_user_world_iphone_camera @ T_iphone_camera_gripper_center_tcp"
        )

    manifest: ExportManifest = {
        "schema_version": 1,
        "row_type": "export_manifest",
        "format": exporter_name,
        "version": exporter_version,
        "run_id": paths.run_dir.name,
        "created_at": utc_now_iso(),
        "status": "ok",
        "outputs": {
            "zarr": relpath(zarr_path, paths.run_dir),
            "export_config": relpath(config_path, paths.run_dir),
        },
        "required_inputs": required_inputs,
        "source_timeline_rows": source_timeline_rows,
        "exported_timeline_rows": exported_timeline_rows,
        "episode_count": len(episode_summaries),
        "episodes": episode_summaries,
        "field_semantics": {
            "rgb_0": "iPhone RGB mounted on gripper, resized THWC uint8.",
            "ts_pose_fb_0": "Gripper-center TCP pose referenced to the episode user_world frame, stored as [x,y,z,qw,qx,qy,qz].",
            "gripper_0": "UMI gripper width in meters when available; otherwise open_fraction mapped linearly by export config.",
            "wrench_left_coinft_0": "Calibrated left CoinFT force/moment in CoinFT sensor frame.",
            "wrench_right_coinft_0": "Calibrated right CoinFT force/moment in CoinFT sensor frame.",
            "wrench_left_0": "Left CoinFT force/moment transformed to UMI-FT TCP/tool frame using original UMI-FT geometry.",
            "wrench_right_0": "Right CoinFT force/moment transformed to UMI-FT TCP/tool frame using original UMI-FT geometry.",
            "wrench_concat_0": "Left/right TCP-frame concatenation [left6,right6].",
            "ts_pose_command_0": "V0 copy of ts_pose_fb_0 because no robot command stream is captured yet.",
            "ts_pose_virtual_target_0": "V0 placeholder equal to ts_pose_fb_0; UMI-FT virtual-target postprocess can overwrite it.",
            "stiffness_0": "V0 placeholder constant; UMI-FT postprocess can overwrite it.",
        },
        "coordinate_frame_notes": pose_notes,
        "timestamp_notes": effective_config["timestamps"],
        "warnings": [
            "ts_pose_fb_0 stores an absolute gripper-center TCP pose in the stable per-episode user_world frame; it is not zeroed at episode start.",
            "Different episodes may use different user_world origins or horizontal headings; UMI-FT window-relative motion is invariant to a constant left world-frame transform.",
            "CoinFT-to-TCP wrench conversion uses the original UMI-FT hardware constants and exported gripper width estimate.",
            "ts_pose_command_0, ts_pose_virtual_target_0, and stiffness_0 are V0 placeholders for UMI-FT compatibility.",
            "The stored ARKit camera_in_user_world_transform is preferred; quaternion and Euler fields are legacy fallbacks only.",
        ],
    }
    if write_global_rgb:
        manifest["field_semantics"][global_rgb_field] = "D435/global camera RGB aligned to rgb_0 timeline, resized THWC uint8."
        manifest["field_semantics"]["rgb_global_time_stamps_0"] = "D435/global camera timestamps on the same episode-local seconds timebase."
    if write_depth:
        manifest["field_semantics"]["depth_0"] = "D435/global depth aligned to rgb_0 timeline, clipped meters repeated to 3 channels float16."
    return manifest
