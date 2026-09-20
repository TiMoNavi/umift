"""Run normalize, align, export, and validation after a recording stops."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from umift_laptop_alignment.orchestration.run_layout import append_sync_log, update_manifest
from umift_laptop_alignment.pipeline.export.controller import ExportController
from umift_laptop_alignment.pipeline.alignment.runner import align_episode
from umift_laptop_alignment.pipeline.normalize.runner import normalize_episode
from umift_laptop_alignment.pipeline.transforms.iphone_v2 import closed_recording_windows


@dataclass(frozen=True)
class PostprocessConfig:
    cleanup_intermediates: bool = False
    target_fps: float = 20.0
    iphone_max_delta_ms: float = 50.0
    d435_max_delta_ms: float = 80.0
    coinft_max_delta_ms: float = 25.0
    alignment_min_valid_ratio: float = 0.8
    max_missing_media_ratio: float = 0.2


def _files_exist(run_dir: Path, relative_paths: list[str]) -> list[str]:
    return [path for path in relative_paths if not (run_dir / path).exists()]


def validate_inputs(run_dir: Path, source_episode_index: int) -> dict[str, Any]:
    episode_name = f"episode_{source_episode_index:06d}"
    required = [
        "raw/iphone_stream",
        f"raw/coinft/{episode_name}",
        f"raw/global_camera/{episode_name}",
    ]
    missing = _files_exist(run_dir, required)
    errors: list[str] = []
    try:
        iphone_windows = closed_recording_windows(run_dir, strict=False)
    except (OSError, ValueError) as exc:
        iphone_windows = []
        errors.append(f"invalid iPhone Protocol V2 archive: {exc}")
    if not iphone_windows:
        errors.append("missing closed iPhone Protocol V2 recording window")
    if not any((run_dir / "raw/coinft" / episode_name).glob("*coinft_stream.csv")):
        errors.append("missing CoinFT CSV")
    if not (run_dir / "raw/global_camera" / episode_name / "frame_timestamps.csv").is_file():
        errors.append("missing D435 frame timestamps")
    return {"ok": not missing and not errors, "missing": missing, "errors": errors}


def validate_export(run_dir: Path, manifest: dict[str, Any], source_episode_index: int) -> dict[str, Any]:
    output = manifest.get("outputs", {}).get("zarr")
    required = ["data", "meta"]
    missing = []
    if not output:
        missing.append("export manifest outputs.zarr")
    else:
        zarr_dir = run_dir / str(output)
        missing.extend(name for name in required if not (zarr_dir / name).exists())
        matching = [
            row
            for row in manifest.get("episodes", [])
            if isinstance(row, dict) and row.get("source_episode_index") == source_episode_index
        ]
        if len(matching) != 1:
            missing.append(f"source episode_{source_episode_index:06d} mapping")
    return {"ok": not missing, "missing": missing}


def cleanup_intermediates(run_dir: Path, source_episode_index: int) -> list[str]:
    removed: list[str] = []
    episode_name = f"episode_{source_episode_index:06d}"
    for name in ("normalized", "aligned"):
        path = run_dir / name / "episodes" / episode_name
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path.relative_to(run_dir)))
    return removed


def validate_normalization(report: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    iphone = report.get("iphone", {}) if isinstance(report.get("iphone"), dict) else {}
    d435 = report.get("d435", {}) if isinstance(report.get("d435"), dict) else {}
    coinft = report.get("coinft", {}) if isinstance(report.get("coinft"), dict) else {}
    if int(iphone.get("frame_count") or 0) <= 0:
        reasons.append("normalized iPhone frames are missing")
    if int(iphone.get("event_count") or 0) != 2:
        reasons.append("Episode must contain exactly one iPhone record_start/record_stop pair")
    if int(d435.get("frame_count") or 0) <= 0:
        reasons.append("normalized D435 frames are missing")
    d435_integrity = d435.get("sensor_integrity", {}) if isinstance(d435.get("sensor_integrity"), dict) else {}
    if d435_integrity and not bool(d435_integrity.get("ok")):
        reasons.append(
            "D435 sensor frame sequence is discontinuous "
            f"(missing={int(d435_integrity.get('missing_frames') or 0)}, "
            f"out_of_order={int(d435_integrity.get('out_of_order_frames') or 0)})"
        )
    if int(coinft.get("sample_count") or 0) <= 0:
        reasons.append("normalized CoinFT samples are missing")
    if int(coinft.get("calibrated_sample_count") or 0) <= 0:
        reasons.append("calibrated dual CoinFT samples are missing")
    return {"ok": not reasons, "reasons": reasons}


def validate_alignment(report: dict[str, Any], min_valid_ratio: float) -> dict[str, Any]:
    reasons: list[str] = []
    counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
    timeline_rows = int(counts.get("timeline_rows") or 0)
    ratios: dict[str, float] = {}
    if timeline_rows <= 0:
        reasons.append("aligned timeline has no rows")
    else:
        for key, label in (
            ("iphone_valid", "iPhone"),
            ("d435_valid", "D435"),
            ("coinft_valid", "CoinFT"),
            ("all_valid", "all streams"),
        ):
            ratio = float(counts.get(key) or 0) / float(timeline_rows)
            ratios[key] = ratio
            if ratio < min_valid_ratio:
                reasons.append(f"{label} valid alignment ratio is too low ({ratio:.1%})")
        duplicate_ratio = float(counts.get("iphone_duplicate_targets") or 0) / float(timeline_rows)
        ratios["iphone_duplicate_targets"] = duplicate_ratio
        if duplicate_ratio > 0.05:
            reasons.append(f"iPhone target timeline reuses too many frames ({duplicate_ratio:.1%})")
    return {"ok": not reasons, "reasons": reasons, "ratios": ratios}


def validate_episode_media(
    manifest: dict[str, Any],
    source_episode_index: int,
    max_missing_ratio: float,
) -> dict[str, Any]:
    summary = next(
        (
            row
            for row in manifest.get("episodes", [])
            if isinstance(row, dict) and row.get("source_episode_index") == source_episode_index
        ),
        None,
    )
    if summary is None:
        return {"ok": False, "reasons": ["exported Episode summary is missing"], "ratios": {}}
    total = int(summary.get("rgb0_len") or 0)
    reasons: list[str] = []
    ratios: dict[str, float] = {}
    if total <= 0:
        reasons.append("exported Episode has no RGB rows")
    else:
        for key, label in (
            ("missing_iphone_images", "iPhone RGB"),
            ("missing_global_rgb_frames", "D435 RGB"),
            ("missing_depth_frames", "D435 depth"),
            ("missing_iphone_depth_frames", "iPhone depth"),
        ):
            ratio = float(summary.get(key) or 0) / float(total)
            ratios[key] = ratio
            if ratio > max_missing_ratio:
                reasons.append(f"{label} missing ratio is too high ({ratio:.1%})")
    return {"ok": not reasons, "reasons": reasons, "ratios": ratios}


class PostprocessOrchestrator:
    def __init__(
        self,
        export_controller: ExportController,
        *,
        config: PostprocessConfig | None = None,
        coinft_config: Any = None,
        state_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.export_controller = export_controller
        self.config = config or PostprocessConfig()
        self.coinft_config = coinft_config
        self.state_callback = state_callback
        self._episode_locks_guard = threading.Lock()
        self._episode_locks: dict[tuple[Path, int], threading.Lock] = {}
        self._active_guard = threading.Lock()
        self._active: dict[tuple[Path, int], dict[str, Any]] = {}

    def _publish(self, state: dict[str, Any]) -> None:
        if self.state_callback:
            self.state_callback(state)

    def run(self, run_dir: Path, *, episode_index: int | None = None) -> dict[str, Any]:
        run_dir = run_dir.expanduser().resolve()
        if not isinstance(episode_index, int):
            raise ValueError("Episode-level postprocess requires episode_index")
        key = (run_dir, episode_index)
        with self._episode_locks_guard:
            episode_lock = self._episode_locks.setdefault(key, threading.Lock())
        with episode_lock:
            with self._active_guard:
                self._active[key] = {"run_dir": str(run_dir), "episode_index": episode_index}
            result = self._run_locked(run_dir, episode_index=episode_index)
            with self._active_guard:
                self._active.pop(key, None)
                active = list(self._active.values())
            if active:
                self._publish(
                    {
                        "status": "running",
                        "stage": "parallel_episode_postprocess",
                        "active_episodes": active,
                        "last_completed": result,
                    }
                )
            else:
                self._publish(result)
            return result

    def _run_locked(self, run_dir: Path, *, episode_index: int) -> dict[str, Any]:
        state: dict[str, Any] = {
            "status": "running",
            "run_dir": str(run_dir),
            "episode_index": episode_index,
            "cleanup_intermediates": self.config.cleanup_intermediates,
            "stage": "input_validation",
        }
        self._publish(state)
        input_quality = validate_inputs(run_dir, episode_index)
        state["input_validation"] = input_quality
        if not input_quality["ok"]:
            return self._finish(run_dir, {**state, "status": "blocked", "error": "; ".join(input_quality["errors"] or input_quality["missing"])})
        try:
            state["stage"] = "normalize"
            self._publish(state)
            normalized = normalize_episode(run_dir, episode_index, coinft_config=self.coinft_config)
            state["normalized"] = normalized
            normalization_quality = validate_normalization(normalized)
            state["normalization_quality"] = normalization_quality
            if not normalization_quality["ok"]:
                return self._finish(
                    run_dir,
                    {**state, "status": "blocked", "error": "; ".join(normalization_quality["reasons"])},
                )
            state["stage"] = "align"
            self._publish(state)
            aligned = align_episode(
                run_dir,
                episode_index,
                timeline_mode="iphone-frame",
                target_fps=self.config.target_fps,
                iphone_max_delta_ms=self.config.iphone_max_delta_ms,
                d435_max_delta_ms=self.config.d435_max_delta_ms,
                coinft_max_delta_ms=self.config.coinft_max_delta_ms,
            )
            state["aligned"] = aligned
            alignment_quality = validate_alignment(aligned, self.config.alignment_min_valid_ratio)
            state["alignment_quality"] = alignment_quality
            if not alignment_quality["ok"]:
                return self._finish(
                    run_dir,
                    {**state, "status": "blocked", "error": "; ".join(alignment_quality["reasons"])},
                )
            state["stage"] = "export"
            self._publish(state)
            exported = self.export_controller.export_episode(run_dir, episode_index)
            state["export"] = exported
            state["stage"] = "output_validation"
            output_quality = validate_export(run_dir, exported, episode_index)
            state["output_validation"] = output_quality
            if not output_quality["ok"]:
                return self._finish(run_dir, {**state, "status": "blocked", "error": "; ".join(output_quality["missing"])})
            media_quality = validate_episode_media(
                exported,
                episode_index,
                self.config.max_missing_media_ratio,
            )
            state["media_quality"] = media_quality
            if not media_quality["ok"]:
                return self._finish(
                    run_dir,
                    {**state, "status": "blocked", "error": "; ".join(media_quality["reasons"])},
                )
            if self.config.cleanup_intermediates:
                state["removed_intermediates"] = cleanup_intermediates(run_dir, episode_index)
            return self._finish(run_dir, {**state, "status": "ok", "stage": "complete"})
        except SystemExit as exc:
            return self._finish(run_dir, {**state, "status": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - persisted as pipeline status for the GUI.
            return self._finish(run_dir, {**state, "status": "error", "error": str(exc)})

    def _finish(self, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
        update_manifest(run_dir, {"postprocess": state})
        append_sync_log(run_dir, {"event": "postprocess_finished", **state})
        return state
