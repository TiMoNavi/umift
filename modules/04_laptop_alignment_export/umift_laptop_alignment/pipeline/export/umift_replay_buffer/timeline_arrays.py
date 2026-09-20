"""Build shared episode-local timeline arrays."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import seconds_from_start


def build_time_stamps(
    *,
    episode_rows: list[dict[str, Any]],
    start_ns: int,
    np: Any,
) -> Any:
    time_stamps = np.zeros((len(episode_rows), 1), dtype=np.float64)
    for index, row in enumerate(episode_rows):
        time_stamps[index, 0] = seconds_from_start(row.get("aligned_monotonic_ns"), start_ns)
    return time_stamps
