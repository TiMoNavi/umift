"""Incremental UMI-FT replay-buffer Zarr exporter."""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from umift_laptop_alignment.orchestration.file_lock import file_lock
from umift_laptop_alignment.orchestration.run_layout import (
    append_sync_log,
    ensure_run_layout,
    read_json,
    register_artifact,
    relpath,
    write_json_atomic,
)
from umift_laptop_alignment.pipeline.episode_exclusions import load_episode_exclusions
from umift_laptop_alignment.pipeline.export.base import ExportConfig, ExportManifest, ExporterPlugin, merged_config
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import iter_jsonl
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.constants import (
    COINFT_TO_GOPRO_Y_M,
    COINFT_TO_GOPRO_Z_M,
    DEFAULT_GRIPPER_MAX_WIDTH_M,
    IPHONE_CAMERA_T_GRIPPER_CENTER_TCP,
)
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.depth import read_d435_depth_scale
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.episode_arrays import build_episode_arrays
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.manifest import build_export_manifest
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.selection import (
    coinft_samples_for_episode,
    row_is_exportable,
)
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.writer import (
    ensure_numpy_zarr_compat,
    write_episode_group,
    write_meta_lengths,
)


_EXPORT_LOCKS_GUARD = threading.Lock()
_EXPORT_THREAD_LOCKS: dict[Path, threading.RLock] = {}


@contextmanager
def dataset_export_lock(target_dir: Path) -> Iterator[None]:
    target_dir = target_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / ".incremental_export.lock"
    with _EXPORT_LOCKS_GUARD:
        thread_lock = _EXPORT_THREAD_LOCKS.setdefault(target_dir, threading.RLock())
    with thread_lock, file_lock(lock_path):
        yield


def _episode_index(name: str) -> int:
    return int(name.rsplit("_", 1)[-1])


def _episode_name(index: int) -> str:
    return f"episode_{index}"


def _source_episode_name(index: int) -> str:
    return f"episode_{index:06d}"


def _load_runtime() -> tuple[Any, Any, Any, Any]:
    try:
        import cv2
        import numpy as np
        import zarr
        from numcodecs import Blosc
    except ImportError as exc:
        raise SystemExit(
            "umift-replay-buffer-zarr requires cv2, numpy, zarr, and numcodecs."
        ) from exc
    ensure_numpy_zarr_compat(np)
    return cv2, np, zarr, Blosc


def _compressors(Blosc: Any) -> tuple[Any, Any, Any]:
    return (
        Blosc(cname="zstd", clevel=5, shuffle=Blosc.SHUFFLE),
        Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE),
        Blosc(cname="zstd", clevel=5, shuffle=Blosc.SHUFFLE),
    )


def _ensure_dataset(zarr_path: Path, zarr: Any) -> Any:
    if zarr_path.exists():
        root = zarr.open_group(str(zarr_path), mode="a")
    else:
        root = zarr.group(store=zarr.DirectoryStore(str(zarr_path)), overwrite=False)
    if "data" not in root:
        root.create_group("data")
    if "meta" not in root:
        root.create_group("meta")
    return root


