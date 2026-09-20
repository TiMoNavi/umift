"""Depth loading and conversion for UMI-FT replay-buffer export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_d435_depth(
    run_dir: Path,
    d435: dict[str, Any],
    cv2: Any,
    np: Any,
    size: tuple[int, int],
    depth_scale_m: float,
    clip_m: float,
) -> Any | None:
    depth = d435.get("depth", {}) if isinstance(d435.get("depth"), dict) else {}
    raw_path = depth.get("raw_path")
    shape = depth.get("shape")
    dtype = depth.get("dtype")
    byte_offset = depth.get("byte_offset")
    if (
        not isinstance(raw_path, str)
        or not isinstance(shape, list)
        or len(shape) != 2
        or not isinstance(dtype, str)
        or not isinstance(byte_offset, int)
    ):
        return None
    height, width = int(shape[0]), int(shape[1])
    item_dtype = np.dtype(dtype)
    byte_count = height * width * item_dtype.itemsize
    path = run_dir / raw_path
    try:
        with path.open("rb") as handle:
            handle.seek(byte_offset)
            payload = handle.read(byte_count)
    except OSError:
        return None
    if len(payload) != byte_count:
        return None
    depth_raw = np.frombuffer(payload, dtype=item_dtype).reshape(height, width)
    depth_m = depth_raw.astype(np.float32) * float(depth_scale_m)
    depth_m = np.clip(depth_m, 0.0, clip_m)
    resized = cv2.resize(depth_m, size, interpolation=cv2.INTER_NEAREST)
    return np.repeat(resized[..., None], 3, axis=2).astype(np.float16)


def read_d435_depth_scale(run_dir: Path, source_episode_index: int | None = None) -> float:
    if isinstance(source_episode_index, int):
        metadata_paths = [
            run_dir / "raw" / "global_camera" / f"episode_{source_episode_index:06d}" / "camera_metadata.json"
        ]
    else:
        metadata_paths = sorted((run_dir / "raw" / "global_camera").glob("episode_*/camera_metadata.json"))
    for path in metadata_paths:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        streams = metadata.get("streams", {}) if isinstance(metadata.get("streams"), dict) else {}
        depth = streams.get("depth", {}) if isinstance(streams.get("depth"), dict) else {}
        scale = depth.get("depthScaleMeters")
        if isinstance(scale, (int, float)) and scale > 0:
            return float(scale)
    return 0.001


def load_iphone_depth(run_dir: Path, info: dict[str, Any], np: Any, cv2: Any, size: tuple[int, int], clip_m: float = 0.5) -> Any | None:
    path_text = info.get("depth_path")
    width, height = info.get("width"), info.get("height")
    if not isinstance(path_text, str) or not isinstance(width, int) or not isinstance(height, int):
        return None
    byte_offset = info.get("byte_offset", 0)
    if not isinstance(byte_offset, int) or byte_offset < 0:
        return None
    byte_count = info.get("byte_count", width * height * 2)
    if not isinstance(byte_count, int) or byte_count != width * height * 2:
        return None
    try:
        with (run_dir / path_text).open("rb") as handle:
            handle.seek(byte_offset)
            payload = handle.read(byte_count)
    except OSError:
        return None
    if len(payload) != byte_count:
        return None
    raw = np.frombuffer(payload, dtype=np.dtype(str(info.get("dtype") or "<f2")))
    if raw.size != width * height:
        return None
    depth = raw.reshape(height, width).astype(np.float32)
    if str(info.get("unit") or "m").lower() in {"mm", "millimeter", "millimeters"}:
        depth *= 0.001
    depth = np.clip(depth, 0.0, clip_m)
    depth = cv2.resize(depth, size, interpolation=cv2.INTER_NEAREST)
    return np.repeat(depth[..., None], 3, axis=2).astype(np.float16)
