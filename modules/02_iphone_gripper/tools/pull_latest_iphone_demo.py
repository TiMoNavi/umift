#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_ROOT = REPO_ROOT / "modules" / "02_iphone_gripper"
DEFAULT_DEVICE = "6805CFA8-2AA2-5509-9D00-D96FF37FA6CC"
DEFAULT_BUNDLE_ID = "com.local.umift.capture.mvp"
DEFAULT_DEVELOPER_DIR = "/Applications/Xcode.app/Contents/Developer"
VALIDATOR = REPO_ROOT / "modules" / "02_iphone_gripper" / "tools" / "validate_processed_demo.py"


def run(cmd, env=None):
    print("+", " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, check=True, text=True, capture_output=True, env=env)


def parse_iso(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def side_for_demo(demo_dir):
    sides = []
    for side in ("left", "right"):
        if (demo_dir / f"{side}.json").exists():
            sides.append(side)
    if len(sides) != 1:
        raise RuntimeError(f"Expected exactly one side json in {demo_dir}, found {sides}")
    return sides[0]


def latest_demo(iphone_root):
    demos = sorted(Path(iphone_root).glob("**/*_demonstration"))
    if not demos:
        raise RuntimeError(f"No *_demonstration folders found under {iphone_root}")
    return max(demos, key=demo_recording_timestamp)


def demo_recording_timestamp(demo_dir):
    for side in ("left", "right"):
        json_path = Path(demo_dir) / f"{side}.json"
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            value = data.get("recordingStartTime")
            if not value and data.get("poseTimes"):
                value = data["poseTimes"][0]
            if value:
                return parse_iso(value).timestamp()
        except Exception:
            pass
    return Path(demo_dir).stat().st_mtime


def pull_processed_data(args):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(args.destination or MODULE_ROOT / "runs" / f"iphone_pull_{timestamp}")
    dest.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DEVELOPER_DIR"] = args.developer_dir
    cmd = [
        "xcrun",
        "devicectl",
        "device",
        "copy",
        "from",
        "--device",
        args.device,
        "--domain-type",
        "appDataContainer",
        "--domain-identifier",
        args.bundle_id,
        "--source",
        "Documents/processed_data",
        "--destination",
        str(dest),
        "--remove-existing-content",
        "true",
    ]
    result = run(cmd, env=env)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return dest


def validate_demo(demo_dir, side):
    cmd = [sys.executable, str(VALIDATOR), str(demo_dir), "--side", side]
    result = run(cmd)
    return result.stdout.strip()


def depth_summary(demo_dir, side):
    raw_path = demo_dir / f"{side}_depth.raw"
    raw = np.fromfile(raw_path, dtype=np.float16)
    frame_size = 192 * 256
    if raw.size % frame_size != 0:
        return {"error": f"item count {raw.size} is not divisible by {frame_size}"}
    depth = raw.reshape(raw.size // frame_size, 192, 256).astype(np.float32)
    return {
        "frames": int(depth.shape[0]),
        "min_m": float(depth.min()),
        "max_m": float(depth.max()),
        "mean_m": float(depth.mean()),
        "nonzero_ratio": float((depth > 0).mean()),
    }


def video_summary(demo_dir, side, suffix):
    try:
        import cv2
    except Exception as exc:
        return {"error": f"cv2 unavailable: {exc}"}

    path = demo_dir / f"{side}_{suffix}.mp4"
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"error": f"OpenCV could not open {path.name}"}
    summary = {
        "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "fps": float(cap.get(cv2.CAP_PROP_FPS)),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    cap.release()
    return summary


def write_report(run_dir, demo_dir, side, validation_output):
    json_path = demo_dir / f"{side}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    pose_times = data.get("poseTimes", [])
    start = parse_iso(pose_times[0]) if pose_times else None
    end = parse_iso(pose_times[-1]) if pose_times else None
    duration = (end - start).total_seconds() if start and end else 0.0
    report_path = Path(run_dir) / "SESSION_REPORT.md"
    depth = depth_summary(demo_dir, side)
    rgb = video_summary(demo_dir, side, "rgb")
    ultrawide = video_summary(demo_dir, side, "ultrawidergb")
    selected_format = data.get("selectedARVideoFormat", {})
    supported_formats = data.get("arkitSupportedVideoFormats", [])
    iphone_coinft_dir = demo_dir / "coinft_sim"
    iphone_coinft_files = sorted(p.name for p in iphone_coinft_dir.glob("*")) if iphone_coinft_dir.exists() else []
    umift_coinft_dir = demo_dir / "coinft"
    umift_coinft_files = sorted(p.relative_to(umift_coinft_dir).as_posix() for p in umift_coinft_dir.glob("**/*.csv")) if umift_coinft_dir.exists() else []

    lines = [
        "# iPhone Demo Session Report",
        "",
        f"- generatedAt: `{datetime.now(timezone.utc).isoformat()}`",
        f"- demoDir: `{demo_dir}`",
        f"- side: `{side}`",
        f"- sessionName: `{data.get('sessionName')}`",
        f"- type: `{data.get('type')}`",
        f"- poseFrames: `{len(pose_times)}`",
        f"- durationSeconds: `{duration:.3f}`",
        f"- approxFPS: `{((len(pose_times) - 1) / duration):.2f}`" if duration > 0 and len(pose_times) > 1 else "- approxFPS: `unknown`",
        "",
        "## Depth",
        "",
        f"- depthSource: `{data.get('depthSource')}`",
        f"- realDepthFrameCount: `{data.get('realDepthFrameCount')}`",
        f"- missingDepthFrameCount: `{data.get('missingDepthFrameCount')}`",
        f"- depthSummary: `{json.dumps(depth, ensure_ascii=False)}`",
        "",
        "## Video",
        "",
        f"- rgbCaptureDeviceType: `{data.get('rgbCaptureDeviceType')}`",
        f"- rgbImageResolution: `{data.get('rgbImageResolution')}`",
        f"- ultrawideMode: `{data.get('ultrawideMode')}`",
        f"- rgbVideo: `{json.dumps(rgb, ensure_ascii=False)}`",
        f"- ultrawideVideo: `{json.dumps(ultrawide, ensure_ascii=False)}`",
        "",
        "## ARKit Video Format Diagnostics",
        "",
        f"- selectedARVideoFormat: `{json.dumps(selected_format, ensure_ascii=False)}`",
        f"- supportedVideoFormatCount: `{len(supported_formats)}`",
        "",
    ]
    for item in supported_formats:
        lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")

    lines.extend([
        "",
        "## Validator",
        "",
        "```text",
        validation_output,
        "```",
        "",
        "## CoinFT Merge Placeholder",
        "",
        "- coinftCsv: `not_attached`",
        "- overlapSeconds: `not_computed`",
        "- status: `iphone_demo_ready_for_coinft_alignment`",
        "",
        "## iPhone-Side CoinFT Simulation",
        "",
        f"- present: `{iphone_coinft_dir.exists()}`",
        f"- directory: `{iphone_coinft_dir if iphone_coinft_dir.exists() else 'not_present'}`",
        f"- files: `{', '.join(iphone_coinft_files) if iphone_coinft_files else 'none'}`",
        "",
        "## UMI-FT Style CoinFT CSV",
        "",
        f"- present: `{umift_coinft_dir.exists()}`",
        f"- directory: `{umift_coinft_dir if umift_coinft_dir.exists() else 'not_present'}`",
        f"- files: `{', '.join(umift_coinft_files) if umift_coinft_files else 'none'}`",
        "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Legacy MVP pull helper. Current app is UMIFTiPhoneCaptureCore JSONL.")
    parser.add_argument(
        "--allow-legacy-mvp",
        action="store_true",
        help="Required to pull or report archived UMIFTiPhoneCaptureMVP processed demos.",
    )
    parser.add_argument("--existing-root", help="Use an existing processed_data/iphone root or a previous pull directory instead of pulling.")
    parser.add_argument("--destination", help="Destination directory for a fresh pull.")
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--developer-dir", default=DEFAULT_DEVELOPER_DIR)
    args = parser.parse_args()

    if not args.allow_legacy_mvp:
        raise SystemExit(
            "This script targets archived UMIFTiPhoneCaptureMVP processed demos. "
            "Current data comes from UMIFTiPhoneCaptureCore via module 04 CaptureCore JSONL receiver. "
            "Rerun with --allow-legacy-mvp only for old MVP data."
        )

    run_dir = Path(args.existing_root).resolve() if args.existing_root else pull_processed_data(args).resolve()
    iphone_root = run_dir if run_dir.name == "iphone" else run_dir / "iphone"
    if not iphone_root.exists():
        raise SystemExit(f"Could not find iphone root at {iphone_root}")

    demo_dir = latest_demo(iphone_root)
    side = side_for_demo(demo_dir)
    validation_output = validate_demo(demo_dir, side)
    report_path = write_report(run_dir, demo_dir, side, validation_output)
    print(f"\nLatest demo: {demo_dir}")
    print(f"Side: {side}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