def _existing_summary_map(manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in manifest.get("episodes", []):
        if not isinstance(row, dict):
            continue
        source_index = row.get("source_episode_index")
        if isinstance(source_index, int):
            result[source_index] = dict(row)
    return result


def _dataset_state(
    zarr_path: Path,
    *,
    zarr: Any,
    existing_manifest: dict[str, Any],
    current_source_index: int | None = None,
    current_counts: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    root = zarr.open_group(str(zarr_path), mode="r")
    data = root["data"]
    previous = _existing_summary_map(existing_manifest)
    summaries: list[dict[str, Any]] = []
    lengths: dict[str, list[int]] = {
        "episode_rgb0_len": [],
        "episode_robot0_len": [],
        "episode_gripper0_len": [],
        "episode_wrench0_len": [],
    }
    group_names = sorted(data.group_keys(), key=_episode_index)
    has_depth = any("depth_0" in data[name] for name in group_names)
    has_global_rgb = any("rgb_global_0" in data[name] for name in group_names)
    if has_depth:
        lengths["episode_depth0_len"] = []
    if has_global_rgb:
        lengths["episode_rgb_global0_len"] = []

    for logical_index, name in enumerate(group_names):
        group = data[name]
        source_index = group.attrs.get("source_episode_index")
        session_id = group.attrs.get("iphone_session_id")
        rgb_len = int(group["rgb_0"].shape[0])
        robot_len = int(group["ts_pose_fb_0"].shape[0])
        gripper_len = int(group["gripper_0"].shape[0])
        wrench_len = int(group["wrench_left_0"].shape[0])
        depth_len = int(group["depth_0"].shape[0]) if "depth_0" in group else 0
        global_len = int(group["rgb_global_0"].shape[0]) if "rgb_global_0" in group else 0
        summary = dict(previous.get(source_index, {})) if isinstance(source_index, int) else {}
        summary.update(
            {
                "episode_index": logical_index,
                "zarr_episode_index": logical_index,
                "source_episode_index": source_index,
                "iphone_session_id": session_id,
                "start_aligned_monotonic_ns": group.attrs.get("start_aligned_monotonic_ns"),
                "end_aligned_monotonic_ns": group.attrs.get("end_aligned_monotonic_ns"),
                "rgb0_len": rgb_len,
                "robot0_len": robot_len,
                "gripper0_len": gripper_len,
                "wrench0_len": wrench_len,
                "depth0_len": depth_len if has_depth else None,
                "source_timeline_rows": rgb_len,
                "exported_timeline_rows": rgb_len,
            }
        )
        if has_global_rgb:
            summary["rgb_global0_len"] = global_len
        if source_index == current_source_index and current_counts is not None:
            summary.update(current_counts)
        summaries.append(summary)
        lengths["episode_rgb0_len"].append(rgb_len)
        lengths["episode_robot0_len"].append(robot_len)
        lengths["episode_gripper0_len"].append(gripper_len)
        lengths["episode_wrench0_len"].append(wrench_len)
        if has_depth:
            lengths["episode_depth0_len"].append(depth_len)
        if has_global_rgb:
            lengths["episode_rgb_global0_len"].append(global_len)
    return summaries, lengths


def _stage_meta(
    staging_dir: Path,
    *,
    lengths: dict[str, list[int]],
    zarr: Any,
    np: Any,
    numeric_compressor: Any,
) -> Path:
    path = staging_dir / f"meta-{uuid.uuid4().hex}"
    group = zarr.group(store=zarr.DirectoryStore(str(path)), overwrite=True)
    write_meta_lengths(group, lengths=lengths, np=np, numeric_compressor=numeric_compressor)
    return path


def _manifest_for_dataset(
    *,
    exporter: "UmiFTReplayBufferExporter",
    paths: Any,
    zarr_path: Path,
    config_path: Path,
    effective_config: dict[str, Any],
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    global_cfg = effective_config["image"].get("global_camera", {})
    write_global_rgb = bool(global_cfg.get("enabled", True)) and bool(global_cfg.get("write_rgb", True))
    write_depth = bool(global_cfg.get("enabled", True)) and bool(global_cfg.get("write_depth_0", True))
    manifest = build_export_manifest(
        exporter_name=exporter.name,
        exporter_version=exporter.version,
        paths=paths,
        zarr_path=zarr_path,
        config_path=config_path,
        required_inputs=exporter.required_inputs(),
        effective_config=effective_config,
        source_timeline_rows=sum(int(row.get("source_timeline_rows") or 0) for row in summaries),
        exported_timeline_rows=sum(int(row.get("exported_timeline_rows") or 0) for row in summaries),
        episode_summaries=summaries,
        write_global_rgb=write_global_rgb,
        global_rgb_field=str(global_cfg.get("rgb_field", "rgb_global_0")),
        write_depth=write_depth,
    )
    manifest["incremental"] = True
    manifest["episode_index_mapping"] = [
        {
            "source_episode_index": row.get("source_episode_index"),
            "zarr_episode_index": row.get("zarr_episode_index"),
            "iphone_session_id": row.get("iphone_session_id"),
        }
        for row in summaries
    ]
    manifest["episode_exclusions"] = load_episode_exclusions(paths.run_dir)
    return manifest


def _swap_meta(zarr_path: Path, staged_meta: Path) -> Path | None:
    meta_path = zarr_path / "meta"
    backup = staged_meta.parent / f"meta-backup-{uuid.uuid4().hex}"
    if meta_path.exists():
        os.replace(meta_path, backup)
    else:
        backup = None
    os.replace(staged_meta, meta_path)
    return backup


def _restore_json(path: Path, previous: dict[str, Any] | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        write_json_atomic(path, previous)


class UmiFTReplayBufferExporter(ExporterPlugin):
    name = "umift-replay-buffer-zarr"
    description = "Incremental UMI-FT episode replay buffer Zarr exporter."
    version = "0.4"

    def default_config(self) -> ExportConfig:
        return {
            "export": {"output_name": "acp_replay_buffer_gripper.zarr"},
            "pose": {
                "reference_frame": "user_world",
                "source_frame": "iphone_camera",
                "target_frame": "gripper_center_tcp",
                "representation": "pose7_qwxyz",
                "euler_input_convention": "roll_pitch_yaw_degrees__Rz_yaw_Ry_pitch_Rx_roll",
                "translation_unit": "m",
                "transform": "iphone_camera_to_gripper_center_tcp",
                "iphone_camera_t_gripper_center_tcp": [list(row) for row in IPHONE_CAMERA_T_GRIPPER_CENTER_TCP],
                "is_robot_base_absolute": False,
            },
            "timestamps": {"unit": "seconds", "origin": "episode_start_aligned_monotonic_ns"},
            "image": {
                "rgb_0": {"source": "iphone", "size": [224, 224], "layout": "THWC"},
                "global_camera": {
                    "enabled": True,
                    "source": "d435",
                    "write_rgb": True,
                    "rgb_field": "rgb_global_0",
                    "write_depth_0": True,
                    "depth_clip_m": 0.5,
                    "size": [224, 224],
                    "layout": "THWC",
                },
            },
            "gripper": {
                "source": "iphone_open_percent",
                "output": "width_m_from_open_fraction_linear",
                "fallback_min_width_m": 0.0,
                "fallback_max_width_m": DEFAULT_GRIPPER_MAX_WIDTH_M,
            },
            "force": {
                "source": "coinft_dual_calibrated",
                "write_coinft_passthrough": True,
                "write_tool_frame": "umi_coinft_to_tcp",
                "write_concat": True,
                "units": ["N", "N", "N", "Nm", "Nm", "Nm"],
                "coinft_to_tcp": {
                    "dist_coinft2gopro_along_cam_z_m": COINFT_TO_GOPRO_Z_M,
                    "dist_coinft2gopro_along_cam_y_m": COINFT_TO_GOPRO_Y_M,
                    "gripper_width_source": "gripper_width_m_or_open_fraction_linear",
                },
            },
            "actions": {
                "ts_pose_command_0": "copy_ts_pose_fb_0",
                "virtual_target": "placeholder_equal_pose_fb",
                "stiffness": "placeholder_constant",
                "stiffness_constant": 0.0,
            },
            "quality": {"max_missing_media_ratio": 0.2},
        }

    def required_inputs(self) -> list[str]:
        return [
            "aligned/episodes/episode_XXXXXX/timeline.jsonl",
            "normalized/episodes/episode_XXXXXX/coinft_samples.jsonl",
            "raw/iphone_stream/<session>",
            "raw/global_camera/episode_XXXXXX",
        ]

    def export(self, run_dir: Path, *, config: ExportConfig | None = None) -> ExportManifest:
        raise SystemExit("Run-level full export was removed. Export a specific Episode with export_episode().")

    def export_episode(
        self,
        run_dir: Path,
        source_episode_index: int,
        *,
        config: ExportConfig | None = None,
    ) -> ExportManifest:
        cv2, np, zarr, Blosc = _load_runtime()
        image_compressor, numeric_compressor, depth_compressor = _compressors(Blosc)
        paths = ensure_run_layout(run_dir)
        effective_config = merged_config(self.default_config(), config)
        # Keep output naming and task-local export choices with the Dataset,
        # rather than reverting to the shared profile after a server restart.
        stored_config_path = paths.exports_dir / "umift_replay_buffer_zarr" / "EXPORT_CONFIG.json"
        effective_config = merged_config(effective_config, read_json(stored_config_path, {}))
        source_name = _source_episode_name(source_episode_index)
        aligned_dir = paths.aligned_dir / "episodes" / source_name
        normalized_dir = paths.normalized_dir / "episodes" / source_name
        timeline_rows = list(iter_jsonl(aligned_dir / "timeline.jsonl"))
        rows = [row for row in timeline_rows if row_is_exportable(row)]
        if not rows:
            raise SystemExit(
                f"Episode {source_episode_index} has no exportable iPhone RGB/pose timeline rows."
            )
        episode_info = read_json(aligned_dir / "episode.json", {})
        start_ns = episode_info.get("start_aligned_monotonic_ns")
        end_ns = episode_info.get("end_aligned_monotonic_ns")
        session_id = episode_info.get("session_id")
        if not isinstance(start_ns, int) or not isinstance(end_ns, int):
            raise SystemExit(f"Episode {source_episode_index} has invalid aligned recording bounds.")
        if not isinstance(session_id, str) or not session_id:
            raise SystemExit(f"Episode {source_episode_index} has no iPhone session mapping.")
        coinft_samples = list(iter_jsonl(normalized_dir / "coinft_samples.jsonl"))
        coinft_episode = coinft_samples_for_episode(
            coinft_samples,
            episode_rows=rows,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        if not coinft_episode:
            raise SystemExit(f"Episode {source_episode_index} has no calibrated dual CoinFT samples.")

        image_size = tuple(int(value) for value in effective_config["image"]["rgb_0"].get("size", [224, 224]))
        global_cfg = effective_config["image"].get("global_camera", {})
        global_enabled = bool(global_cfg.get("enabled", True))
        write_global_rgb = global_enabled and bool(global_cfg.get("write_rgb", True))
        write_depth = global_enabled and bool(global_cfg.get("write_depth_0", True))
        global_rgb_field = str(global_cfg.get("rgb_field", "rgb_global_0"))
        global_size = tuple(int(value) for value in global_cfg.get("size", list(image_size)))
        arrays, counts = build_episode_arrays(
            paths.run_dir,
            episode_rows=rows,
            coinft_rows=coinft_episode,
            start_ns=start_ns,
            image_size=image_size,
            global_size=global_size,
            write_global_rgb=write_global_rgb,
            write_depth=write_depth,
            global_rgb_field=global_rgb_field,
            d435_depth_scale=read_d435_depth_scale(paths.run_dir, source_episode_index),
            depth_clip_m=float(global_cfg.get("depth_clip_m", 0.5)),
            stiffness_constant=float(effective_config["actions"].get("stiffness_constant", 0.0)),
            pose_config=effective_config["pose"],
            gripper_config=effective_config["gripper"],
            force_config=effective_config["force"],
            cv2=cv2,
            np=np,
        )
        media_total = int(arrays["rgb_0"].shape[0])
        max_missing_ratio = float(effective_config.get("quality", {}).get("max_missing_media_ratio", 0.2))
        missing_labels = {
            "missing_iphone_images": "iPhone RGB",
            "missing_global_rgb_frames": "D435 RGB",
            "missing_depth_frames": "D435 depth",
            "missing_iphone_depth_frames": "iPhone depth",
        }
        media_errors = []
        for key, label in missing_labels.items():
            ratio = float(counts.get(key, 0)) / float(max(1, media_total))
            if ratio > max_missing_ratio:
                media_errors.append(f"{label} missing ratio is too high ({ratio:.1%})")
        if media_errors:
            raise SystemExit("; ".join(media_errors))

        target_dir = paths.exports_dir / "umift_replay_buffer_zarr"
        target_dir.mkdir(parents=True, exist_ok=True)
        zarr_path = target_dir / str(effective_config["export"]["output_name"])
        staging_dir = target_dir / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_group = staging_dir / f"{source_name}-{uuid.uuid4().hex}"
        group = zarr.group(store=zarr.DirectoryStore(str(staged_group)), overwrite=True)
        group.attrs["source_episode_index"] = source_episode_index
        group.attrs["iphone_session_id"] = session_id
        group.attrs["start_aligned_monotonic_ns"] = start_ns
        group.attrs["end_aligned_monotonic_ns"] = end_ns
        write_episode_group(
            group,
            arrays,
            image_compressor=image_compressor,
            numeric_compressor=numeric_compressor,
            depth_compressor=depth_compressor,
        )

        config_path = target_dir / "EXPORT_CONFIG.json"
        manifest_path = target_dir / "UMIFT_EXPORT_MANIFEST.json"
        try:
            with dataset_export_lock(target_dir):
                root = _ensure_dataset(zarr_path, zarr)
                data = root["data"]
                logical_index: int | None = None
                for name in data.group_keys():
                    if data[name].attrs.get("source_episode_index") == source_episode_index:
                        logical_index = _episode_index(name)
                        break
                if logical_index is None:
                    indices = [_episode_index(name) for name in data.group_keys()]
                    logical_index = max(indices, default=-1) + 1
                group = zarr.open_group(str(staged_group), mode="a")
                group.attrs["zarr_episode_index"] = logical_index

                final_group = zarr_path / "data" / _episode_name(logical_index)
                group_backup = staging_dir / f"episode-backup-{uuid.uuid4().hex}"
                previous_outer_manifest = read_json(manifest_path, None)
                previous_inner_manifest = read_json(zarr_path / "UMIFT_EXPORT_MANIFEST.json", None)
                previous_config = read_json(config_path, None)
                meta_backup: Path | None = None
                if final_group.exists():
                    os.replace(final_group, group_backup)
                else:
                    group_backup = None
                os.replace(staged_group, final_group)
                try:
                    existing_manifest = previous_outer_manifest or previous_inner_manifest or {}
                    summaries, lengths = _dataset_state(
                        zarr_path,
                        zarr=zarr,
                        existing_manifest=existing_manifest,
                        current_source_index=source_episode_index,
                        current_counts=counts,
                    )
                    staged_meta = _stage_meta(
                        staging_dir,
                        lengths=lengths,
                        zarr=zarr,
                        np=np,
                        numeric_compressor=numeric_compressor,
                    )
                    meta_backup = _swap_meta(zarr_path, staged_meta)
                    manifest = _manifest_for_dataset(
                        exporter=self,
                        paths=paths,
                        zarr_path=zarr_path,
                        config_path=config_path,
                        effective_config=effective_config,
                        summaries=summaries,
                    )
                    write_json_atomic(config_path, effective_config)
                    write_json_atomic(manifest_path, manifest)
                    write_json_atomic(zarr_path / "UMIFT_EXPORT_MANIFEST.json", manifest)
                except Exception:
                    if meta_backup is not None and meta_backup.exists():
                        shutil.rmtree(zarr_path / "meta", ignore_errors=True)
                        os.replace(meta_backup, zarr_path / "meta")
                    if final_group.exists():
                        shutil.rmtree(final_group)
                    if group_backup is not None and group_backup.exists():
                        os.replace(group_backup, final_group)
                    _restore_json(manifest_path, previous_outer_manifest)
                    _restore_json(zarr_path / "UMIFT_EXPORT_MANIFEST.json", previous_inner_manifest)
                    _restore_json(config_path, previous_config)
                    raise
                if group_backup is not None:
                    shutil.rmtree(group_backup, ignore_errors=True)
                if meta_backup is not None:
                    shutil.rmtree(meta_backup, ignore_errors=True)
        finally:
            shutil.rmtree(staged_group, ignore_errors=True)

        register_artifact(
            paths.run_dir,
            stream="export",
            role=self.name,
            path=target_dir,
            metadata={"kind": "directory", "incremental": True},
        )
        append_sync_log(
            paths.run_dir,
            {
                "event": "exported_episode",
                "format": self.name,
                "source_episode_index": source_episode_index,
                "zarr_episode_index": logical_index,
                "manifest": relpath(manifest_path, paths.run_dir),
            },
        )
        return manifest

    def delete_episode(
        self,
        run_dir: Path,
        zarr_path: Path,
        logical_index: int,
    ) -> ExportManifest:
        _, np, zarr, Blosc = _load_runtime()
        _, numeric_compressor, _ = _compressors(Blosc)
        paths = ensure_run_layout(run_dir)
        target_dir = zarr_path.parent
        config_path = target_dir / "EXPORT_CONFIG.json"
        manifest_path = target_dir / "UMIFT_EXPORT_MANIFEST.json"
        effective_config = merged_config(self.default_config(), read_json(config_path, {}))
        staging_dir = target_dir / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        with dataset_export_lock(target_dir):
            root = zarr.open_group(str(zarr_path), mode="a")
            data = root["data"]
            target_name = _episode_name(logical_index)
            if target_name not in data:
                raise KeyError(f"unknown Zarr Episode {target_name}")
            names = sorted(data.group_keys(), key=_episode_index)
            previous_outer_manifest = read_json(manifest_path, None)
            previous_inner_manifest = read_json(zarr_path / "UMIFT_EXPORT_MANIFEST.json", None)
            removed_backup = staging_dir / f"deleted-episode-{uuid.uuid4().hex}"
            os.replace(zarr_path / "data" / target_name, removed_backup)
            moves: list[tuple[Path, Path]] = []
            meta_backup: Path | None = None
            try:
                for name in names:
                    index = _episode_index(name)
                    if index <= logical_index:
                        continue
                    source = zarr_path / "data" / name
                    destination = zarr_path / "data" / _episode_name(index - 1)
                    os.replace(source, destination)
                    moves.append((source, destination))
                    zarr.open_group(str(destination), mode="a").attrs["zarr_episode_index"] = index - 1
                summaries, lengths = _dataset_state(
                    zarr_path,
                    zarr=zarr,
                    existing_manifest=previous_outer_manifest or previous_inner_manifest or {},
                )
                staged_meta = _stage_meta(
                    staging_dir,
                    lengths=lengths,
                    zarr=zarr,
                    np=np,
                    numeric_compressor=numeric_compressor,
                )
                meta_backup = _swap_meta(zarr_path, staged_meta)
                manifest = _manifest_for_dataset(
                    exporter=self,
                    paths=paths,
                    zarr_path=zarr_path,
                    config_path=config_path,
                    effective_config=effective_config,
                    summaries=summaries,
                )
                write_json_atomic(manifest_path, manifest)
                write_json_atomic(zarr_path / "UMIFT_EXPORT_MANIFEST.json", manifest)
            except Exception:
                if meta_backup is not None and meta_backup.exists():
                    shutil.rmtree(zarr_path / "meta", ignore_errors=True)
                    os.replace(meta_backup, zarr_path / "meta")
                for source, destination in reversed(moves):
                    os.replace(destination, source)
                    zarr.open_group(str(source), mode="a").attrs["zarr_episode_index"] = _episode_index(source.name)
                os.replace(removed_backup, zarr_path / "data" / target_name)
                _restore_json(manifest_path, previous_outer_manifest)
                _restore_json(zarr_path / "UMIFT_EXPORT_MANIFEST.json", previous_inner_manifest)
                raise
            shutil.rmtree(removed_backup, ignore_errors=True)
            if meta_backup is not None:
                shutil.rmtree(meta_backup, ignore_errors=True)
        return manifest
