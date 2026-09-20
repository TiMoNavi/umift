"""CoinFT calibration config and raw-to-wrench helpers for the main chain."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import time

import numpy as np

from umift_laptop_alignment.capture.receivers.coinft.model_store import (
    CoinFTSideFiles,
    discover_side_files,
    discover_side_files_from_paths,
    infer_hardware_label,
    load_model_set_json,
)


COINFT_CHANNELS = 12
WRENCH_AXES = ("fx", "fy", "fz", "mx", "my", "mz")
SIDES = ("left", "right")


@dataclass(frozen=True)
class CoinFTSlotCalibrationConfig:
    side: str
    sensor_index: int
    slot_id: str
    hardware_label: str
    model_manifest_path: Path | None = None
    model_path: Path | None = None
    norm_path: Path | None = None
    axis_map: tuple[int, int, int, int, int, int] = (0, 1, 2, 3, 4, 5)
    axis_sign: tuple[float, float, float, float, float, float] = (1, 1, 1, 1, 1, 1)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "sensor_index": self.sensor_index,
            "slot_id": self.slot_id,
            "hardware_label": self.hardware_label,
            "model_manifest_path": str(self.model_manifest_path) if self.model_manifest_path else None,
            "model_path": str(self.model_path) if self.model_path else None,
            "norm_path": str(self.norm_path) if self.norm_path else None,
            "axis_map": list(self.axis_map),
            "axis_sign": list(self.axis_sign),
        }


@dataclass(frozen=True)
class CoinFTCalibrationConfig:
    mode: str = "raw"
    config_path: Path | None = None
    model_set_id: str | None = None
    model_set_path: Path | None = None
    tare_samples: int = 300
    ignored_tare_samples: int = 10
    moving_average_window: int = 5
    raw_stability_window_s: float = 2.0
    raw_stability_min_wait_s: float = 5.0
    raw_stability_check_interval_s: float = 0.5
    raw_stability_mean_delta_counts: float = 8.0
    raw_stability_noise_std_counts: float = 20.0
    raw_stability_required_passes: int = 3
    runtime_settling_s: float = 5.0
    final_tare_window_s: float = 2.0
    bias_window_s: float = 1.0
    zero_check_window_s: float = 1.0
    zero_force_limit_n: float = 0.15
    expected_sample_rate_hz: float = 100.0
    slots: dict[str, CoinFTSlotCalibrationConfig] | None = None

    @property
    def calibrated(self) -> bool:
        return self.mode == "calibrated"

    def slot(self, side: str) -> CoinFTSlotCalibrationConfig | None:
        return (self.slots or {}).get(side)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "config_path": str(self.config_path) if self.config_path else None,
            "model_set_id": self.model_set_id,
            "model_set_path": str(self.model_set_path) if self.model_set_path else None,
            "tare_samples": self.tare_samples,
            "ignored_tare_samples": self.ignored_tare_samples,
            "moving_average_window": self.moving_average_window,
            "raw_stability_window_s": self.raw_stability_window_s,
            "raw_stability_min_wait_s": self.raw_stability_min_wait_s,
            "raw_stability_check_interval_s": self.raw_stability_check_interval_s,
            "raw_stability_mean_delta_counts": self.raw_stability_mean_delta_counts,
            "raw_stability_noise_std_counts": self.raw_stability_noise_std_counts,
            "raw_stability_required_passes": self.raw_stability_required_passes,
            "runtime_settling_s": self.runtime_settling_s,
            "final_tare_window_s": self.final_tare_window_s,
            "bias_window_s": self.bias_window_s,
            "zero_check_window_s": self.zero_check_window_s,
            "zero_force_limit_n": self.zero_force_limit_n,
            "expected_sample_rate_hz": self.expected_sample_rate_hz,
            "slots": {side: slot.to_manifest_dict() for side, slot in (self.slots or {}).items()},
        }


def raw_coinft_config() -> CoinFTCalibrationConfig:
    return CoinFTCalibrationConfig(mode="raw", slots={})


def load_coinft_calibration_config(path: str | Path | None) -> CoinFTCalibrationConfig:
    if path is None or not str(path).strip():
        # A model must be selected explicitly through --coinft-config or the
        # GUI.  Bundled presets are examples, not an implicit runtime default.
        return raw_coinft_config()
    else:
        input_path = Path(path).expanduser()
        if not input_path.is_absolute():
            input_path = (Path.cwd() / input_path).resolve()
        else:
            input_path = input_path.resolve()
        if input_path.is_dir() or input_path.name == "model_set.json":
            config_path, raw = load_model_set_json(input_path)
        else:
            config_path = input_path
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)

    is_model_set = raw.get("schema") == "umift.coinft.model_set.v1" or bool(raw.get("model_set_id"))
    mode_default = "calibrated" if is_model_set else "raw"
    mode = str(raw.get("calibration_mode", raw.get("mode", mode_default))).strip().lower()
    if mode not in {"raw", "calibrated"}:
        raise ValueError(f"Unsupported CoinFT calibration mode: {mode}")

    base_dir = config_path.parent
    raw_slots = raw.get("slots", {})
    slots: dict[str, CoinFTSlotCalibrationConfig] = {}
    for side in SIDES:
        item = raw_slots.get(side) if isinstance(raw_slots, dict) else None
        if item is None:
            item = raw.get(side, {}) if isinstance(raw.get(side), dict) else {}
        if not item and mode != "calibrated":
            continue
        if not isinstance(item, dict):
            raise ValueError(f"CoinFT slot config for {side!r} must be an object")
        slots[side] = CoinFTSlotCalibrationConfig(
            side=side,
            sensor_index=int(item.get("sensor_index", 0 if side == "left" else 1)),
            slot_id=str(item.get("slot_id", f"{side}_finger_tip")),
            hardware_label=str(item.get("hardware_label", item.get("sensor_id", f"{side}_coinft"))),
            model_manifest_path=_resolve_optional_path(base_dir, item.get("model_manifest_path")),
            model_path=_resolve_optional_path(base_dir, item.get("model_path")),
            norm_path=_resolve_optional_path(base_dir, item.get("norm_path")),
            axis_map=_six_int_tuple(item.get("axis_map", [0, 1, 2, 3, 4, 5]), "axis_map"),
            axis_sign=_six_float_tuple(item.get("axis_sign", [1, 1, 1, 1, 1, 1]), "axis_sign"),
        )

    if mode == "calibrated":
        missing = [side for side in SIDES if side not in slots]
        if missing:
            raise ValueError(f"Calibrated CoinFT mode requires slot configs for: {', '.join(missing)}")

    return CoinFTCalibrationConfig(
        mode=mode,
        config_path=config_path,
        model_set_id=str(raw.get("model_set_id")) if raw.get("model_set_id") else None,
        model_set_path=config_path if is_model_set else None,
        tare_samples=int(raw.get("tare_samples", 300)),
        ignored_tare_samples=int(raw.get("ignored_tare_samples", 10)),
        moving_average_window=int(raw.get("moving_average_window", 5)),
        raw_stability_window_s=float(raw.get("raw_stability_window_s", 2.0)),
        raw_stability_min_wait_s=float(raw.get("raw_stability_min_wait_s", 5.0)),
        raw_stability_check_interval_s=float(raw.get("raw_stability_check_interval_s", 0.5)),
        raw_stability_mean_delta_counts=float(raw.get("raw_stability_mean_delta_counts", 8.0)),
        raw_stability_noise_std_counts=float(raw.get("raw_stability_noise_std_counts", 20.0)),
        raw_stability_required_passes=int(raw.get("raw_stability_required_passes", 3)),
        runtime_settling_s=float(raw.get("runtime_settling_s", 5.0)),
        final_tare_window_s=float(raw.get("final_tare_window_s", 2.0)),
        bias_window_s=float(raw.get("bias_window_s", 1.0)),
        zero_check_window_s=float(raw.get("zero_check_window_s", 1.0)),
        zero_force_limit_n=float(raw.get("zero_force_limit_n", 0.15)),
        expected_sample_rate_hz=float(raw.get("expected_sample_rate_hz", 100.0)),
        slots=slots,
    )


def coinft_calibration_config_from_files(
    *,
    left_model_path: str | Path,
    left_norm_path: str | Path,
    right_model_path: str | Path,
    right_norm_path: str | Path,
    left_hardware_label: str | None = None,
    right_hardware_label: str | None = None,
    model_set_id: str | None = None,
) -> CoinFTCalibrationConfig:
    """Build a validated runtime config without copying models into source."""

    left = discover_side_files_from_paths(
        "left",
        model_path=left_model_path,
        norm_path=left_norm_path,
        hardware_label=left_hardware_label,
    )
    right = discover_side_files_from_paths(
        "right",
        model_path=right_model_path,
        norm_path=right_norm_path,
        hardware_label=right_hardware_label,
    )
    return _coinft_calibration_config_from_side_files(left, right, model_set_id=model_set_id)


def coinft_calibration_config_from_side_dirs(
    *,
    left_dir: str | Path,
    right_dir: str | Path,
    model_set_id: str | None = None,
) -> CoinFTCalibrationConfig:
    """Build a validated runtime config from separate left/right folders."""

    left = discover_side_files("left", left_dir)
    right = discover_side_files("right", right_dir)
    return _coinft_calibration_config_from_side_files(left, right, model_set_id=model_set_id)


def _coinft_calibration_config_from_side_files(
    left: CoinFTSideFiles,
    right: CoinFTSideFiles,
    *,
    model_set_id: str | None,
) -> CoinFTCalibrationConfig:
    resolved_id = (model_set_id or f"{left.hardware_label}__{right.hardware_label}").strip()
    slots = {
        side_files.side: CoinFTSlotCalibrationConfig(
            side=side_files.side,
            sensor_index=0 if side_files.side == "left" else 1,
            slot_id=f"{side_files.side}_finger_tip",
            hardware_label=side_files.hardware_label or infer_hardware_label(side_files.model_path),
            model_path=side_files.model_path,
            norm_path=side_files.norm_path,
        )
        for side_files in (left, right)
    }
    return CoinFTCalibrationConfig(
        mode="calibrated",
        model_set_id=resolved_id,
        slots=slots,
    )


class CoinFTCalibrator:
    """Runtime raw-to-wrench conversion for one physical CoinFT."""

    def __init__(self, slot: CoinFTSlotCalibrationConfig, *, moving_average_window: int):
        self.slot = slot
        self.axis_map = np.array(slot.axis_map, dtype=np.int64)
        self.axis_sign = np.array(slot.axis_sign, dtype=np.float32)
        self.offset: np.ndarray | None = None
        self.ft_bias: np.ndarray | None = None
        self.filter = deque(maxlen=max(1, int(moving_average_window)))
        self.session = None
        self.input_name = None
        self.norm: dict[str, np.ndarray] | None = None
        self.model_path = slot.model_path
        self.norm_path = slot.norm_path
        if slot.model_manifest_path:
            self.model_path, self.norm_path = self._load_manifest(slot.model_manifest_path, slot.hardware_label)
        if not self.model_path or not self.norm_path:
            raise ValueError(f"Calibrated CoinFT slot {slot.side} requires model/norm paths or a manifest")
        self._load_model(self.model_path, self.norm_path)

    def _load_manifest(self, manifest_path: Path, expected_hardware_label: str) -> tuple[Path, Path]:
        if not manifest_path.exists():
            raise FileNotFoundError(f"Missing CoinFT model manifest: {manifest_path}")
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest_label = manifest.get("hardware_label", manifest.get("sensor_id"))
        if manifest_label != expected_hardware_label:
            raise ValueError(
                f"CoinFT manifest hardware_label mismatch for {self.slot.side}: "
                f"slot expects {expected_hardware_label}, manifest declares {manifest_label}"
            )
        base_dir = manifest_path.parent
        model_path = _resolve_required_path(base_dir, manifest.get("model_path"), "model_path")
        norm_path = _resolve_required_path(base_dir, manifest.get("norm_path"), "norm_path")
        if self.model_path and self.model_path.resolve() != model_path.resolve():
            raise ValueError(f"CoinFT model_path disagrees with manifest for {expected_hardware_label}")
        if self.norm_path and self.norm_path.resolve() != norm_path.resolve():
            raise ValueError(f"CoinFT norm_path disagrees with manifest for {expected_hardware_label}")
        expected_model_sha256 = manifest.get("model_sha256")
        if expected_model_sha256:
            verify_sha256(model_path, str(expected_model_sha256), "CoinFT model")
        expected_norm_sha256 = manifest.get("norm_sha256")
        if expected_norm_sha256:
            verify_sha256(norm_path, str(expected_norm_sha256), "CoinFT norm")
        return model_path, norm_path

    def _load_model(self, model_path: Path, norm_path: Path) -> None:
        if not model_path.exists():
            raise FileNotFoundError(f"Missing CoinFT ONNX model: {model_path}")
        if not norm_path.exists():
            raise FileNotFoundError(f"Missing CoinFT norm JSON: {norm_path}")
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("onnxruntime is required for calibrated CoinFT mode") from exc
        with norm_path.open("r", encoding="utf-8") as handle:
            raw_norm = json.load(handle)
        self.norm = {
            "mu_x": np.array(raw_norm["mu_x"], dtype=np.float32),
            "sd_x": np.array(raw_norm["sd_x"], dtype=np.float32),
            "mu_y": np.array(raw_norm["mu_y"], dtype=np.float32),
            "sd_y": np.array(raw_norm["sd_y"], dtype=np.float32),
        }
        self.session = ort.InferenceSession(str(model_path))
        self.input_name = self.session.get_inputs()[0].name

    def set_offset(self, offset: np.ndarray) -> None:
        self.offset = np.asarray(offset, dtype=np.float64)
        self.filter.clear()

    def set_ft_bias(self, ft_bias: np.ndarray) -> None:
        self.ft_bias = np.asarray(ft_bias, dtype=np.float64)
        self.filter.clear()

    def reset_filter(self) -> None:
        self.filter.clear()

    def zero_raw(self, raw: np.ndarray) -> np.ndarray:
        raw = np.asarray(raw, dtype=np.float64)
        if self.offset is None:
            return raw
        return raw - self.offset

    def predict_uncorrected(self, raw: np.ndarray) -> np.ndarray:
        if self.session is None or self.norm is None or self.input_name is None:
            raise RuntimeError("CoinFT calibrator is missing an ONNX session")
        zeroed = self.zero_raw(raw).astype(np.float32)
        x_norm = (zeroed - self.norm["mu_x"]) / self.norm["sd_x"]
        pred_norm = self.session.run(None, {self.input_name: x_norm.reshape(1, -1)})[0].flatten()
        wrench = pred_norm * self.norm["sd_y"] + self.norm["mu_y"]
        return (wrench[self.axis_map] * self.axis_sign).astype(np.float64)

    def predict(self, raw: np.ndarray) -> np.ndarray:
        wrench = self.predict_uncorrected(raw)
        if self.ft_bias is not None:
            wrench = wrench - self.ft_bias
        self.filter.append(wrench)
        return np.mean(np.array(self.filter), axis=0)


class CoinFTCalibrationRuntime:
    """Two-side startup calibration with raw stability, two tares, and fixed output bias."""

    def __init__(self, config: CoinFTCalibrationConfig):
        self.config = config
        self.status = "raw_only" if not config.calibrated else "warming_up"
        self.last_error: str | None = None
        self.calibrators: dict[str, CoinFTCalibrator] = {}
        self.raw_history: deque[tuple[float, dict[str, np.ndarray]]] = deque()
        self.bias_history: deque[tuple[float, dict[str, np.ndarray]]] = deque()
        self.zero_check_history: deque[tuple[float, dict[str, np.ndarray]]] = deque()
        self.offline_tare_buffers: dict[str, list[np.ndarray]] = {side: [] for side in SIDES}
        self.started_s: float | None = None
        self.runtime_settling_started_s: float | None = None
        self.bias_started_s: float | None = None
        self.zero_check_started_s: float | None = None
        self.last_stability_check_s: float | None = None
        self.stability_passes = 0
        self.stability_mean_delta_counts: float | None = None
        self.stability_noise_std_counts: float | None = None
        self.zero_force_max_abs_n: float | None = None
        self.provisional_offsets: dict[str, np.ndarray] = {}
        self.final_offsets: dict[str, np.ndarray] = {}
        self.ft_biases: dict[str, np.ndarray] = {}
        self._virtual_timestamp_s: float | None = None
        if config.calibrated:
            for side in SIDES:
                slot = config.slot(side)
                if slot is None:
                    raise ValueError(f"Missing CoinFT calibration slot for {side}")
                self.calibrators[side] = CoinFTCalibrator(slot, moving_average_window=config.moving_average_window)

    @property
    def calibrated(self) -> bool:
        return self.config.calibrated

    @property
    def ready(self) -> bool:
        return not self.config.calibrated or self.status == "ready"

    def process(
        self,
        left_raw: tuple[int, ...] | list[int] | np.ndarray,
        right_raw: tuple[int, ...] | list[int] | np.ndarray,
        *,
        timestamp_s: float | None = None,
    ) -> dict[str, Any]:
        if not self.config.calibrated:
            return {"calibration_status": "raw_only", "calibration_ready": True}

        raws = {
            "left": np.asarray(left_raw, dtype=np.float64),
            "right": np.asarray(right_raw, dtype=np.float64),
        }
        timestamp_s = self._timestamp(timestamp_s)
        if self.started_s is None:
            self.started_s = timestamp_s
        try:
            self._append_raw(timestamp_s, raws)
            if self.status in {"warming_up", "delayed"}:
                self._advance_raw_stability(timestamp_s)
                if self.status in {"warming_up", "delayed"}:
                    return self._status_result()

            if self.status == "provisional_tare":
                self.status = "runtime_settling"
                self.runtime_settling_started_s = timestamp_s
                return self._status_result()

            if self.status == "runtime_settling":
                self._advance_final_tare(timestamp_s)
                return self._status_result()

            if self.status == "final_tare":
                self.status = "bias_calibration"
                self.bias_started_s = timestamp_s
                self.bias_history.clear()

            if self.status == "bias_calibration":
                self._advance_bias_calibration(timestamp_s, raws)
                return self._status_result()

            output = self._wrench_result(raws)
            if self.status == "zero_check":
                self._advance_zero_check(timestamp_s, output)
                output.update(self._status_result())
                return output
            if self.status == "zero_check_failed":
                return self._status_result()

            output.update(self._status_result())
            self.last_error = None
            return output
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(exc)
            return {"calibration_status": "error", "calibration_ready": False, "calibration_error": str(exc)}

    def process_offline(
        self,
        left_raw: tuple[int, ...] | list[int] | np.ndarray,
        right_raw: tuple[int, ...] | list[int] | np.ndarray,
    ) -> dict[str, Any]:
        """Preserve the historical one-tare path for reprocessing legacy raw CSV files."""
        if not self.config.calibrated:
            return {"calibration_status": "raw_only", "calibration_ready": True}
        raws = {
            "left": np.asarray(left_raw, dtype=np.float64),
            "right": np.asarray(right_raw, dtype=np.float64),
        }
        try:
            if self.status not in {"offline_tare_pending", "offline_ready"}:
                self.status = "offline_tare_pending"
            if self.status == "offline_tare_pending":
                for side, raw in raws.items():
                    self.offline_tare_buffers[side].append(raw)
                count = min(len(values) for values in self.offline_tare_buffers.values())
                if count < max(1, self.config.tare_samples):
                    return {
                        "calibration_status": "tare_pending",
                        "calibration_ready": False,
                        "tare_count": count,
                        "tare_target": self.config.tare_samples,
                    }
                skip = max(0, int(self.config.ignored_tare_samples))
                for side, rows in self.offline_tare_buffers.items():
                    arr = np.asarray(rows[skip:] or rows, dtype=np.float64)
                    self.calibrators[side].set_offset(np.mean(arr, axis=0))
                self.status = "offline_ready"
            output = self._wrench_result(raws)
            output.update(
                {
                    "calibration_status": "ok",
                    "calibration_ready": True,
                    "tare_count": self.config.tare_samples,
                    "tare_target": self.config.tare_samples,
                }
            )
            return output
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(exc)
            return {"calibration_status": "error", "calibration_ready": False, "calibration_error": str(exc)}

    def _timestamp(self, timestamp_s: float | None) -> float:
        if timestamp_s is not None:
            return float(timestamp_s)
        if self._virtual_timestamp_s is None:
            self._virtual_timestamp_s = time.monotonic()
        else:
            rate = max(1.0, float(self.config.expected_sample_rate_hz))
            self._virtual_timestamp_s += 1.0 / rate
        return self._virtual_timestamp_s

    def _append_raw(self, timestamp_s: float, raws: dict[str, np.ndarray]) -> None:
        self.raw_history.append((timestamp_s, {side: raw.copy() for side, raw in raws.items()}))
        keep_s = max(
            self.config.raw_stability_window_s * 2.0,
            self.config.final_tare_window_s,
        ) + 1.0
        while self.raw_history and self.raw_history[0][0] < timestamp_s - keep_s:
            self.raw_history.popleft()

    def _raw_window(self, start_s: float, end_s: float) -> dict[str, np.ndarray] | None:
        rows = [(timestamp, raws) for timestamp, raws in self.raw_history if start_s <= timestamp <= end_s]
        required_span = max(0.0, end_s - start_s) * 0.7
        if len(rows) < 2 or rows[-1][0] - rows[0][0] < required_span:
            return None
        return {
            side: np.stack([raws[side] for _, raws in rows], axis=0)
            for side in SIDES
        }

    def _advance_raw_stability(self, timestamp_s: float) -> None:
        if self.started_s is None:
            return
        elapsed_s = timestamp_s - self.started_s
        if elapsed_s < self.config.raw_stability_min_wait_s:
            self.status = "warming_up"
            return
        if (
            self.last_stability_check_s is not None
            and timestamp_s - self.last_stability_check_s < self.config.raw_stability_check_interval_s
        ):
            return
        self.last_stability_check_s = timestamp_s
        window_s = self.config.raw_stability_window_s
        previous = self._raw_window(timestamp_s - 2.0 * window_s, timestamp_s - window_s)
        current = self._raw_window(timestamp_s - window_s, timestamp_s)
        if previous is None or current is None:
            self.status = "delayed"
            self.stability_passes = 0
            return
        previous_all = np.concatenate([previous[side] for side in SIDES], axis=1)
        current_all = np.concatenate([current[side] for side in SIDES], axis=1)
        mean_delta = float(np.max(np.abs(np.mean(current_all, axis=0) - np.mean(previous_all, axis=0))))
        noise_std = float(np.median(np.std(current_all, axis=0)))
        self.stability_mean_delta_counts = mean_delta
        self.stability_noise_std_counts = noise_std
        stable = (
            mean_delta <= self.config.raw_stability_mean_delta_counts
            and noise_std <= self.config.raw_stability_noise_std_counts
        )
        self.stability_passes = self.stability_passes + 1 if stable else 0
        if self.stability_passes < max(1, self.config.raw_stability_required_passes):
            self.status = "delayed"
            return
        for side in SIDES:
            offset = np.mean(current[side], axis=0)
            self.provisional_offsets[side] = offset
            self.calibrators[side].set_offset(offset)
        self.status = "provisional_tare"

    def _advance_final_tare(self, timestamp_s: float) -> None:
        if self.runtime_settling_started_s is None:
            self.runtime_settling_started_s = timestamp_s
        if timestamp_s - self.runtime_settling_started_s < self.config.runtime_settling_s:
            return
        window = self._raw_window(timestamp_s - self.config.final_tare_window_s, timestamp_s)
        if window is None:
            return
        for side in SIDES:
            provisional = self.provisional_offsets[side]
            residual = np.mean(window[side] - provisional, axis=0)
            final_offset = provisional + residual
            self.final_offsets[side] = final_offset
            self.calibrators[side].set_offset(final_offset)
            self.calibrators[side].reset_filter()
        self.raw_history.clear()
        self.status = "final_tare"

    def _advance_bias_calibration(self, timestamp_s: float, raws: dict[str, np.ndarray]) -> None:
        predictions = {
            side: self.calibrators[side].predict_uncorrected(raws[side])
            for side in SIDES
        }
        self.bias_history.append((timestamp_s, predictions))
        if self.bias_started_s is None:
            self.bias_started_s = timestamp_s
        if timestamp_s - self.bias_started_s < self.config.bias_window_s:
            return
        for side in SIDES:
            bias = np.mean(np.stack([values[side] for _, values in self.bias_history], axis=0), axis=0)
            self.ft_biases[side] = bias
            self.calibrators[side].set_ft_bias(bias)
            self.calibrators[side].reset_filter()
        self.bias_history.clear()
        self.zero_check_history.clear()
        self.zero_check_started_s = timestamp_s
        self.status = "zero_check"

    def _advance_zero_check(self, timestamp_s: float, output: dict[str, Any]) -> None:
        predictions = {
            side: np.asarray(output[f"{side}_wrench"], dtype=np.float64)
            for side in SIDES
        }
        self.zero_check_history.append((timestamp_s, predictions))
        if self.zero_check_started_s is None:
            self.zero_check_started_s = timestamp_s
        if timestamp_s - self.zero_check_started_s < self.config.zero_check_window_s:
            return
        force_means = np.stack(
            [
                np.mean(np.stack([values[side][:3] for _, values in self.zero_check_history], axis=0), axis=0)
                for side in SIDES
            ],
            axis=0,
        )
        self.zero_force_max_abs_n = float(np.max(np.abs(force_means)))
        if self.zero_force_max_abs_n <= self.config.zero_force_limit_n:
            self.status = "ready"
            self.last_error = None
        else:
            self.status = "zero_check_failed"
            self.last_error = (
                f"CoinFT unloaded zero check failed: {self.zero_force_max_abs_n:.3f} N exceeds "
                f"{self.config.zero_force_limit_n:.3f} N; unload the gripper and restart CoinFT"
            )

    def _wrench_result(self, raws: dict[str, np.ndarray]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for side, raw in raws.items():
            calibrator = self.calibrators[side]
            output[f"{side}_raw_zeroed"] = calibrator.zero_raw(raw).astype(float).tolist()
            output[f"{side}_wrench"] = calibrator.predict(raw).astype(float).tolist()
        return output

    def _status_result(self) -> dict[str, Any]:
        return {
            "calibration_status": self.status,
            "calibration_ready": self.ready,
            "calibration_error": self.last_error,
            "tare_count": len(self.raw_history),
            "tare_target": self.config.tare_samples,
            "stability_passes": self.stability_passes,
            "stability_required_passes": self.config.raw_stability_required_passes,
            "stability_mean_delta_counts": self.stability_mean_delta_counts,
            "stability_noise_std_counts": self.stability_noise_std_counts,
            "zero_force_max_abs_n": self.zero_force_max_abs_n,
            "zero_force_limit_n": self.config.zero_force_limit_n,
        }

    def runtime_metadata(self) -> dict[str, Any]:
        payload = self.config.to_manifest_dict()
        payload["status"] = self.status
        payload["ready"] = self.ready
        payload["last_error"] = self.last_error
        payload["stability"] = {
            "passes": self.stability_passes,
            "required_passes": self.config.raw_stability_required_passes,
            "mean_delta_counts": self.stability_mean_delta_counts,
            "noise_std_counts": self.stability_noise_std_counts,
        }
        payload["zero_check"] = {
            "max_abs_force_n": self.zero_force_max_abs_n,
            "limit_n": self.config.zero_force_limit_n,
        }
        for side, calibrator in self.calibrators.items():
            side_payload = payload.setdefault("slots", {}).setdefault(side, {})
            side_payload["runtime_offset"] = None if calibrator.offset is None else calibrator.offset.astype(float).tolist()
            side_payload["provisional_offset"] = (
                self.provisional_offsets[side].astype(float).tolist()
                if side in self.provisional_offsets
                else None
            )
            side_payload["final_offset"] = (
                self.final_offsets[side].astype(float).tolist()
                if side in self.final_offsets
                else None
            )
            side_payload["ft_bias"] = (
                self.ft_biases[side].astype(float).tolist()
                if side in self.ft_biases
                else None
            )
            side_payload["resolved_model_path"] = str(calibrator.model_path) if calibrator.model_path else None
            side_payload["resolved_norm_path"] = str(calibrator.norm_path) if calibrator.norm_path else None
        return payload


def calibrated_csv_fieldnames() -> list[str]:
    fields = [
        "calibration_status",
        "calibration_ready",
        "calibration_error",
        "tare_count",
        "tare_target",
        "stability_passes",
        "stability_required_passes",
        "stability_mean_delta_counts",
        "stability_noise_std_counts",
        "zero_force_max_abs_n",
        "zero_force_limit_n",
    ]
    for side in SIDES:
        fields.extend([f"{side}_zeroed_c{i}" for i in range(1, COINFT_CHANNELS + 1)])
        fields.extend([f"{side}_{axis}" for axis in WRENCH_AXES])
    return fields


def calibration_result_csv_fields(result: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "calibration_status": result.get("calibration_status"),
        "calibration_ready": result.get("calibration_ready"),
        "calibration_error": result.get("calibration_error"),
        "tare_count": result.get("tare_count"),
        "tare_target": result.get("tare_target"),
        "stability_passes": result.get("stability_passes"),
        "stability_required_passes": result.get("stability_required_passes"),
        "stability_mean_delta_counts": result.get("stability_mean_delta_counts"),
        "stability_noise_std_counts": result.get("stability_noise_std_counts"),
        "zero_force_max_abs_n": result.get("zero_force_max_abs_n"),
        "zero_force_limit_n": result.get("zero_force_limit_n"),
    }
    for side in SIDES:
        zeroed = result.get(f"{side}_raw_zeroed")
        if isinstance(zeroed, list):
            for index, value in enumerate(zeroed[:COINFT_CHANNELS], start=1):
                row[f"{side}_zeroed_c{index}"] = value
        wrench = result.get(f"{side}_wrench")
        if isinstance(wrench, list):
            for axis, value in zip(WRENCH_AXES, wrench):
                row[f"{side}_{axis}"] = value
    return row


def calibration_status_summary(config: CoinFTCalibrationConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "config_path": str(config.config_path) if config.config_path else None,
        "model_set_id": config.model_set_id,
        "model_set_path": str(config.model_set_path) if config.model_set_path else None,
        "left_hardware_label": config.slot("left").hardware_label if config.slot("left") else None,
        "right_hardware_label": config.slot("right").hardware_label if config.slot("right") else None,
    }


def verify_sha256(path: Path, expected: str, label: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise ValueError(f"{label} sha256 mismatch for {path}: expected {expected}, got {actual}")


def _resolve_optional_path(base_dir: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _resolve_required_path(base_dir: Path, value: Any, label: str) -> Path:
    path = _resolve_optional_path(base_dir, value)
    if path is None:
        raise ValueError(f"CoinFT model manifest must contain {label}")
    return path


def _six_int_tuple(value: Any, label: str) -> tuple[int, int, int, int, int, int]:
    if not isinstance(value, list) or len(value) != 6:
        raise ValueError(f"{label} must be a list of 6 integers")
    return tuple(int(item) for item in value)  # type: ignore[return-value]


def _six_float_tuple(value: Any, label: str) -> tuple[float, float, float, float, float, float]:
    if not isinstance(value, list) or len(value) != 6:
        raise ValueError(f"{label} must be a list of 6 numbers")
    return tuple(float(item) for item in value)  # type: ignore[return-value]
