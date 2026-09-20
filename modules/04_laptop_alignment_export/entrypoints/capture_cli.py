#!/usr/bin/env python3
# capture_cli.py — command-line monitor and controller for UMIFT capture sessions.
#
# Commands:
#   run   Arm all devices, wait for ready, then monitor continuously.
#         Recording starts / stops automatically when the iPhone sends events.
#         Press Ctrl-C (or run 'stop' in another terminal) to disarm and exit.
#
#   stop  Stop the current capture session (disarm all devices).
#
# Usage:
#   python3 entrypoints/capture_cli.py run  [--port 8765] [--auto-process]
#   python3 entrypoints/capture_cli.py stop [--port 8765]
#
# With --auto-process, after each episode stops the script automatically runs:
#   normalize_run.py -> align_run.py -> export_run.py
#
# Status line printed once per second during 'run':
#   IDLE  iPhone 30 fps / 14 Mbps  CoinFT L Fx+0.12 Fy-0.03 Fz+1.20N  R ...  gate:ready
#   REC   iPhone 30 fps / 14 Mbps  CoinFT L Fx+0.12 Fy-0.03 Fz+1.20N  R ...  gate:ready
#
# Exit codes: 0 clean, 1 error / gate timeout, 2 Ctrl-C (stop sent automatically)

from __future__ import annotations

import argparse
import json
import subprocess
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ARM_TIMEOUT_S = 60
POLL_INTERVAL_S = 1.0

