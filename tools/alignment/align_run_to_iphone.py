#!/usr/bin/env python3
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


FORCE_COLUMNS = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
GRIPPER_COLUMNS = ["frame_index", "iphone_pose_time_s", "gripper_width_m", "confidence", "status"]


def parse_iso(value: str) -> float:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).timestamp()


def iso_from_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")


def find_demo(run_dir: Path) -> tuple[Path, str, dict]:
    iphone_root = run_dir / "iphone_gripper"
    demos = sorted(iphone_root.glob("**/*_demonstration"))
    if not demos:
        raise SystemExit(f"No *_demonstration folders found under {iphone_root}")
    demo_dir = max(demos, key=lambda p: p.stat().st_mtime)
    sides = [s for s in ("left", "right") if (demo_dir / f"{s}.json").exists()]
    if len(sides) != 1:
        raise SystemExit(f"Expected exactly one side json in {demo_dir}, found {sides}")
    side = sides[0]
    meta = json.loads((demo_dir / f"{side}.json").read_text(encoding="utf-8"))
    return demo_dir, side, meta


def read_coinft_csv(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    if not rows:
        raise SystemExit(f"No rows in {csv_path}")
    ts = np.array([parse_iso(row["Timestamp"]) for row in rows], dtype=np.float64)
    values = np.array([[float(row[c]) for c in FORCE_COLUMNS] for row in rows], dtype=np.float64)
    return rows, ts, values


def read_global_timestamps(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    if not rows:
        raise SystemExit(f"No rows in {csv_path}")
    time_key = None
    for candidate in ("host_receive_time_s", "frame_time_s", "timestamp_s", "time_s"):
        if candidate in rows[0]:
            time_key = candidate
            break
    if time_key is None:
        raise SystemExit(f"Could not find timestamp column in {csv_path}")
    ts = np.array([float(row[time_key]) for row in rows], dtype=np.float64)
    return rows, ts, time_key


def read_gripper_csv(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    if not rows:
        raise SystemExit(f"No rows in {csv_path}")
    ts = np.array([float(row["iphone_pose_time_s"]) for row in rows], dtype=np.float64)
    width = np.array([float(row["gripper_width_m"]) for row in rows], dtype=np.float64)
    conf = np.array([float(row.get("confidence", "1.0")) for row in rows], dtype=np.float64)
    return rows, ts, width, conf


def nearest_indices(sample_ts: np.ndarray, target_ts: np.ndarray):
    indices = np.searchsorted(sample_ts, target_ts)
    left = np.clip(indices - 1, 0, len(sample_ts) - 1)
    right = np.clip(indices, 0, len(sample_ts) - 1)
    choose_right = np.abs(sample_ts[right] - target_ts) < np.abs(sample_ts[left] - target_ts)
    nearest = np.where(choose_right, right, left)
    age = target_ts - sample_ts[nearest]
    return nearest, age


def interpolate_columns(sample_ts: np.ndarray, values: np.ndarray, target_ts: np.ndarray):
    out = np.empty((len(target_ts), values.shape[1]), dtype=np.float64)
    for i in range(values.shape[1]):
        out[:, i] = np.interp(target_ts, sample_ts, values[:, i])
    return out


def rotation_matrix_to_rotvec(rmat: np.ndarray) -> np.ndarray:
    trace = np.trace(rmat)
    cos_theta = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    theta = math.acos(cos_theta)
    if theta < 1e-8:
        return np.zeros(3, dtype=np.float64)
    denom = 2.0 * math.sin(theta)
    rx = (rmat[2, 1] - rmat[1, 2]) / denom
    ry = (rmat[0, 2] - rmat[2, 0]) / denom
    rz = (rmat[1, 0] - rmat[0, 1]) / denom
    axis = np.array([rx, ry, rz], dtype=np.float64)
    return axis * theta


def homogeneous_transform_to_pose6d(transform: np.ndarray) -> np.ndarray:
    arr = np.asarray(transform, dtype=np.float64)
    if arr.shape != (4, 4):
        raise SystemExit(f"poseTransforms entry must have shape (4, 4), got {arr.shape}")
    rmat = arr[:3, :3]
    pos = arr[:3, 3]
    rotvec = rotation_matrix_to_rotvec(rmat)
    return np.array([pos[0], pos[1], pos[2], rotvec[0], rotvec[1], rotvec[2]], dtype=np.float64)


def quaternion_to_rotation_matrix(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    norm = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
    if norm == 0:
        return np.eye(3, dtype=np.float64)
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    return np.array([
        [1 - 2*qy*qy - 2*qz*qz, 2*qx*qy - 2*qz*qw, 2*qx*qz + 2*qy*qw],
        [2*qx*qy + 2*qz*qw, 1 - 2*qx*qx - 2*qz*qz, 2*qy*qz - 2*qx*qw],
        [2*qx*qz - 2*qy*qw, 2*qy*qz + 2*qx*qw, 1 - 2*qx*qx - 2*qy*qy],
    ], dtype=np.float64)


def pose_row_from_meta(meta: dict, index: int) -> np.ndarray:
    if "cameraPoses" in meta:
        pose = meta["cameraPoses"][index]
        pos = pose["position"]
        quat = pose["orientation"]
        rmat = quaternion_to_rotation_matrix(quat["x"], quat["y"], quat["z"], quat["w"])
        rotvec = rotation_matrix_to_rotvec(rmat)
        return np.array([pos["x"], pos["y"], pos["z"], rotvec[0], rotvec[1], rotvec[2]], dtype=np.float64)

    if "poseTransforms" in meta:
        return homogeneous_transform_to_pose6d(meta["poseTransforms"][index])

    for key in ("poses", "pose6d", "sessionWorldPoses"):
        if key in meta:
            pose = meta[key][index]
            if len(pose) == 6:
                return np.array(pose, dtype=np.float64)

    raise SystemExit("Could not find pose data in iPhone metadata. Expected cameraPoses, poseTransforms, or a 6D pose list.")


def compute_effective_start(target_ts: np.ndarray, width: np.ndarray, closed_threshold: float) -> tuple[float, int]:
    hits = np.where(width <= closed_threshold)[0]
    if len(hits) == 0:
        raise SystemExit("No close event found in gripper width.")
    first_idx = int(hits[0])
    return float(target_ts[first_idx] + 3.0), first_idx


def compute_effective_end(target_ts: np.ndarray, poses: np.ndarray, start_time: float, stable_window_s: float, min_episode_duration_s: float, translation_range_m: float, rotation_range_deg: float):
    valid = np.where(target_ts >= start_time + min_episode_duration_s)[0]
    if len(valid) == 0:
        return float(target_ts[-1]), len(target_ts) - 1, {"status": "fallback_end_of_sequence"}

    for start_idx in valid:
        end_time = target_ts[start_idx] + stable_window_s
        end_idx = np.searchsorted(target_ts, end_time, side="right") - 1
        if end_idx <= start_idx:
            continue
        window = poses[start_idx:end_idx + 1]
        trans = window[:, :3]
        rot = window[:, 3:]
        trans_range = float(np.max(np.linalg.norm(trans - np.mean(trans, axis=0), axis=1)))
        rot_deg = np.rad2deg(np.linalg.norm(rot - np.mean(rot, axis=0), axis=1))
        rot_range = float(np.max(rot_deg))
        if trans_range < translation_range_m and rot_range < rotation_range_deg:
            return float(target_ts[start_idx]), start_idx, {
                "status": "stable_pose_window",
                "windowStartIndex": int(start_idx),
                "windowEndIndex": int(end_idx),
                "translationRangeM": trans_range,
                "rotationRangeDeg": rot_range,
            }

    return float(target_ts[-1]), len(target_ts) - 1, {"status": "fallback_end_of_sequence"}


def main():
    parser = argparse.ArgumentParser(description="Align one UMIFT-datacollect run to iPhone poseTimes and build aligned intermediates.")
    parser.add_argument("run_dir", help="Run directory under runs/<run_id>.")
    parser.add_argument("--force-source", choices=["left", "right", "fused"], default="fused")
    parser.add_argument("--closed-threshold", type=float, help="Absolute close threshold in meters. Defaults to relative threshold from observed min/max.")
    parser.add_argument("--stable-window-s", type=float, default=3.0)
    parser.add_argument("--min-episode-duration-s", type=float, default=5.0)
    parser.add_argument("--translation-range-m", type=float, default=0.01)
    parser.add_argument("--rotation-range-deg", type=float, default=2.0)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    demo_dir, side, meta = find_demo(run_dir)
    pose_ts = np.array([parse_iso(x) for x in meta["poseTimes"]], dtype=np.float64)
    poses = np.stack([pose_row_from_meta(meta, i) for i in range(len(pose_ts))], axis=0)

    coinft_dir = run_dir / "coinft"
    lf_csvs = sorted(coinft_dir.glob("*LF.csv"))
    rf_csvs = sorted(coinft_dir.glob("*RF.csv"))
    if not lf_csvs or not rf_csvs:
        raise SystemExit(f"Could not find LF/RF CoinFT CSVs under {coinft_dir}")
    _, lf_ts, lf_vals = read_coinft_csv(lf_csvs[-1])
    _, rf_ts, rf_vals = read_coinft_csv(rf_csvs[-1])

    sample_ts = lf_ts if args.force_source != "right" else rf_ts
    if args.force_source == "left":
        force_vals = lf_vals
    elif args.force_source == "right":
        force_vals = rf_vals
    else:
        common_ts = lf_ts if len(lf_ts) <= len(rf_ts) else rf_ts
        lf_interp = interpolate_columns(lf_ts, lf_vals, common_ts)
        rf_interp = interpolate_columns(rf_ts, rf_vals, common_ts)
        sample_ts = common_ts
        force_vals = 0.5 * (lf_interp + rf_interp)

    global_csv = run_dir / "global_camera" / "frame_timestamps.csv"
    if not global_csv.exists():
        raise SystemExit(f"Missing global camera timestamp file: {global_csv}")
    _, global_ts, global_time_key = read_global_timestamps(global_csv)

    gripper_csv = run_dir / "gripper_width" / "gripper_width_by_frame.csv"
    if not gripper_csv.exists():
        raise SystemExit(f"Missing gripper width CSV: {gripper_csv}")
    _, gripper_ts, gripper_width, gripper_conf = read_gripper_csv(gripper_csv)
    width_interp = np.interp(pose_ts, gripper_ts, gripper_width)
    conf_interp = np.interp(pose_ts, gripper_ts, gripper_conf)

    width_min = float(np.nanmin(width_interp))
    width_max = float(np.nanmax(width_interp))
    closed_threshold = float(args.closed_threshold) if args.closed_threshold is not None else width_min + 0.15 * (width_max - width_min)
    effective_start_time, start_idx = compute_effective_start(pose_ts, width_interp, closed_threshold)
    effective_end_time, end_idx, end_info = compute_effective_end(
        pose_ts, poses, effective_start_time, args.stable_window_s, args.min_episode_duration_s,
        args.translation_range_m, args.rotation_range_deg,
    )

    in_effective = (pose_ts >= effective_start_time) & (pose_ts <= effective_end_time)
    effective_indices = np.where(in_effective)[0]
    if len(effective_indices) == 0:
        raise SystemExit("No frames remain after effective segment cropping.")

    aligned_force = interpolate_columns(sample_ts, force_vals, pose_ts)
    force_nearest_idx, force_age = nearest_indices(sample_ts, pose_ts)
    global_nearest_idx, global_age = nearest_indices(global_ts, pose_ts)

    aligned_dir = run_dir / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)
    aligned_index_csv = aligned_dir / "aligned_index.csv"
    aligned_force_csv = aligned_dir / "aligned_force.csv"
    aligned_pose_csv = aligned_dir / "aligned_pose.csv"
    report_json = aligned_dir / "ALIGNMENT_REPORT.json"
    report_md = aligned_dir / "ALIGNMENT_REPORT.md"

    with aligned_index_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "frame_index", "target_time_s", "target_time_iso8601", "in_effective_segment",
            "effective_start_time_s", "effective_end_time_s",
            "global_frame_index", "global_frame_age_s",
            "force_sample_index", "force_sample_age_s",
            "gripper_width_m", "gripper_confidence",
            "gripper_state",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, ts in enumerate(pose_ts):
            writer.writerow({
                "frame_index": i,
                "target_time_s": f"{ts:.9f}",
                "target_time_iso8601": iso_from_ts(ts),
                "in_effective_segment": bool(in_effective[i]),
                "effective_start_time_s": f"{effective_start_time:.9f}",
                "effective_end_time_s": f"{effective_end_time:.9f}",
                "global_frame_index": int(global_nearest_idx[i]),
                "global_frame_age_s": f"{global_age[i]:.6f}",
                "force_sample_index": int(force_nearest_idx[i]),
                "force_sample_age_s": f"{force_age[i]:.6f}",
                "gripper_width_m": f"{width_interp[i]:.6f}",
                "gripper_confidence": f"{conf_interp[i]:.6f}",
                "gripper_state": 0.0 if width_interp[i] <= closed_threshold else 1.0,
            })

    with aligned_force_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["frame_index", "target_time_s", "target_time_iso8601", *FORCE_COLUMNS]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, ts in enumerate(pose_ts):
            row = {
                "frame_index": i,
                "target_time_s": f"{ts:.9f}",
                "target_time_iso8601": iso_from_ts(ts),
            }
            for col, value in zip(FORCE_COLUMNS, aligned_force[i]):
                row[col] = f"{value:.6f}"
            writer.writerow(row)

    with aligned_pose_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["frame_index", "target_time_s", "target_time_iso8601", "x", "y", "z", "rx", "ry", "rz"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, ts in enumerate(pose_ts):
            writer.writerow({
                "frame_index": i,
                "target_time_s": f"{ts:.9f}",
                "target_time_iso8601": iso_from_ts(ts),
                "x": f"{poses[i,0]:.9f}",
                "y": f"{poses[i,1]:.9f}",
                "z": f"{poses[i,2]:.9f}",
                "rx": f"{poses[i,3]:.9f}",
                "ry": f"{poses[i,4]:.9f}",
                "rz": f"{poses[i,5]:.9f}",
            })

    report = {
        "runDir": str(run_dir),
        "demoDir": str(demo_dir),
        "side": side,
        "frameCount": int(len(pose_ts)),
        "effectiveStartTime": iso_from_ts(effective_start_time),
        "effectiveEndTime": iso_from_ts(effective_end_time),
        "effectiveFrameCount": int(np.sum(in_effective)),
        "effectiveStartIndex": int(start_idx),
        "effectiveEndIndex": int(end_idx),
        "closedThresholdM": closed_threshold,
        "gripperWidthMinM": width_min,
        "gripperWidthMaxM": width_max,
        "forceSource": args.force_source,
        "forceFrame": "coinft",
        "globalTimestampColumn": global_time_key,
        "maxAbsForceAgeS": float(np.max(np.abs(force_age))),
        "p95AbsForceAgeS": float(np.quantile(np.abs(force_age), 0.95)),
        "maxAbsGlobalAgeS": float(np.max(np.abs(global_age))),
        "p95AbsGlobalAgeS": float(np.quantile(np.abs(global_age), 0.95)),
        "endDetection": end_info,
        "outputs": {
            "alignedIndexCsv": str(aligned_index_csv),
            "alignedForceCsv": str(aligned_force_csv),
            "alignedPoseCsv": str(aligned_pose_csv),
        },
    }
    report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report_md.write_text(
        "\n".join([
            "# Alignment Report",
            "",
            f"- runDir: `{run_dir}`",
            f"- demoDir: `{demo_dir}`",
            f"- frameCount: `{len(pose_ts)}`",
            f"- effectiveStartTime: `{report['effectiveStartTime']}`",
            f"- effectiveEndTime: `{report['effectiveEndTime']}`",
            f"- effectiveFrameCount: `{report['effectiveFrameCount']}`",
            f"- forceSource: `{args.force_source}`",
            f"- closedThresholdM: `{closed_threshold:.6f}`",
            f"- maxAbsForceAgeS: `{report['maxAbsForceAgeS']:.6f}`",
            f"- p95AbsForceAgeS: `{report['p95AbsForceAgeS']:.6f}`",
            f"- maxAbsGlobalAgeS: `{report['maxAbsGlobalAgeS']:.6f}`",
            f"- p95AbsGlobalAgeS: `{report['p95AbsGlobalAgeS']:.6f}`",
            f"- endDetectionStatus: `{end_info.get('status', 'unknown')}`",
            "",
            "## Outputs",
            "",
            f"- aligned_index: `{aligned_index_csv}`",
            f"- aligned_force: `{aligned_force_csv}`",
            f"- aligned_pose: `{aligned_pose_csv}`",
            "",
        ]),
        encoding="utf-8",
    )
    print(f"Wrote {aligned_index_csv}")
    print(f"Wrote {aligned_force_csv}")
    print(f"Wrote {aligned_pose_csv}")
    print(f"Wrote {report_json}")
    print(f"Wrote {report_md}")


if __name__ == "__main__":
    main()
