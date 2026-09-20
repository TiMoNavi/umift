#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


LEFT_ID = 0
RIGHT_ID = 1
MARKER_SIZE_M = 0.016
ARUCO_DICT_NAME = "DICT_4X4_50"


def build_intrinsics(width: int, height: int) -> np.ndarray:
    # Approximate iPhone 15 Pro ultra-wide intrinsics from metadata.
    # 14 mm-equivalent, 4:3 sensor, 2.22 mm actual focal length.
    eq_f_mm = 14.0
    actual_f_mm = 2.22
    full_diag_mm = math.sqrt(36.0 ** 2 + 24.0 ** 2)
    sensor_diag_mm = actual_f_mm * full_diag_mm / eq_f_mm
    sensor_w_mm = sensor_diag_mm * 4.0 / 5.0
    sensor_h_mm = sensor_diag_mm * 3.0 / 5.0
    fx = actual_f_mm / sensor_w_mm * width
    fy = actual_f_mm / sensor_h_mm * height
    return np.array(
        [[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def detect_markers(img: np.ndarray):
    aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT_NAME))
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(aruco_dict, params)
    corners, ids, rejected = detector.detectMarkers(img)
    return corners, ids, rejected


def estimate_pose(
    corners: np.ndarray,
    K: np.ndarray,
    marker_size_m: float = MARKER_SIZE_M,
):
    obj = np.array(
        [
            [-marker_size_m / 2, marker_size_m / 2, 0.0],
            [marker_size_m / 2, marker_size_m / 2, 0.0],
            [marker_size_m / 2, -marker_size_m / 2, 0.0],
            [-marker_size_m / 2, -marker_size_m / 2, 0.0],
        ],
        dtype=np.float64,
    )
    ok, rvec, tvec = cv2.solvePnP(
        obj,
        corners.astype(np.float64),
        K,
        np.zeros((4, 1), dtype=np.float64),
        flags=cv2.SOLVEPNP_IPPE_SQUARE,
    )
    if not ok:
        return None
    return rvec.reshape(-1), tvec.reshape(-1)


def get_gripper_width(
    tag_dict: Dict[int, Dict[str, np.ndarray]],
    nominal_z: Optional[float] = 0.072,
    z_tolerance: float = 0.008,
) -> Optional[float]:
    zmax = None if nominal_z is None else nominal_z + z_tolerance
    zmin = None if nominal_z is None else nominal_z - z_tolerance

    left_x = None
    if LEFT_ID in tag_dict:
        tvec = tag_dict[LEFT_ID]["tvec"]
        if nominal_z is None or (zmin < tvec[-1] < zmax):
            left_x = float(tvec[0])

    right_x = None
    if RIGHT_ID in tag_dict:
        tvec = tag_dict[RIGHT_ID]["tvec"]
        if nominal_z is None or (zmin < tvec[-1] < zmax):
            right_x = float(tvec[0])

    if left_x is not None and right_x is not None:
        return right_x - left_x
    if left_x is not None:
        return abs(left_x) * 2.0
    if right_x is not None:
        return abs(right_x) * 2.0
    return None


def load_image(path: Path) -> np.ndarray:
    img = cv2.imread(str(path))
    if img is None:
        raise RuntimeError(f"Failed to read image: {path}")
    return img


def summarize(images: List[Path]) -> List[dict]:
    rows: List[dict] = []
    for path in images:
        img = load_image(path)
        h, w = img.shape[:2]
        K = build_intrinsics(w, h)
        corners, ids, rejected = detect_markers(img)
        tag_dict: Dict[int, Dict[str, np.ndarray]] = {}
        detected_ids = []
        if ids is not None:
            for marker_id, marker_corners in zip(ids.ravel().tolist(), corners):
                pose = estimate_pose(marker_corners.reshape(4, 2), K)
                if pose is None:
                    continue
                rvec, tvec = pose
                tag_dict[int(marker_id)] = {"rvec": rvec, "tvec": tvec, "corners": marker_corners.reshape(4, 2)}
                detected_ids.append(int(marker_id))

        width_raw_m = get_gripper_width(tag_dict, nominal_z=None)
        visible_z = [
            float(tag_dict[k]["tvec"][-1])
            for k in (LEFT_ID, RIGHT_ID)
            if k in tag_dict
        ]
        rows.append(
            {
                "file": path.name,
                "detected_ids": detected_ids,
                "num_candidates": 0 if ids is None else int(len(ids)),
                "num_rejected": 0 if rejected is None else int(len(rejected)),
                "width_raw_m": None if width_raw_m is None else float(width_raw_m),
                "mean_visible_z_m": None if not visible_z else float(np.mean(visible_z)),
                "left_tvec": None if LEFT_ID not in tag_dict else tag_dict[LEFT_ID]["tvec"].tolist(),
                "right_tvec": None if RIGHT_ID not in tag_dict else tag_dict[RIGHT_ID]["tvec"].tolist(),
            }
        )
    return rows


def normalize_0_100(widths: List[Optional[float]]) -> List[Optional[float]]:
    valid = [w for w in widths if w is not None]
    if len(valid) < 2:
        return [None if w is None else 0.0 for w in widths]
    closed = min(valid)
    open_ = max(valid)
    if abs(open_ - closed) < 1e-9:
        return [0.0 if w is not None else None for w in widths]
    out = []
    for w in widths:
        if w is None:
            out.append(None)
        else:
            out.append(max(0.0, min(100.0, (w - closed) / (open_ - closed) * 100.0)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "ultrawide_samples" / "2026-06-15" / "jpg",
    )
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    images = sorted([p for p in args.input_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    rows = summarize(images)
    visible_z_values = [r["mean_visible_z_m"] for r in rows if r["mean_visible_z_m"] is not None]
    nominal_z = None if not visible_z_values else float(np.median(visible_z_values))
    for r in rows:
        tag_dict = {}
        if r["left_tvec"] is not None:
            tag_dict[LEFT_ID] = {"tvec": np.asarray(r["left_tvec"], dtype=np.float64)}
        if r["right_tvec"] is not None:
            tag_dict[RIGHT_ID] = {"tvec": np.asarray(r["right_tvec"], dtype=np.float64)}
        r["width_filtered_m"] = get_gripper_width(tag_dict, nominal_z=nominal_z)
    normalized = normalize_0_100([r["width_raw_m"] for r in rows])
    for r, n in zip(rows, normalized):
        r["score_0_100"] = n
        r["nominal_z_used_m"] = nominal_z

    print(json.dumps(rows, ensure_ascii=False, indent=2))

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
