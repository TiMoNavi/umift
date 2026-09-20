"""Validation and discovery helpers for explicitly selected CoinFT models."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


SIDES = ("left", "right")


@dataclass(frozen=True)
class CoinFTSideFiles:
    side: str
    source_dir: Path
    model_path: Path
    norm_path: Path
    external_data_paths: tuple[Path, ...]
    hardware_label: str


def model_set_path_from_input(path: str | Path | None) -> Path:
    if path is None or not str(path).strip():
        raise ValueError("CoinFT model_set path must be selected explicitly")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if candidate.is_dir():
        return (candidate / "model_set.json").resolve()
    return candidate.resolve()


def load_model_set_json(path: str | Path) -> tuple[Path, dict[str, Any]]:
    model_set_path = model_set_path_from_input(path)
    with model_set_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"CoinFT model_set must be a JSON object: {model_set_path}")
    validate_model_set_payload(payload, model_set_path.parent)
    return model_set_path, payload


def validate_model_set_payload(payload: dict[str, Any], base_dir: Path) -> None:
    slots = payload.get("slots")
    if not isinstance(slots, dict):
        raise ValueError("CoinFT model_set must contain slots")
    for side in SIDES:
        item = slots.get(side)
        if not isinstance(item, dict):
            raise ValueError(f"CoinFT model_set missing slot: {side}")
        model_path = resolve_model_path(base_dir, item.get("model_path"), f"{side}.model_path")
        norm_path = resolve_model_path(base_dir, item.get("norm_path"), f"{side}.norm_path")
        if not model_path.is_file():
            raise FileNotFoundError(f"Missing CoinFT {side} ONNX model: {model_path}")
        if not norm_path.is_file():
            raise FileNotFoundError(f"Missing CoinFT {side} norm JSON: {norm_path}")
        validate_norm_json(norm_path)


def resolve_model_path(base_dir: Path, value: Any, label: str) -> Path:
    if not value:
        raise ValueError(f"CoinFT model_set missing {label}")
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def validate_norm_json(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    expected_lengths = {"mu_x": 12, "sd_x": 12, "mu_y": 6, "sd_y": 6}
    for key, expected in expected_lengths.items():
        value = raw.get(key)
        if not isinstance(value, list) or len(value) != expected:
            raise ValueError(f"CoinFT norm {path} field {key} must be length {expected}")


def discover_side_files(side: str, source_dir: str | Path) -> CoinFTSideFiles:
    source = Path(source_dir).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"CoinFT {side} model folder does not exist: {source}")
    onnx_files = sorted(path for path in source.glob("*.onnx") if path.is_file())
    norm_files = sorted(path for path in source.glob("*_norm.json") if path.is_file())
    external_files = tuple(sorted(path for path in source.glob("*.onnx.data") if path.is_file()))
    if len(onnx_files) != 1:
        raise ValueError(f"CoinFT {side} model folder must contain exactly one .onnx file: {source}")
    if len(norm_files) != 1:
        raise ValueError(f"CoinFT {side} model folder must contain exactly one *_norm.json file: {source}")
    if not external_files:
        raise ValueError(f"CoinFT {side} model folder must contain the ONNX external data file (*.onnx.data): {source}")
    validate_norm_json(norm_files[0])
    return CoinFTSideFiles(
        side=side,
        source_dir=source,
        model_path=onnx_files[0],
        norm_path=norm_files[0],
        external_data_paths=external_files,
        hardware_label=infer_hardware_label(onnx_files[0]),
    )


def discover_side_files_from_paths(
    side: str,
    *,
    model_path: str | Path,
    norm_path: str | Path,
    hardware_label: str | None = None,
) -> CoinFTSideFiles:
    model = Path(model_path).expanduser().resolve()
    norm = Path(norm_path).expanduser().resolve()
    if not model.is_file() or model.suffix.lower() != ".onnx":
        raise FileNotFoundError(f"CoinFT {side} ONNX model does not exist: {model}")
    if not norm.is_file() or norm.suffix.lower() != ".json":
        raise FileNotFoundError(f"CoinFT {side} norm JSON does not exist: {norm}")
    validate_norm_json(norm)
    external_files = tuple(sorted(path for path in model.parent.glob("*.onnx.data") if path.is_file()))
    return CoinFTSideFiles(
        side=side,
        source_dir=model.parent,
        model_path=model,
        norm_path=norm,
        external_data_paths=external_files,
        hardware_label=(hardware_label or "").strip() or infer_hardware_label(model),
    )


def infer_hardware_label(model_path: Path) -> str:
    stem = model_path.stem
    for suffix in ("_MLP", "-MLP"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem
