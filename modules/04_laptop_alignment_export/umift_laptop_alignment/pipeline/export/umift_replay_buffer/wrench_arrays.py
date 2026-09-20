"""Build CoinFT passthrough and TCP/tool-frame wrench arrays."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import seconds_from_start
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.selection import dual_wrench_from_row
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.wrench import coinft_pair_to_tcp


def build_wrench_arrays(
    *,
    coinft_rows: list[dict[str, Any]],
    start_ns: int,
    time_stamps: Any,
    gripper_widths_m: Any,
    force_config: dict[str, Any],
    np: Any,
) -> dict[str, Any]:
    f_len = len(coinft_rows)
    left = np.zeros((f_len, 6), dtype=np.float64)
    right = np.zeros((f_len, 6), dtype=np.float64)
    left_tcp = np.zeros((f_len, 6), dtype=np.float64)
    right_tcp = np.zeros((f_len, 6), dtype=np.float64)
    wrench_times = np.zeros((f_len, 1), dtype=np.float64)
    gripper_time_axis = time_stamps.reshape(-1)

    for index, row in enumerate(coinft_rows):
        left_wrench, right_wrench = dual_wrench_from_row(row)
        if left_wrench is None or right_wrench is None:
            raise SystemExit("Internal error: selected CoinFT row without dual wrench.")
        left[index] = np.asarray(left_wrench, dtype=np.float64)
        right[index] = np.asarray(right_wrench, dtype=np.float64)
        wrench_times[index, 0] = seconds_from_start(row.get("aligned_monotonic_ns"), start_ns)
        gripper_width = float(np.interp(wrench_times[index, 0], gripper_time_axis, gripper_widths_m))
        left_tcp[index], right_tcp[index] = coinft_pair_to_tcp(
            left[index],
            right[index],
            gripper_width,
            force_config,
            np,
        )

    concat_coinft = np.concatenate([left, right], axis=1)
    concat_tcp = np.concatenate([left_tcp, right_tcp], axis=1)
    return {
        "wrench_left_coinft_0": left.copy(),
        "wrench_right_coinft_0": right.copy(),
        "wrench_concat_coinft_0": concat_coinft.copy(),
        "wrench_left_0": left_tcp.copy(),
        "wrench_right_0": right_tcp.copy(),
        "wrench_concat_0": concat_tcp.copy(),
        "wrench_time_stamps_left_0": wrench_times.copy(),
        "wrench_time_stamps_right_0": wrench_times.copy(),
        "wrench_time_stamps_0": wrench_times.copy(),
    }
