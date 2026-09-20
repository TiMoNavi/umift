"""Camera timestamp helpers for UMI-FT replay-buffer export."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import numeric


def d435_seconds_from_start(row: dict[str, Any], start_ns: int) -> float:
    aligned_ns = row.get("aligned_monotonic_ns")
    if not isinstance(aligned_ns, int):
        return 0.0
    d435 = row.get("d435", {}) if isinstance(row.get("d435"), dict) else {}
    delta_ms = numeric(d435.get("source_delta_ms"), 0.0)
    return (aligned_ns + int(delta_ms * 1_000_000.0) - start_ns) / 1_000_000_000.0