_ENTRYPOINTS_DIR = Path(__file__).parent


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(host: str, port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _post(host: str, port: int, path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _cmd(host: str, port: int, command: str, args: dict | None = None) -> dict:
    return _post(host, port, "/main/command", {"command": command, "args": args or {}})


def _state(host: str, port: int) -> dict:
    return _get(host, port, "/state").get("state") or {}


# ── Status line ───────────────────────────────────────────────────────────────

def _fmt_f(v: object) -> str:
    return f"{v:+.2f}" if isinstance(v, (int, float)) else "--"


def _wrench_side(side: object) -> str:
    if isinstance(side, list) and len(side) >= 3:
        return f"Fx{_fmt_f(side[0])} Fy{_fmt_f(side[1])} Fz{_fmt_f(side[2])}N"
    return "Fx-- Fy-- Fz--N"


def _status_line(state: dict) -> str:
    now = datetime.now().strftime("%H:%M:%S")

    ctrl = state.get("recording_control") or {}
    recording = ctrl.get("active", False)
    armed = ctrl.get("armed", False)
    tag = "REC " if recording else ("ARM " if armed else "IDLE")

    iv = state.get("iphone_video") or {}
    fps = iv.get("fps")
    mbps = iv.get("mbps")
    iphone = (
        f"iPhone {fps:.0f} fps / {mbps:.1f} Mbps"
        if isinstance(fps, (int, float)) and isinstance(mbps, (int, float))
        else "iPhone --"
    )

    coinft = state.get("coinft") or state.get("teensy") or {}
    wrench = coinft.get("wrench")
    if isinstance(wrench, list) and len(wrench) >= 2:
        cft = f"CoinFT L {_wrench_side(wrench[0])}  R {_wrench_side(wrench[1])}"
    else:
        lm, rm = coinft.get("left_mean"), coinft.get("right_mean")
        cft = (
            f"CoinFT raw L={lm:.0f} R={rm:.0f}"
            if isinstance(lm, (int, float)) and isinstance(rm, (int, float))
            else "CoinFT --"
        )

    summary = state.get("capture_summary") or {}
    gate = "gate:ready" if summary.get("ready") else "gate:blocked"

    streams = (state.get("recording_gate") or {}).get("streams") or {}
    frames = (streams.get("iphone") or {}).get("frame_count") or iv.get("last_sequence") or 0

    ep = ctrl.get("episode_index")
    ep_tag = f"  ep={ep}" if isinstance(ep, int) else ""

    return f"[{now}] {tag}  {iphone}  {cft}  frames={frames}{ep_tag}  {gate}"


# ── CoinFT drift detection ────────────────────────────────────────────────────

DRIFT_THRESHOLD_N = 0.3    # N per axis
DRIFT_WARN_SECONDS = 10    # consecutive seconds above threshold before warning


def _max_force_abs(state: dict) -> float | None:
    """Return the max absolute force across both sides and Fx/Fy/Fz, or None if not calibrated."""
    coinft = state.get("coinft") or state.get("teensy") or {}
    wrench = coinft.get("wrench")
    if not isinstance(wrench, list) or len(wrench) < 2:
        return None  # not calibrated, can't assess
    max_val = 0.0
    for side in wrench[:2]:
        if isinstance(side, list):
            for i in range(3):  # Fx, Fy, Fz only (indices 0-2)
                v = side[i] if i < len(side) else None
                if isinstance(v, (int, float)):
                    max_val = max(max_val, abs(v))
    return max_val




def _run_step(label: str, cmd: list[str]) -> bool:
    print(f"\n{'─' * 60}\n  {label}\n{'─' * 60}")
    result = subprocess.run(cmd, cwd=str(_ENTRYPOINTS_DIR.parent))
    if result.returncode != 0:
        print(f"\nERROR: {label} failed (exit {result.returncode})", file=sys.stderr)
        return False
    print(f"  done: {label}")
    return True


def _auto_process(run_dir: str, episode_index: int, export_config: str) -> None:
    py = sys.executable
    ep = str(episode_index)
    print(f"\n{'═' * 60}\n  Auto-process episode {ep} in {run_dir}\n{'═' * 60}")

    steps = [
        ("Normalize", [py, str(_ENTRYPOINTS_DIR / "normalize_run.py"), run_dir, ep]),
        ("Align",     [py, str(_ENTRYPOINTS_DIR / "align_run.py"),     run_dir, ep]),
    ]
    export_cmd = [py, str(_ENTRYPOINTS_DIR / "export_run.py"), run_dir, ep,
                  "--format", "umift-replay-buffer-zarr"]
    if export_config:
        export_cmd += ["--config", export_config]
    steps.append(("Export", export_cmd))

    for label, cmd in steps:
        if not _run_step(label, cmd):
            return
    print(f"\n  Episode {ep} processed successfully.")


def _get_last_episode(host: str, port: int) -> tuple[str | None, int | None]:
    try:
        s = _state(host, port)
        ctrl = s.get("recording_control") or {}
        run_dir = s.get("run_dir") or ""
        ep = ctrl.get("episode_index")
        if run_dir and isinstance(ep, int):
            return run_dir, ep
    except Exception:
        pass
    return None, None


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_stop(host: str, port: int) -> int:
    print("Sending stop_capture…", flush=True)
    try:
        r = _cmd(host, port, "stop_capture")
        print(f"Stopped: ok={r.get('ok')}")
        return 0
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: cannot reach {host}:{port} — {e}", file=sys.stderr)
        return 1


def cmd_run(host: str, port: int, output_root: str, run_dir: str,
            auto_process: bool, export_config: str) -> int:

    # 1. Connect
    print(f"Connecting to {host}:{port}…", flush=True)
    try:
        _state(host, port)
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: cannot reach GUI at {host}:{port} — {e}", file=sys.stderr)
        print("Make sure receiver_web_gui.py is running first.", file=sys.stderr)
        return 1

    # 2. Arm all
    print("Arming all devices (iPhone + D435 + CoinFT)…")
    try:
        arm_args: dict = {}
        if output_root:
            arm_args["output_root"] = output_root
        if run_dir:
            arm_args["run_dir"] = run_dir
        r = _cmd(host, port, "arm_all", arm_args)
        if not r.get("ok"):
            print(f"ERROR: arm_all failed — {r.get('error') or r}", file=sys.stderr)
            return 1
        if r.get("run_dir"):
            print(f"Run directory: {r['run_dir']}")
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: arm_all failed — {e}", file=sys.stderr)
        return 1

    # 3. Wait for gate ready
    print(f"Waiting for all streams to be ready (timeout {ARM_TIMEOUT_S}s)…")
    deadline = time.monotonic() + ARM_TIMEOUT_S
    last_reasons: list = []
    while time.monotonic() < deadline:
        try:
            s = _state(host, port)
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
            continue
        summary = s.get("capture_summary") or {}
        reasons = summary.get("gate_reasons") or []
        if reasons != last_reasons:
            for reason in reasons:
                print(f"  waiting: {reason}")
            last_reasons = list(reasons)
        if summary.get("ready"):
            print("Gate ready — all streams live.")
            break
        time.sleep(1.0)
    else:
        print("ERROR: streams did not become ready within timeout.", file=sys.stderr)
        try:
            s = _state(host, port)
            for r in (s.get("capture_summary") or {}).get("gate_reasons") or []:
                print(f"  blocked: {r}", file=sys.stderr)
        except Exception:
            pass
        return 1

    # 4. Monitor loop
    if auto_process:
        print("Monitoring. iPhone controls recording. Ctrl-C to stop and exit.")
    else:
        print("Monitoring. iPhone controls recording. Ctrl-C to stop and exit.")
    print()

    stop_requested = False
    last_episode: int | None = None
    last_run_dir: str | None = None
    drift_ticks = 0          # consecutive seconds with force > threshold while not recording
    drift_warned = False     # suppress repeat warnings until it clears

    def _sigint(_sig: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _sigint)

    try:
        while not stop_requested:
            try:
                s = _state(host, port)
            except (urllib.error.URLError, OSError) as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: lost connection — {e}")
                time.sleep(POLL_INTERVAL_S)
                continue

            print(_status_line(s), flush=True)

            ctrl = s.get("recording_control") or {}
            ep = ctrl.get("episode_index")
            active = ctrl.get("active", False)

            # CoinFT drift check: only when not recording
            if not active:
                max_f = _max_force_abs(s)
                if max_f is not None and max_f > DRIFT_THRESHOLD_N:
                    drift_ticks += 1
                    if drift_ticks >= DRIFT_WARN_SECONDS and not drift_warned:
                        print(
                            f"\n{'!' * 60}\n"
                            f"  WARNING: CoinFT force drift detected ({max_f:.3f} N > {DRIFT_THRESHOLD_N} N)\n"
                            f"  Sensor has been above threshold for {drift_ticks}s while not recording.\n"
                            f"  Please re-zero: click 'Start / Re-zero CoinFT' in the GUI,\n"
                            f"  or run:  python3 entrypoints/capture_cli.py stop --port {port}\n"
                            f"           python3 entrypoints/capture_cli.py run  --port {port}\n"
                            f"{'!' * 60}\n",
                            flush=True,
                        )
                        drift_warned = True
                else:
                    if drift_warned:
                        print("  CoinFT force drift cleared.", flush=True)
                    drift_ticks = 0
                    drift_warned = False
            else:
                # reset while recording so it doesn't fire immediately after stop
                drift_ticks = 0
                drift_warned = False

            # Detect episode completion: was recording, now stopped
            if (
                auto_process
                and last_episode is not None
                and not active
                and ctrl.get("last_event") == "stopped"
            ):
                rd = s.get("run_dir") or last_run_dir
                if rd and last_episode is not None:
                    _auto_process(rd, last_episode, export_config)
                last_episode = None

            if active:
                last_episode = ep if isinstance(ep, int) else last_episode
                last_run_dir = s.get("run_dir") or last_run_dir

            time.sleep(POLL_INTERVAL_S)

    finally:
        if stop_requested:
            print("\nCtrl-C — sending stop_capture…")
            try:
                _cmd(host, port, "stop_capture")
                print("Stopped.")
                if auto_process:
                    time.sleep(1.5)  # let the server finalize the episode record
                    rd, ep = _get_last_episode(host, port)
                    if rd and ep is not None:
                        _auto_process(rd, ep, export_config)
                    else:
                        print("WARNING: could not determine last episode — run normalize/align/export manually.",
                              file=sys.stderr)
            except (urllib.error.URLError, OSError) as e:
                print(f"WARNING: stop failed — {e}", file=sys.stderr)
            return 2

    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        prog="capture_cli.py",
        description=(
            "Arm all devices, monitor capture, and optionally auto-process episodes.\n"
            "Recording is controlled by the iPhone; this script only monitors and reacts."
        ),
    )
    p.add_argument("command", choices=["run", "stop"],
                   help="'run' to start monitoring, 'stop' to disarm")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"GUI host (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"GUI port (default {DEFAULT_PORT})")
    p.add_argument("--output-root", default="", metavar="PATH",
                   help="Override output root directory")
    p.add_argument("--run-dir", default="", metavar="PATH",
                   help="Resume a specific run directory")
    p.add_argument("--auto-process", action="store_true",
                   help="After each episode stops, auto-run normalize → align → export")
    p.add_argument("--export-config", default="", metavar="PATH",
                   help="Export config JSON/YAML for export_run.py (used with --auto-process)")
    args = p.parse_args()

    if args.command == "stop":
        return cmd_stop(args.host, args.port)
    return cmd_run(args.host, args.port, args.output_root, args.run_dir,
                   args.auto_process, args.export_config)


if __name__ == "__main__":
    sys.exit(main())
