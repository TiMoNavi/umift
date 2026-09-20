#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

import cv2
import numpy as np
import zarr


def parse_iso(value: str) -> float:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    from datetime import datetime
    return datetime.fromisoformat(value).timestamp()


def load_csv_rows(path: Path):
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_demo(run_dir: Path):
    demos = sorted((run_dir / "iphone_gripper").glob("**/*_demonstration"))
    if not demos:
        raise SystemExit("No iPhone demonstration found.")
    demo = max(demos, key=lambda p: p.stat().st_mtime)
    side = [s for s in ("left", "right") if (demo / f"{s}.json").exists()]
    if len(side) != 1:
        raise SystemExit(f"Expected exactly one side json in {demo}, found {side}")
    side = side[0]
    meta = json.loads((demo / f"{side}.json").read_text(encoding="utf-8"))
    return demo, side, meta


def read_video_frames(video_path: Path):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {video_path}")
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frames.append(frame)
    cap.release()
    if not frames:
        raise SystemExit(f"No frames decoded from {video_path}")
    return frames


def resize_chw(frame_rgb: np.ndarray):
    resized = cv2.resize(frame_rgb, (320, 240), interpolation=cv2.INTER_LINEAR)
    chw = np.transpose(resized, (2, 0, 1))
    return chw.astype(np.uint8)


def load_pose_csv(path: Path):
    rows = load_csv_rows(path)
    arr = np.array([[float(r[k]) for k in ("x", "y", "z", "rx", "ry", "rz")] for r in rows], dtype=np.float32)
    return rows, arr


def load_force_csv(path: Path):
    rows = load_csv_rows(path)
    arr = np.array([[float(r[k]) for k in ("Fx", "Fy", "Fz", "Mx", "My", "Mz")] for r in rows], dtype=np.float32)
    return rows, arr


def build_episode_ends(episode: np.ndarray):
    unique_eps = np.unique(episode)
    ends = []
    running = 0
    for ep in unique_eps:
        running += int(np.sum(episode == ep))
        ends.append(running)
    return np.array(ends, dtype=np.uint32)


def minmax_json(pos: np.ndarray, action: np.ndarray, force: np.ndarray):
    return {
        "pos": {"max": np.max(pos, axis=0).tolist(), "min": np.min(pos, axis=0).tolist()},
        "action": {"max": np.max(action, axis=0).tolist(), "min": np.min(action, axis=0).tolist()},
        "force": {"max": np.max(force, axis=0).tolist(), "min": np.min(force, axis=0).tolist()},
    }


def main():
    parser = argparse.ArgumentParser(description="Export one aligned run to ForceFlow-compatible Zarr.")
    parser.add_argument("run_dir", help="Run directory under runs/<run_id>.")
    parser.add_argument("--task", required=True, help="Task name, used as zarr directory name.")
    parser.add_argument("--output-dir", help="Defaults to <run_dir>/export.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    output_root = Path(args.output_dir).resolve() if args.output_dir else run_dir / "export"
    output_root.mkdir(parents=True, exist_ok=True)
    zarr_path = output_root / f"{args.task}.zarr"

    demo_dir, side, meta = find_demo(run_dir)
    rgb_video = demo_dir / f"{side}_rgb.mp4"
    global_video = run_dir / "global_camera" / "rgb.mp4"
    aligned_index = load_csv_rows(run_dir / "aligned" / "aligned_index.csv")
    _, pos_arr = load_pose_csv(run_dir / "aligned" / "aligned_pose.csv")
    _, force_arr = load_force_csv(run_dir / "aligned" / "aligned_force.csv")

    arm_frames = read_video_frames(rgb_video)
    fix_frames = read_video_frames(global_video)

    effective_rows = [r for r in aligned_index if str(r["in_effective_segment"]).lower() in ("true", "1")]
    if not effective_rows:
        raise SystemExit("No effective rows found in aligned_index.csv")

    frame_indices = np.array([int(r["frame_index"]) for r in effective_rows], dtype=np.int64)
    global_indices = np.array([int(r["global_frame_index"]) for r in effective_rows], dtype=np.int64)
    gripper_state = np.array([[float(r["gripper_state"])] for r in effective_rows], dtype=np.float32)

    rgb_arm = np.stack([resize_chw(arm_frames[min(i, len(arm_frames) - 1)]) for i in frame_indices], axis=0)
    rgb_fix = np.stack([resize_chw(fix_frames[min(i, len(fix_frames) - 1)]) for i in global_indices], axis=0)
    pos = pos_arr[frame_indices].astype(np.float32)
    force = force_arr[frame_indices].astype(np.float32)

    action = np.zeros_like(pos, dtype=np.float32)
    if len(pos) > 1:
        action[:-1] = pos[1:] - pos[:-1]
        action[-1] = action[-2]

    gripper_action = np.zeros_like(gripper_state, dtype=np.float32)
    if len(gripper_state) > 1:
        gripper_action[:-1] = gripper_state[1:]
        gripper_action[-1] = gripper_state[-1]
    else:
        gripper_action[0] = gripper_state[0]

    episode = np.zeros((len(frame_indices),), dtype=np.uint16)
    episode_ends = build_episode_ends(episode)

    root = zarr.open(str(zarr_path), mode="w")
    data = root.create_group("data")
    meta_group = root.create_group("meta")
    data.create_dataset("rgb_arm", data=rgb_arm, shape=rgb_arm.shape, dtype=np.uint8)
    data.create_dataset("rgb_fix", data=rgb_fix, shape=rgb_fix.shape, dtype=np.uint8)
    data.create_dataset("pos", data=pos, shape=pos.shape, dtype=np.float32)
    data.create_dataset("force", data=force, shape=force.shape, dtype=np.float32)
    data.create_dataset("action", data=action, shape=action.shape, dtype=np.float32)
    data.create_dataset("gripper_state", data=gripper_state, shape=gripper_state.shape, dtype=np.float32)
    data.create_dataset("gripper_action", data=gripper_action, shape=gripper_action.shape, dtype=np.float32)
    data.create_dataset("episode", data=episode, shape=episode.shape, dtype=np.uint16)
    meta_group.create_dataset("episode_ends", data=episode_ends, shape=episode_ends.shape, dtype=np.uint32)

    normalizer = minmax_json(pos, action, force)
    normalizer_path = zarr_path / f"{args.task}_normalizer.json"
    normalizer_path.write_text(json.dumps(normalizer, indent=2, ensure_ascii=False), encoding="utf-8")

    export_meta = {
        "runDir": str(run_dir),
        "demoDir": str(demo_dir),
        "task": args.task,
        "side": side,
        "frameCount": int(len(frame_indices)),
        "forceSource": "see aligned/ALIGNMENT_REPORT.json",
        "forceFrame": "coinft",
        "posConvention": "[x,y,z,rx,ry,rz]",
        "actionConvention": "delta_pose_6d",
        "gripperStateConvention": "0=closed,1=open",
        "gripperActionConvention": "0=close,1=open",
    }
    (zarr_path / "export_metadata.json").write_text(json.dumps(export_meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {zarr_path}")
    print(f"Wrote {normalizer_path}")
    print(f"Wrote {zarr_path / 'export_metadata.json'}")


if __name__ == "__main__":
    main()
