#!/usr/bin/env python3
"""Thin wrapper around the recovered official UMI-FT UART collector.

This wrapper exists only to make Step 05 easier to run from this repo.
It does not change the official CSV layout or packet parsing behavior.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    official_script = here / "umift_ft_timestampped_UART.py"

    parser = argparse.ArgumentParser(
        description="Run the recovered official CoinFT UART collector without changing its output format."
    )
    parser.add_argument("--port", required=True, help="Teensy serial port, e.g. /dev/cu.usbmodemXXXX")
    parser.add_argument("--duration", type=float, default=10.0, help="Capture duration in seconds")
    parser.add_argument("--result-dir", required=True, help="Directory where official CSV outputs should be written")
    parser.add_argument("--filename", default="UMIFT_data", help="Filename prefix passed through to the official script")
    parser.add_argument("--left-model", required=True, help="Left CoinFT ONNX model")
    parser.add_argument("--right-model", required=True, help="Right CoinFT ONNX model")
    parser.add_argument("--left-norm", required=True, help="Left CoinFT norm.json")
    parser.add_argument("--right-norm", required=True, help="Right CoinFT norm.json")
    parser.add_argument("--window", type=int, default=1, help="Moving-average window passed through")
    parser.add_argument("--debug-wrench", action="store_true")
    parser.add_argument("--live-plot", action="store_true")
    args = parser.parse_args()

    cmd = [
        sys.executable,
        str(official_script),
        "--port",
        args.port,
        "--duration",
        str(args.duration),
        "--result_dir",
        args.result_dir,
        "--filename",
        args.filename,
        "--models",
        args.left_model,
        args.right_model,
        "--norms",
        args.left_norm,
        args.right_norm,
        "--window",
        str(args.window),
    ]
    if args.debug_wrench:
        cmd.append("--debug_wrench")
    if args.live_plot:
        cmd.append("--live_plot")

    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
