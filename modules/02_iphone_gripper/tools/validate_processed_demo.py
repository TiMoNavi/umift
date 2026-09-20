#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


def parse_iso(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).timestamp()


def video_frame_count(path):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open {path}")
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    cap.release()
    return count, width, height, fps


def validate_demo(demo_dir, side):
    errors = []
    warnings = []
    facts = []
    demo_dir = Path(demo_dir)
    json_path = demo_dir / f"{side}.json"
    rgb_path = demo_dir / f"{side}_rgb.mp4"
    uw_path = demo_dir / f"{side}_ultrawidergb.mp4"
    depth_path = demo_dir / f"{side}_depth.raw"

    if not json_path.exists():
        return [f"Missing {json_path}"], warnings, facts
    data = json.loads(json_path.read_text(encoding="utf-8"))

    pose_times = data.get("poseTimes", [])
    pose_transforms = data.get("poseTransforms", [])
    uw_times = data.get("ultrawideRGBTimes", [])
    depth_times = data.get("depthTimes", [])
    gripper_widths = data.get("gripperWidthMeters")

    facts.append(f"sessionName={data.get('sessionName')}")
    facts.append(f"side={data.get('side')} type={data.get('type')}")
    facts.append(f"pose frames={len(pose_times)}")

    if not pose_times:
        errors.append("poseTimes is empty")
    if len(pose_transforms) != len(pose_times):
        errors.append(f"poseTransforms length {len(pose_transforms)} != poseTimes length {len(pose_times)}")
    if len(uw_times) != len(pose_times):
        errors.append(f"ultrawideRGBTimes length {len(uw_times)} != poseTimes length {len(pose_times)}")
    if depth_times and len(depth_times) != len(pose_times):
        warnings.append(f"depthTimes length {len(depth_times)} != poseTimes length {len(pose_times)}")
    if gripper_widths is not None and len(gripper_widths) != len(pose_times):
        errors.append(f"gripperWidthMeters length {len(gripper_widths)} != poseTimes length {len(pose_times)}")

    try:
        timestamps = [parse_iso(x) for x in pose_times]
        if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            errors.append("poseTimes must be strictly increasing")
        elif len(timestamps) > 1:
            duration = timestamps[-1] - timestamps[0]
            facts.append(f"duration={duration:.3f}s approx_fps={(len(timestamps)-1)/duration:.2f}")
    except Exception as exc:
        errors.append(f"Could not parse poseTimes: {exc}")

    for i, transform in enumerate(pose_transforms):
        arr = np.asarray(transform)
        if arr.shape != (4, 4):
            errors.append(f"poseTransforms[{i}] shape {arr.shape} != (4, 4)")
            break

    if not rgb_path.exists():
        errors.append(f"Missing {rgb_path.name}")
    else:
        rgb_count, width, height, fps = video_frame_count(rgb_path)
        facts.append(f"{rgb_path.name}: frames={rgb_count} size={width}x{height} fps={fps:.2f}")
        if rgb_count != len(pose_times):
            errors.append(f"{rgb_path.name} frame count {rgb_count} != poseTimes length {len(pose_times)}")

    if not uw_path.exists():
        errors.append(f"Missing {uw_path.name}")
    else:
        uw_count, width, height, fps = video_frame_count(uw_path)
        expected_uw = sum(1 for x in uw_times if x)
        facts.append(f"{uw_path.name}: frames={uw_count} size={width}x{height} fps={fps:.2f}")
        if uw_count != expected_uw:
            errors.append(f"{uw_path.name} frame count {uw_count} != non-empty ultrawideRGBTimes {expected_uw}")

    if not depth_path.exists():
        errors.append(f"Missing {depth_path.name}")
    else:
        raw = np.fromfile(depth_path, dtype=np.float16)
        frame_size = 192 * 256
        if raw.size % frame_size != 0:
            errors.append(f"{depth_path.name} float16 item count {raw.size} is not divisible by 192*256")
        else:
            depth_count = raw.size // frame_size
            facts.append(f"{depth_path.name}: frames={depth_count} shape=({depth_count},192,256)")
            if depth_count != len(pose_times):
                errors.append(f"{depth_path.name} frame count {depth_count} != poseTimes length {len(pose_times)}")

    return errors, warnings, facts


def main():
    parser = argparse.ArgumentParser(description="Validate UMI-FT processed iPhone demo folders.")
    parser.add_argument("path", help="A *_demonstration directory or processed_data/iphone directory.")
    parser.add_argument("--side", choices=["left", "right"], default="right")
    args = parser.parse_args()

    root = Path(args.path)
    demo_dirs = [root] if root.name.endswith("_demonstration") else sorted(root.glob("**/*_demonstration"))
    if not demo_dirs:
        raise SystemExit(f"No *_demonstration directories found under {root}")

    total_errors = 0
    for demo_dir in demo_dirs:
        errors, warnings, facts = validate_demo(demo_dir, args.side)
        print(f"\n{demo_dir}")
        for fact in facts:
            print(f"  {fact}")
        for warning in warnings:
            print(f"  WARNING: {warning}")
        for error in errors:
            print(f"  ERROR: {error}")
        if not errors:
            print("  OK")
        total_errors += len(errors)

    if total_errors:
        raise SystemExit(f"Validation failed with {total_errors} error(s).")
    print(f"\nValidated {len(demo_dirs)} demo(s).")


if __name__ == "__main__":
    main()

