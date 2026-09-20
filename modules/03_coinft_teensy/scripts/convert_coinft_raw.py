#!/usr/bin/env python3
"""Convert raw CoinFT capture CSVs into split LF/RF CSVs.

This script does not touch the serial device. It consumes the faithful raw CSV
written by collect_coinft_raw.py and produces derived files. ONNX calibration is
optional but required if the output will feed ForceFlow force training.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


COINFT_CHANNELS = 12
FORCE_COLUMNS = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d_%H%M%S")


def iso_from_unix_ns(unix_ns: int) -> str:
    seconds = unix_ns / 1_000_000_000.0
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"No rows found in {path}")
    return rows


def side_raw_array(rows: list[dict[str, str]], side: str) -> np.ndarray:
    prefix = "left" if side == "LF" else "right"
    columns = [f"{prefix}_c{i}" for i in range(1, COINFT_CHANNELS + 1)]
    missing = [column for column in columns if column not in rows[0]]
    if missing:
        raise SystemExit(f"Missing raw columns in input CSV: {missing}")
    return np.array([[float(row[column]) for column in columns] for row in rows], dtype=np.float64)


def compute_offset(raw: np.ndarray, *, initial_samples: int, ignored_samples: int, no_tare_offset: bool) -> np.ndarray:
    if no_tare_offset:
        return np.zeros((COINFT_CHANNELS,), dtype=np.float64)
    if raw.shape[0] <= ignored_samples:
        raise SystemExit(f"Need more than ignored_samples={ignored_samples} rows to compute offset.")
    sample_count = min(raw.shape[0], max(initial_samples, ignored_samples + 1))
    return np.mean(raw[ignored_samples:sample_count], axis=0)


def load_norm(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        raise SystemExit(f"Norm file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "mu_x": np.array(data["mu_x"], dtype=np.float32),
        "sd_x": np.array(data["sd_x"], dtype=np.float32),
        "mu_y": np.array(data["mu_y"], dtype=np.float32),
        "sd_y": np.array(data["sd_y"], dtype=np.float32),
    }


def load_calibrators(args: argparse.Namespace) -> dict[str, Any] | None:
    paths = [args.left_model, args.right_model, args.left_norm, args.right_norm]
    if args.raw_only:
        return None
    if not all(paths):
        raise SystemExit("Calibration output requires --left-model --right-model --left-norm --right-norm, or pass --raw-only.")

    try:
        import onnxruntime as ort
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"onnxruntime is required for calibrated output: {exc}") from exc

    return {
        "LF": {
            "session": ort.InferenceSession(str(Path(args.left_model).expanduser().resolve())),
            "norm": load_norm(Path(args.left_norm).expanduser().resolve()),
        },
        "RF": {
            "session": ort.InferenceSession(str(Path(args.right_model).expanduser().resolve())),
            "norm": load_norm(Path(args.right_norm).expanduser().resolve()),
        },
    }


def calibrated_force(raw_offset: np.ndarray, calibrator: dict[str, Any]) -> np.ndarray:
    norm = calibrator["norm"]
    session = calibrator["session"]
    x_n = (raw_offset.astype(np.float32) - norm["mu_x"]) / norm["sd_x"]
    x_n = x_n.reshape(1, COINFT_CHANNELS)
    input_name = session.get_inputs()[0].name
    y_n = session.run(None, {input_name: x_n})[0].flatten()
    return (y_n * norm["sd_y"] + norm["mu_y"]).astype(np.float64)


def output_fieldnames(raw_only: bool) -> list[str]:
    base = [
        "Timestamp",
        "host_receive_monotonic_ns",
        "host_receive_unix_ns",
        "sequence_id",
        "teensy_time_us",
    ]
    if not raw_only:
        base.extend(FORCE_COLUMNS)
    base.extend([f"C{i}" for i in range(1, COINFT_CHANNELS + 1)])
    return base


def convert_side(
    *,
    rows: list[dict[str, str]],
    side: str,
    raw: np.ndarray,
    offset: np.ndarray,
    output_path: Path,
    calibrator: dict[str, Any] | None,
    window: int,
    raw_channel_mode: str,
) -> None:
    moving_average: deque[np.ndarray] = deque(maxlen=max(1, window))
    raw_only = calibrator is None

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fieldnames(raw_only))
        writer.writeheader()
        for index, source_row in enumerate(rows):
            raw_offset = raw[index] - offset
            channels = raw[index] if raw_channel_mode == "raw" else raw_offset
            host_unix_ns = int(source_row["host_receive_unix_ns"])
            out: dict[str, Any] = {
                "Timestamp": iso_from_unix_ns(host_unix_ns),
                "host_receive_monotonic_ns": source_row["host_receive_monotonic_ns"],
                "host_receive_unix_ns": source_row["host_receive_unix_ns"],
                "sequence_id": source_row["sequence_id"],
                "teensy_time_us": source_row["teensy_time_us"],
            }

            if calibrator is not None:
                force = calibrated_force(raw_offset, calibrator)
                moving_average.append(force)
                force_avg = np.mean(np.stack(list(moving_average), axis=0), axis=0)
                for column, value in zip(FORCE_COLUMNS, force_avg):
                    out[column] = f"{float(value):.9f}"

            for channel_index, value in enumerate(channels, start=1):
                out[f"C{channel_index}"] = f"{float(value):.9f}"
            writer.writerow(out)


def convert(args: argparse.Namespace) -> int:
    input_csv = Path(args.input_csv).expanduser().resolve()
    rows = load_rows(input_csv)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_csv.parent / "converted_coinft"
    output_dir.mkdir(parents=True, exist_ok=True)

    calibrators = load_calibrators(args)
    raw_arrays = {
        "LF": side_raw_array(rows, "LF"),
        "RF": side_raw_array(rows, "RF"),
    }
    offsets = {
        side: compute_offset(
            raw,
            initial_samples=args.initial_samples,
            ignored_samples=args.ignored_samples,
            no_tare_offset=args.no_tare_offset,
        )
        for side, raw in raw_arrays.items()
    }

    stamp = timestamp_id()
    session = args.session_label
    outputs: dict[str, str] = {}
    for side in ("LF", "RF"):
        output_path = output_dir / f"UMIFT_data_{stamp}_{session}_{side}.csv"
        convert_side(
            rows=rows,
            side=side,
            raw=raw_arrays[side],
            offset=offsets[side],
            output_path=output_path,
            calibrator=None if calibrators is None else calibrators[side],
            window=args.window,
            raw_channel_mode=args.raw_channel_mode,
        )
        outputs[side] = str(output_path)

    log = {
        "schema_version": 1,
        "row_type": "coinft_conversion_log",
        "created_at": utc_now_iso(),
        "input_csv": str(input_csv),
        "output_dir": str(output_dir),
        "raw_only": calibrators is None,
        "raw_channel_mode": args.raw_channel_mode,
        "window": args.window,
        "initial_samples": args.initial_samples,
        "ignored_samples": args.ignored_samples,
        "no_tare_offset": args.no_tare_offset,
        "row_count": len(rows),
        "offsets": {
            side: [float(value) for value in offsets[side]]
            for side in ("LF", "RF")
        },
        "outputs": outputs,
    }
    log_path = output_dir / "conversion_log.json"
    write_json(log_path, log)

    print(f"Wrote {outputs['LF']}")
    print(f"Wrote {outputs['RF']}")
    print(f"Wrote {log_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert collect_coinft_raw.py CSV output into split LF/RF CoinFT CSVs.")
    parser.add_argument("input_csv", help="Path to raw_coinft_stream.csv.")
    parser.add_argument("--output-dir", help="Output directory. Defaults to <input_csv_dir>/converted_coinft.")
    parser.add_argument("--session-label", default="coinft")
    parser.add_argument("--raw-only", action="store_true", help="Only split raw channels; do not emit Fx/Fy/Fz/Mx/My/Mz.")
    parser.add_argument("--left-model")
    parser.add_argument("--right-model")
    parser.add_argument("--left-norm")
    parser.add_argument("--right-norm")
    parser.add_argument("--initial-samples", type=int, default=1000)
    parser.add_argument("--ignored-samples", type=int, default=10)
    parser.add_argument("--no-tare-offset", action="store_true", help="Do not subtract an initial raw offset.")
    parser.add_argument("--window", type=int, default=1, help="Moving-average window for calibrated wrench.")
    parser.add_argument("--raw-channel-mode", choices=("offset", "raw"), default="offset", help="Whether C1..C12 are offset-subtracted or raw ADC values.")
    args = parser.parse_args()
    return convert(args)


if __name__ == "__main__":
    raise SystemExit(main())
