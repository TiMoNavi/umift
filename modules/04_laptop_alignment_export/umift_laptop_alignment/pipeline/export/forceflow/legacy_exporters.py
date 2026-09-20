#!/usr/bin/env python3
"""Export aligned UMIFT runs through small, format-specific adapters."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from umift_laptop_alignment.orchestration.run_layout import append_sync_log, ensure_run_layout, json_line, register_artifact, relpath, utc_now_iso, write_json
from umift_laptop_alignment.pipeline.export.forceflow.action_mapping import single_step_pose_delta
from umift_laptop_alignment.pipeline.export.forceflow.episode_mapping import episode_ends_from_ids, episode_ids_from_rows
from umift_laptop_alignment.pipeline.export.forceflow.gripper_mapping import (
    GripperInputScale,
    next_gripper_action,
    raw_open_to_binary,
)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


class Exporter(ABC):
    name: str
    description: str

    @abstractmethod
    def export(self, run_dir: Path) -> dict[str, Any]:
        raise NotImplementedError


class DebugJsonlExporter(Exporter):
    name = "debug-jsonl"
    description = "Copy aligned timeline/episodes into exports/debug_jsonl for inspection or downstream scripts."

    def export(self, run_dir: Path) -> dict[str, Any]:
        paths = ensure_run_layout(run_dir)
        target_dir = paths.exports_dir / "debug_jsonl"
        target_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, str] = {}
        for filename in ("timeline.jsonl", "episodes.jsonl", "ALIGNMENT_REPORT.json"):
            source = paths.aligned_dir / filename
            if source.exists():
                destination = target_dir / filename
                shutil.copy2(source, destination)
                outputs[filename] = relpath(destination, paths.run_dir)
        manifest = {
            "schema_version": 1,
            "row_type": "export_manifest",
            "format": self.name,
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "outputs": outputs,
        }
        manifest_path = target_dir / "EXPORT_MANIFEST.json"
        write_json(manifest_path, manifest)
        register_artifact(paths.run_dir, stream="export", role=self.name, path=target_dir, metadata={"kind": "directory"})
        append_sync_log(paths.run_dir, {"event": "exported_run", "format": self.name, "manifest": relpath(manifest_path, paths.run_dir)})
        return manifest


class CsvIndexExporter(Exporter):
    name = "index-csv"
    description = "Write a flat CSV index of aligned samples and nearest-source deltas."

    def export(self, run_dir: Path) -> dict[str, Any]:
        paths = ensure_run_layout(run_dir)
        target_dir = paths.exports_dir / "index_csv"
        target_dir.mkdir(parents=True, exist_ok=True)
        csv_path = target_dir / "aligned_index.csv"
        fieldnames = [
            "global_sample_index",
            "episode_index",
            "sample_index",
            "aligned_monotonic_ns",
            "iphone_sequence",
            "iphone_image_path",
            "d435_frame_index",
            "d435_delta_ms",
            "coinft_packet_index",
            "coinft_delta_ms",
            "iphone_valid",
            "d435_valid",
            "coinft_valid",
            "all_valid",
        ]
        row_count = 0
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in iter_jsonl(paths.aligned_dir / "timeline.jsonl"):
                iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
                d435 = row.get("d435", {}) if isinstance(row.get("d435"), dict) else {}
                coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
                valid = row.get("valid_mask", {}) if isinstance(row.get("valid_mask"), dict) else {}
                rgb = iphone.get("rgb", {}) if isinstance(iphone.get("rgb"), dict) else {}
                writer.writerow(
                    {
                        "global_sample_index": row.get("global_sample_index"),
                        "episode_index": row.get("episode_index"),
                        "sample_index": row.get("sample_index"),
                        "aligned_monotonic_ns": row.get("aligned_monotonic_ns"),
                        "iphone_sequence": iphone.get("sequence"),
                        "iphone_image_path": rgb.get("image_path"),
                        "d435_frame_index": d435.get("frame_index"),
                        "d435_delta_ms": d435.get("source_delta_ms"),
                        "coinft_packet_index": coinft.get("packet_index"),
                        "coinft_delta_ms": coinft.get("source_delta_ms"),
                        "iphone_valid": valid.get("iphone"),
                        "d435_valid": valid.get("d435"),
                        "coinft_valid": valid.get("coinft"),
                        "all_valid": all(bool(valid.get(name)) for name in ("iphone", "d435", "coinft")),
                    }
                )
                row_count += 1
        manifest = {
            "schema_version": 1,
            "row_type": "export_manifest",
            "format": self.name,
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "row_count": row_count,
            "outputs": {"aligned_index_csv": relpath(csv_path, paths.run_dir)},
        }
        manifest_path = target_dir / "EXPORT_MANIFEST.json"
        write_json(manifest_path, manifest)
        register_artifact(paths.run_dir, stream="export", role=self.name, path=target_dir, metadata={"kind": "directory"})
        append_sync_log(paths.run_dir, {"event": "exported_run", "format": self.name, "manifest": relpath(manifest_path, paths.run_dir)})
        return manifest


class ForceFlowMappingTemplateExporter(Exporter):
    name = "forceflow-template"
    description = "Write a ForceFlow mapping template; it does not fabricate missing robot action/state arrays."

    def export(self, run_dir: Path) -> dict[str, Any]:
        paths = ensure_run_layout(run_dir)
        target_dir = paths.exports_dir / "forceflow_template"
        target_dir.mkdir(parents=True, exist_ok=True)
        template_path = target_dir / "forceflow_mapping_template.json"
        payload = {
            "schema_version": 1,
            "row_type": "forceflow_mapping_template",
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "required_disk_layout": {
                "rgb_arm": "(N, 3, 240, 320) uint8",
                "rgb_fix": "(N, 3, 240, 320) uint8",
                "pos": "(N, 6) float32",
                "force": "(N, 6) float32",
                "action": "(N, 6) float32",
                "gripper_state": "(N, 1) float32",
                "gripper_action": "(N, 1) float32",
                "episode": "(N,) uint16",
                "meta/episode_ends": "(M,) uint32",
            },
            "suggested_sources": {
                "rgb_arm": "aligned_sample.iphone.rgb.image_path or future gripper ultrawide stream",
                "rgb_fix": "aligned_sample.d435.rgb.video_path + frame_index",
                "pos": "aligned_sample.iphone.pose after task-frame calibration, or robot state if added",
                "force": "aligned_sample.coinft calibrated 6-axis force; raw 24-channel CoinFT needs convert/calibration first",
                "action": "requires robot command/action stream; currently not present in module 04 raw streams",
                "gripper_state": "aligned_sample.iphone.gripper.open_percent normalized to task convention",
                "gripper_action": "requires commanded gripper stream or finite-difference policy; currently not present",
            },
            "blockers_for_real_zarr": [
                "Need explicit 6D action source.",
                "Need calibrated 6D force conversion from CoinFT raw channels.",
                "Need task-frame convention for iPhone pose to ForceFlow pos.",
            ],
        }
        write_json(template_path, payload)
        manifest = {
            "schema_version": 1,
            "row_type": "export_manifest",
            "format": self.name,
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "outputs": {"mapping_template": relpath(template_path, paths.run_dir)},
        }
        manifest_path = target_dir / "EXPORT_MANIFEST.json"
        write_json(manifest_path, manifest)
        register_artifact(paths.run_dir, stream="export", role=self.name, path=target_dir, metadata={"kind": "directory"})
        append_sync_log(paths.run_dir, {"event": "exported_run", "format": self.name, "manifest": relpath(manifest_path, paths.run_dir)})
        return manifest


def numeric(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def coerce_force_mode(mode: str) -> str:
    mode = str(mode or "proxy").strip().lower()
    if mode == "calibrated":
        return "calibrated-dual"
    if mode not in {"proxy", "calibrated-left", "calibrated-dual"}:
        raise ValueError(f"Unsupported ForceFlow V1 force mode: {mode}")
    return mode


def calibrated_left_wrench(row: dict[str, Any]) -> list[float] | None:
    coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
    wrench = coinft.get("left_wrench")
    if not isinstance(wrench, list) or len(wrench) != 6:
        return None
    values = [numeric(value, math.nan) for value in wrench]
    if any(not math.isfinite(value) for value in values):
        return None
    return values


def calibrated_left_wrench_is_ok(row: dict[str, Any]) -> bool:
    coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
    quality = coinft.get("quality", {}) if isinstance(coinft.get("quality"), dict) else {}
    calibration = coinft.get("calibration", {}) if isinstance(coinft.get("calibration"), dict) else {}
    if quality and not bool(quality.get("has_calibrated_wrench")):
        return False
    status = calibration.get("status")
    return status in (None, "ok")


def six_float_wrench(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 6:
        return None
    out = [numeric(item, math.nan) for item in value]
    if any(not math.isfinite(item) for item in out):
        return None
    return out


def calibrated_dual_wrench(row: dict[str, Any]) -> list[list[float]] | None:
    coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
    wrench = coinft.get("wrench")
    if isinstance(wrench, list) and len(wrench) == 2:
        left = six_float_wrench(wrench[0])
        right = six_float_wrench(wrench[1])
        if left is not None and right is not None:
            return [left, right]
    left = six_float_wrench(coinft.get("left_wrench"))
    right = six_float_wrench(coinft.get("right_wrench"))
    if left is None or right is None:
        return None
    return [left, right]


def calibrated_dual_wrench_is_ok(row: dict[str, Any]) -> bool:
    coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
    quality = coinft.get("quality", {}) if isinstance(coinft.get("quality"), dict) else {}
    calibration = coinft.get("calibration", {}) if isinstance(coinft.get("calibration"), dict) else {}
    if quality and not bool(quality.get("has_dual_calibrated_wrench", quality.get("has_calibrated_wrench"))):
        return False
    status = calibration.get("status")
    return status in (None, "ok")


def forceflow_minmax(values: Any) -> dict[str, list[float]]:
    import numpy as np

    arr = np.asarray(values, dtype=np.float32)
    if arr.size == 0:
        min_v = np.zeros((6,), dtype=np.float32)
        max_v = np.ones((6,), dtype=np.float32)
    else:
        min_v = arr.min(axis=0).astype(np.float32)
        max_v = arr.max(axis=0).astype(np.float32)
    equal = np.isclose(max_v, min_v)
    max_v[equal] = min_v[equal] + 1.0
    return {"min": min_v.tolist(), "max": max_v.tolist()}


def create_zarr_array(group: Any, name: str, *, shape: tuple[int, ...], dtype: Any, chunks: tuple[int, ...]) -> Any:
    try:
        return group.create_dataset(name, shape=shape, dtype=dtype, chunks=chunks)
    except TypeError:
        return group.create_array(name, shape=shape, dtype=dtype, chunks=chunks)


def ensure_numpy_zarr_compat(np_module: Any) -> None:
    # zarr 2.x still calls np.product when guessing chunks; NumPy 2 removed it.
    if not hasattr(np_module, "product") and hasattr(np_module, "prod"):
        setattr(np_module, "product", np_module.prod)


class ForceFlowSmokeZarrExporter(Exporter):
    name = "forceflow-smoke-zarr"
    description = "Create a test ForceFlow Zarr with placeholder pos matrix, finite-difference action, and raw CoinFT force proxy."

    image_shape = (3, 240, 320)

    def export(self, run_dir: Path) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
            import zarr
        except ImportError as exc:
            raise SystemExit(
                "forceflow-smoke-zarr requires cv2, numpy, and zarr. "
                "Run it in the ForceFlow environment or install these packages for this Python."
            ) from exc
        ensure_numpy_zarr_compat(np)

        paths = ensure_run_layout(run_dir)
        timeline_rows = list(iter_jsonl(paths.aligned_dir / "timeline.jsonl"))
        rows = [
            row
            for row in timeline_rows
            if all(bool(row.get("valid_mask", {}).get(name)) for name in ("iphone", "d435", "coinft"))
        ]
        if not rows:
            raise SystemExit(f"No all-valid aligned timeline rows found: {paths.aligned_dir / 'timeline.jsonl'}")

        target_dir = paths.exports_dir / "forceflow_smoke_zarr"
        target_dir.mkdir(parents=True, exist_ok=True)
        task_name = paths.run_dir.name
        zarr_path = target_dir / f"{task_name}.zarr"
        if zarr_path.exists():
            shutil.rmtree(zarr_path)

        pos_mapping = {
            "schema_version": 1,
            "row_type": "forceflow_pos_mapping",
            "mode": "placeholder_matrix",
            "input": "[x_m, y_m, z_m, roll_deg, pitch_deg, yaw_deg] from iPhone ARKit pose",
            "output": "[x_mm, y_mm, z_mm, roll_rad, pitch_rad, yaw_rad] placeholder task frame",
            "matrix_6x6": [
                [1000.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.0, 1000.0, 0.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 1000.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0, math.pi / 180.0, 0.0, 0.0],
                [0.0, 0.0, 0.0, 0.0, math.pi / 180.0, 0.0],
                [0.0, 0.0, 0.0, 0.0, 0.0, math.pi / 180.0],
            ],
            "offset_6": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "warning": "This is not a calibrated task-frame transform.",
        }
        matrix = np.asarray(pos_mapping["matrix_6x6"], dtype=np.float32)
        offset = np.asarray(pos_mapping["offset_6"], dtype=np.float32)

        n = len(rows)
        pos = np.zeros((n, 6), dtype=np.float32)
        force_proxy = np.zeros((n, 6), dtype=np.float32)
        gripper_state = np.zeros((n, 1), dtype=np.float32)
        episode = np.zeros((n,), dtype=np.uint16)

        raw_force_values: list[np.ndarray] = []
        for i, row in enumerate(rows):
            iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
            pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
            pose_vec = np.asarray(
                [
                    numeric(pose.get("x_m")),
                    numeric(pose.get("y_m")),
                    numeric(pose.get("z_m")),
                    numeric(pose.get("roll_deg")),
                    numeric(pose.get("pitch_deg")),
                    numeric(pose.get("yaw_deg")),
                ],
                dtype=np.float32,
            )
            pos[i] = matrix @ pose_vec + offset

            gripper = iphone.get("gripper", {}) if isinstance(iphone.get("gripper"), dict) else {}
            open_percent = numeric(gripper.get("open_percent"), 0.0)
            gripper_state[i, 0] = 1.0 if open_percent >= 50.0 else 0.0

            coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
            left_raw = coinft.get("left_raw") if isinstance(coinft.get("left_raw"), list) else []
            raw6 = np.asarray([(numeric(left_raw[j]) if j < len(left_raw) else 0.0) for j in range(6)], dtype=np.float32)
            raw_force_values.append(raw6)

            episode_index = row.get("episode_index")
            episode[i] = int(episode_index) if isinstance(episode_index, int) and 0 <= episode_index < 65535 else 0

        if raw_force_values:
            raw_force = np.stack(raw_force_values).astype(np.float32)
            baseline = raw_force[0:1]
            force_proxy = raw_force - baseline

        action = np.zeros((n, 6), dtype=np.float32)
        gripper_action = np.zeros((n, 1), dtype=np.float32)
        if n > 1:
            action[:-1] = pos[1:] - pos[:-1]
            action[-1] = action[-2]
            gripper_action[:-1, 0] = gripper_state[1:, 0]
            gripper_action[-1, 0] = gripper_action[-2, 0]
        else:
            gripper_action[:, 0] = gripper_state[:, 0]

        root = zarr.open(str(zarr_path), mode="w")
        data_group = root.create_group("data")
        meta_group = root.create_group("meta")

        rgb_arm = create_zarr_array(data_group, "rgb_arm", shape=(n, *self.image_shape), dtype=np.uint8, chunks=(1, *self.image_shape))
        rgb_fix = create_zarr_array(data_group, "rgb_fix", shape=(n, *self.image_shape), dtype=np.uint8, chunks=(1, *self.image_shape))
        data_group.create_dataset("pos", data=pos, shape=pos.shape, dtype=np.float32)
        data_group.create_dataset("force", data=force_proxy, shape=force_proxy.shape, dtype=np.float32)
        data_group.create_dataset("action", data=action, shape=action.shape, dtype=np.float32)
        data_group.create_dataset("gripper_state", data=gripper_state, shape=gripper_state.shape, dtype=np.float32)
        data_group.create_dataset("gripper_action", data=gripper_action, shape=gripper_action.shape, dtype=np.float32)
        data_group.create_dataset("episode", data=episode, shape=episode.shape, dtype=np.uint16)

        episode_ends = self.compute_episode_ends(rows)
        meta_group.create_dataset("episode_ends", data=episode_ends, shape=episode_ends.shape, dtype=np.uint32)

        captures: dict[Path, Any] = {}
        try:
            for i, row in enumerate(rows):
                rgb_arm[i] = self.load_iphone_image(paths.run_dir, row, cv2, np)
                rgb_fix[i] = self.load_d435_image(paths.run_dir, row, cv2, np, captures)
        finally:
            for capture in captures.values():
                capture.release()

        normalizer = {
            "pos": forceflow_minmax(pos),
            "action": forceflow_minmax(action),
            "force": forceflow_minmax(force_proxy),
        }
        normalizer_path = target_dir / f"{task_name}_normalizer.json"
        write_json(normalizer_path, normalizer)
        write_json(zarr_path / f"{task_name}_normalizer.json", normalizer)
        mapping_path = target_dir / "pos_mapping_used.json"
        write_json(mapping_path, pos_mapping)

        manifest = {
            "schema_version": 1,
            "row_type": "export_manifest",
            "format": self.name,
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "row_count": n,
            "source_timeline_rows": len(timeline_rows),
            "row_filter": "valid_mask.iphone && valid_mask.d435 && valid_mask.coinft",
            "mode": "smoke_test_not_physical_calibration",
            "outputs": {
                "zarr": relpath(zarr_path, paths.run_dir),
                "normalizer": relpath(normalizer_path, paths.run_dir),
                "pos_mapping": relpath(mapping_path, paths.run_dir),
            },
            "warnings": [
                "pos uses placeholder matrix conversion, not calibrated task-frame pose.",
                "action is finite difference of placeholder pos, not a real command stream.",
                "force is a raw CoinFT proxy from left channels 1..6, not calibrated 6D force.",
                "missing images are exported as black frames.",
            ],
        }
        manifest_path = target_dir / "EXPORT_MANIFEST.json"
        write_json(manifest_path, manifest)
        register_artifact(paths.run_dir, stream="export", role=self.name, path=target_dir, metadata={"kind": "directory"})
        append_sync_log(paths.run_dir, {"event": "exported_run", "format": self.name, "manifest": relpath(manifest_path, paths.run_dir)})
        return manifest

    def compute_episode_ends(self, rows: list[dict[str, Any]]) -> Any:
        import numpy as np

        ends: list[int] = []
        current_episode: Any = None
        for index, row in enumerate(rows, start=1):
            episode_index = row.get("episode_index")
            if current_episode is None:
                current_episode = episode_index
            elif episode_index != current_episode:
                ends.append(index - 1)
                current_episode = episode_index
        ends.append(len(rows))
        return np.asarray(ends, dtype=np.uint32)

    def blank_image(self, np: Any) -> Any:
        return np.zeros(self.image_shape, dtype=np.uint8)

    def chw_rgb_from_image(self, image: Any, cv2: Any, np: Any) -> Any:
        if image is None:
            return self.blank_image(np)
        image = cv2.resize(image, (320, 240), interpolation=cv2.INTER_AREA)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return image.transpose(2, 0, 1).astype(np.uint8)

    def load_iphone_image(self, run_dir: Path, row: dict[str, Any], cv2: Any, np: Any) -> Any:
        iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
        rgb = iphone.get("rgb", {}) if isinstance(iphone.get("rgb"), dict) else {}
        image_path = rgb.get("image_path")
        if not isinstance(image_path, str) or not image_path:
            return self.blank_image(np)
        image = cv2.imread(str(run_dir / image_path), cv2.IMREAD_COLOR)
        return self.chw_rgb_from_image(image, cv2, np)

    def load_d435_image(self, run_dir: Path, row: dict[str, Any], cv2: Any, np: Any, captures: dict[Path, Any]) -> Any:
        d435 = row.get("d435", {}) if isinstance(row.get("d435"), dict) else {}
        rgb = d435.get("rgb", {}) if isinstance(d435.get("rgb"), dict) else {}
        video_path = rgb.get("video_path")
        frame_index = rgb.get("frame_index")
        if not isinstance(video_path, str) or not isinstance(frame_index, int):
            return self.blank_image(np)
        absolute = run_dir / video_path
        capture = captures.get(absolute)
        if capture is None:
            capture = cv2.VideoCapture(str(absolute))
            captures[absolute] = capture
        if not capture.isOpened():
            return self.blank_image(np)
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            return self.blank_image(np)
        return self.chw_rgb_from_image(frame, cv2, np)


class ForceFlowV1ZarrExporter(ForceFlowSmokeZarrExporter):
    name = "forceflow-v1-zarr"
    description = "Create a ForceFlow V1 Zarr with user-world pose, force proxy, single-step pose delta, and next gripper state."

    def __init__(
        self,
        *,
        target_fps: float = 30.0,
        action_horizon_s: float = 0.2,
        gripper_action_window_s: tuple[float, float] = (0.3, 0.8),
        force_mode: str = "proxy",
        pose_mode: str = "user-world",
        gripper_input_scale: GripperInputScale = "auto",
        gripper_threshold: float = 0.5,
    ) -> None:
        self.target_fps = target_fps
        self.action_horizon_s = action_horizon_s
        self.gripper_action_window_s = gripper_action_window_s
        self.force_mode = coerce_force_mode(force_mode)
        self.pose_mode = pose_mode
        self.gripper_input_scale = gripper_input_scale
        self.gripper_threshold = gripper_threshold

    def export(self, run_dir: Path) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
            import zarr
        except ImportError as exc:
            raise SystemExit(
                "forceflow-v1-zarr requires cv2, numpy, and zarr. "
                "Run it in the ForceFlow environment or install these packages for this Python."
            ) from exc
        ensure_numpy_zarr_compat(np)

        paths = ensure_run_layout(run_dir)
        timeline_rows = list(iter_jsonl(paths.aligned_dir / "timeline.jsonl"))
        rows = [row for row in timeline_rows if self.row_is_v1_exportable(row)]
        if not rows:
            raise SystemExit(
                "No ForceFlow V1 exportable rows found. Need all-valid iPhone/D435/CoinFT rows "
                "with iPhone pose.valid=true and pose.world_calibrated=true."
            )

        target_dir = paths.exports_dir / "forceflow_v1_zarr"
        target_dir.mkdir(parents=True, exist_ok=True)
        task_name = paths.run_dir.name
        zarr_path = target_dir / f"{task_name}.zarr"
        if zarr_path.exists():
            shutil.rmtree(zarr_path)

        n = len(rows)
        pos = np.zeros((n, 6), dtype=np.float32)
        force = np.zeros((n, 2, 6), dtype=np.float32) if self.force_mode == "calibrated-dual" else np.zeros((n, 6), dtype=np.float32)
        gripper_state = np.zeros((n, 1), dtype=np.float32)
        episode = episode_ids_from_rows(rows)

        raw_force_values: list[Any] = []
        calibrated_force_values: list[Any] = []
        calibrated_dual_force_values: list[Any] = []
        for i, row in enumerate(rows):
            iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
            pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
            pos[i] = np.asarray(
                [
                    numeric(pose.get("x_m")),
                    numeric(pose.get("y_m")),
                    numeric(pose.get("z_m")),
                    math.radians(numeric(pose.get("roll_deg"))),
                    math.radians(numeric(pose.get("pitch_deg"))),
                    math.radians(numeric(pose.get("yaw_deg"))),
                ],
                dtype=np.float32,
            )

            gripper = iphone.get("gripper", {}) if isinstance(iphone.get("gripper"), dict) else {}
            gripper_state[i, 0] = raw_open_to_binary(
                gripper.get("open_percent"),
                input_scale=self.gripper_input_scale,
                threshold=self.gripper_threshold,
            )

            coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
            if self.force_mode == "calibrated-dual":
                wrench = calibrated_dual_wrench(row)
                if wrench is None or not calibrated_dual_wrench_is_ok(row):
                    raise SystemExit(
                        "ForceFlow V1 calibrated-dual export requires normalized/aligned "
                        "coinft.wrench or left_wrench/right_wrench with calibration.status=ok for every exported row. "
                        "Run normalize with a calibrated CoinFT model set or choose force_mode=proxy."
                    )
                calibrated_dual_force_values.append(np.asarray(wrench, dtype=np.float32))
            elif self.force_mode == "calibrated-left":
                wrench = calibrated_left_wrench(row)
                if wrench is None or not calibrated_left_wrench_is_ok(row):
                    raise SystemExit(
                        "ForceFlow V1 calibrated-left export requires normalized/aligned "
                        "coinft.left_wrench with calibration.status=ok for every exported row. "
                        "Run normalize with --coinft-config or choose force_mode=proxy."
                    )
                calibrated_force_values.append(np.asarray(wrench, dtype=np.float32))
            else:
                left_raw = coinft.get("left_raw") if isinstance(coinft.get("left_raw"), list) else []
                raw6 = np.asarray([(numeric(left_raw[j]) if j < len(left_raw) else 0.0) for j in range(6)], dtype=np.float32)
                raw_force_values.append(raw6)

        if self.force_mode == "calibrated-dual":
            force = np.stack(calibrated_dual_force_values).astype(np.float32)
        elif self.force_mode == "calibrated-left":
            force = np.stack(calibrated_force_values).astype(np.float32)
        elif raw_force_values:
            raw_force = np.stack(raw_force_values).astype(np.float32)
            baseline = raw_force[0:1]
            force = raw_force - baseline

        action = single_step_pose_delta(pos, episode)
        gripper_action = next_gripper_action(gripper_state, episode)

        root = zarr.open(str(zarr_path), mode="w")
        data_group = root.create_group("data")
        meta_group = root.create_group("meta")

        rgb_arm = create_zarr_array(data_group, "rgb_arm", shape=(n, *self.image_shape), dtype=np.uint8, chunks=(1, *self.image_shape))
        rgb_fix = create_zarr_array(data_group, "rgb_fix", shape=(n, *self.image_shape), dtype=np.uint8, chunks=(1, *self.image_shape))
        data_group.create_dataset("pos", data=pos, shape=pos.shape, dtype=np.float32)
        data_group.create_dataset("force", data=force, shape=force.shape, dtype=np.float32)
        if self.force_mode == "calibrated-dual":
            data_group.create_dataset("force_left", data=force[:, 0, :], shape=(n, 6), dtype=np.float32)
            data_group.create_dataset("force_right", data=force[:, 1, :], shape=(n, 6), dtype=np.float32)
        data_group.create_dataset("action", data=action, shape=action.shape, dtype=np.float32)
        data_group.create_dataset("gripper_state", data=gripper_state, shape=gripper_state.shape, dtype=np.float32)
        data_group.create_dataset("gripper_action", data=gripper_action, shape=gripper_action.shape, dtype=np.float32)
        data_group.create_dataset("episode", data=episode, shape=episode.shape, dtype=np.uint16)

        episode_ends = episode_ends_from_ids(episode)
        meta_group.create_dataset("episode_ends", data=episode_ends, shape=episode_ends.shape, dtype=np.uint32)

        captures: dict[Path, Any] = {}
        try:
            for i, row in enumerate(rows):
                rgb_arm[i] = self.load_iphone_image(paths.run_dir, row, cv2, np)
                rgb_fix[i] = self.load_d435_image(paths.run_dir, row, cv2, np, captures)
        finally:
            for capture in captures.values():
                capture.release()

        normalizer = {
            "pos": forceflow_minmax(pos),
            "action": forceflow_minmax(action),
            "force": forceflow_minmax(force),
        }
        normalizer_path = target_dir / f"{task_name}_normalizer.json"
        write_json(normalizer_path, normalizer)
        write_json(zarr_path / f"{task_name}_normalizer.json", normalizer)

        alignment_report = self.load_alignment_report(paths.aligned_dir / "ALIGNMENT_REPORT.json")
        manifest = {
            "schema_version": 1,
            "row_type": "export_manifest",
            "format": self.name,
            "run_id": paths.run_dir.name,
            "created_at": utc_now_iso(),
            "row_count": n,
            "source_timeline_rows": len(timeline_rows),
            "row_filter": "all-valid streams && iPhone pose.valid && iPhone pose.world_calibrated",
            "target_fps": self.target_fps,
            "target_dt_ms": 1000.0 / self.target_fps if self.target_fps > 0 else None,
            "timeline_mode": alignment_report.get("timeline", {}).get("mode"),
            "pose_mode": self.pose_mode,
            "pose_frame": "user_world",
            "pose_origin_definition": "center raycast first hit on an ARKit horizontal estimated plane at iPhone mark time",
            "pose_axis_definition": {
                "x": "mark-time iPhone camera forward vector projected onto the horizontal plane",
                "y": "cross(gravity_up, x), the horizontal right direction implied by the mark pose",
                "z": "ARKit gravity up",
            },
            "pose_calibration_point": "user-selected physical table/workspace point under the screen center at mark time",
            "pose_transform_source": "cameraInUserWorld = inverse(userWorldTransform) * ARFrame.camera.transform",
            "force_mode": self.force_mode,
            "force_mapping": self.force_mapping_manifest(),
            "action_mode": "single_step_pose_delta",
            "action_definition": "data/action[t] = pos[t+1] - pos[t] within the same episode",
            "action_horizon_steps": 1,
            "action_terminal_padding": "zero_pose_delta_at_episode_tail",
            "legacy_action_horizon_s_ignored": self.action_horizon_s,
            "gripper_input_scale": self.gripper_input_scale,
            "gripper_mapping_layer": "umift_laptop_alignment.pipeline.export.forceflow.gripper_mapping",
            "gripper_threshold": self.gripper_threshold,
            "gripper_binary_rule": "open_unit >= threshold -> 1.0 open else 0.0 closed",
            "gripper_state_mode": "binary_absolute_open_state",
            "gripper_action_mode": "next_binary_absolute_state",
            "gripper_action_definition": "data/gripper_action[t] = binary_gripper_state[t+1] within the same episode",
            "gripper_action_terminal_padding": "current_binary_state_at_episode_tail",
            "legacy_gripper_action_window_s_ignored": list(self.gripper_action_window_s),
            "outputs": {
                "zarr": relpath(zarr_path, paths.run_dir),
                "normalizer": relpath(normalizer_path, paths.run_dir),
            },
            "quality": self.quality_summary(timeline_rows, rows, alignment_report),
            "warnings": [
                *self.force_warnings(),
                "action is a single-step pose delta label from observed pose, not a robot command stream.",
                "gripper_action is the next observed binary gripper state, not a hardware command.",
                "missing images are exported as black frames.",
            ],
        }
        manifest_path = target_dir / "EXPORT_MANIFEST.json"
        write_json(manifest_path, manifest)
        write_json(zarr_path / "EXPORT_MANIFEST.json", manifest)
        register_artifact(paths.run_dir, stream="export", role=self.name, path=target_dir, metadata={"kind": "directory"})
        append_sync_log(paths.run_dir, {"event": "exported_run", "format": self.name, "manifest": relpath(manifest_path, paths.run_dir)})
        return manifest

    def row_is_v1_exportable(self, row: dict[str, Any]) -> bool:
        valid = row.get("valid_mask", {}) if isinstance(row.get("valid_mask"), dict) else {}
        if not all(bool(valid.get(name)) for name in ("iphone", "d435", "coinft")):
            return False
        iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
        pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
        return bool(pose.get("valid")) and bool(pose.get("world_calibrated")) and pose.get("frame") == "user_world"

    def force_mapping_manifest(self) -> dict[str, Any]:
        if self.force_mode == "calibrated-left":
            return {
                "mode": "coinft_left_wrench",
                "source": "aligned_sample.coinft.left_wrench",
                "physical_units": True,
                "units": ["N", "N", "N", "Nm", "Nm", "Nm"],
            }
        if self.force_mode == "calibrated-dual":
            return {
                "mode": "coinft_dual_wrench",
                "source": "aligned_sample.coinft.wrench or left_wrench/right_wrench",
                "shape": ["N", 2, 6],
                "side_order": ["left", "right"],
                "axes": ["fx", "fy", "fz", "mx", "my", "mz"],
                "physical_units": True,
                "units": ["N", "N", "N", "Nm", "Nm", "Nm"],
                "debug_arrays": ["data/force_left", "data/force_right"],
            }
        return {
            "mode": "left_raw_channels_0_to_5_minus_first_sample_baseline",
            "source": "aligned_sample.coinft.left_raw[0:6]",
            "physical_units": False,
        }

    def force_warnings(self) -> list[str]:
        if self.force_mode in {"calibrated-left", "calibrated-dual"}:
            return []
        return ["force is a proxy from raw left channels 0..5, baseline-subtracted, not calibrated physical 6D force."]

    def load_alignment_report(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def quality_summary(
        self,
        timeline_rows: list[dict[str, Any]],
        exported_rows: list[dict[str, Any]],
        alignment_report: dict[str, Any],
    ) -> dict[str, Any]:
        total = max(1, len(timeline_rows))
        counts = alignment_report.get("counts", {}) if isinstance(alignment_report.get("counts"), dict) else {}
        stream_valid = {"iphone": 0, "d435": 0, "coinft": 0, "all": 0}
        pose_valid = 0
        world_calibrated = 0
        for row in timeline_rows:
            valid_mask = row.get("valid_mask", {}) if isinstance(row.get("valid_mask"), dict) else {}
            iphone_ok = bool(valid_mask.get("iphone"))
            d435_ok = bool(valid_mask.get("d435"))
            coinft_ok = bool(valid_mask.get("coinft"))
            stream_valid["iphone"] += int(iphone_ok)
            stream_valid["d435"] += int(d435_ok)
            stream_valid["coinft"] += int(coinft_ok)
            stream_valid["all"] += int(iphone_ok and d435_ok and coinft_ok)
            iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
            pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
            pose_valid += int(bool(pose.get("valid")))
            world_calibrated += int(bool(pose.get("world_calibrated")))
        iphone_valid = counts.get("iphone_valid")
        d435_valid = counts.get("d435_valid")
        coinft_valid = counts.get("coinft_valid")
        all_valid = counts.get("all_valid")
        return {
            "timeline_rows": len(timeline_rows),
            "exported_rows": len(exported_rows),
            "exported_ratio": len(exported_rows) / total,
            "iphone_valid_ratio": float(iphone_valid if isinstance(iphone_valid, (int, float)) else stream_valid["iphone"]) / total,
            "d435_valid_ratio": float(d435_valid if isinstance(d435_valid, (int, float)) else stream_valid["d435"]) / total,
            "coinft_valid_ratio": float(coinft_valid if isinstance(coinft_valid, (int, float)) else stream_valid["coinft"]) / total,
            "all_valid_ratio": float(all_valid if isinstance(all_valid, (int, float)) else stream_valid["all"]) / total,
            "pose_valid_ratio": pose_valid / total,
            "world_calibrated_ratio": world_calibrated / total,
        }


EXPORTERS: dict[str, Exporter] = {
    exporter.name: exporter
    for exporter in (
        DebugJsonlExporter(),
        CsvIndexExporter(),
        ForceFlowMappingTemplateExporter(),
        ForceFlowSmokeZarrExporter(),
        ForceFlowV1ZarrExporter(),
    )
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export an aligned UMIFT run using a format adapter.")
    parser.add_argument("run_dir", type=Path, nargs="?")
    parser.add_argument("--format", choices=sorted(EXPORTERS), default="index-csv")
    parser.add_argument("--list-formats", action="store_true")
    parser.add_argument("--target-fps", type=float, default=30.0, help="forceflow-v1-zarr target FPS metadata.")
    parser.add_argument(
        "--action-horizon-s",
        type=float,
        default=0.2,
        help="Deprecated compatibility flag; V1 now always exports single-step pose delta.",
    )
    parser.add_argument(
        "--gripper-action-window-s",
        default="0.3,0.8",
        help="Deprecated compatibility flag; V1 now uses the next binary gripper state.",
    )
    parser.add_argument("--force-mode", choices=["proxy", "calibrated", "calibrated-left", "calibrated-dual"], default="proxy")
    parser.add_argument("--pose-mode", choices=["user-world", "camera-tcp"], default="user-world")
    parser.add_argument(
        "--gripper-input-scale",
        choices=["auto", "unit_0_1", "percent_0_100"],
        default="auto",
        help="Scale of the captured gripper opening value before binary thresholding.",
    )
    parser.add_argument("--gripper-threshold", type=float, default=0.5, help="Open-unit threshold for binary gripper mapping.")
    args = parser.parse_args()

    if args.list_formats:
        for name, exporter in sorted(EXPORTERS.items()):
            print(f"{name}\t{exporter.description}")
        return 0
    if args.run_dir is None:
        raise SystemExit("run_dir is required unless --list-formats is used.")

    exporter = EXPORTERS[args.format]
    if args.format == ForceFlowV1ZarrExporter.name:
        try:
            start_s, end_s = (float(part.strip()) for part in args.gripper_action_window_s.split(",", 1))
        except ValueError as exc:
            raise SystemExit("--gripper-action-window-s must look like '0.3,0.8'") from exc
        exporter = ForceFlowV1ZarrExporter(
            target_fps=args.target_fps,
            action_horizon_s=args.action_horizon_s,
            gripper_action_window_s=(start_s, end_s),
            force_mode=args.force_mode,
            pose_mode=args.pose_mode,
            gripper_input_scale=args.gripper_input_scale,
            gripper_threshold=args.gripper_threshold,
        )

    manifest = exporter.export(args.run_dir.expanduser().resolve())
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
