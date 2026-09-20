#!/usr/bin/env python3
"""Small timestamped ring buffers for live capture pre-roll.

The capture GUI keeps devices streaming continuously, but only writes official
run artifacts after an iPhone record event passes preflight.  This module gives
each live stream the same in-memory staging area so a record_start can include a
small amount of already-received data without making the raw layer format-aware.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


def seconds_to_ns(value: float) -> int:
    return int(max(0.0, float(value)) * 1_000_000_000)


def preroll_start_ns(event_timestamp_ns: int | None, preroll_s: float) -> int | None:
    if event_timestamp_ns is None:
        return None
    return event_timestamp_ns - seconds_to_ns(preroll_s)


@dataclass(frozen=True)
class BufferedSample:
    timestamp_ns: int
    payload: Any
    sequence_id: Any = None
    unix_ns: int | None = None


class TimestampedRingBuffer:
    def __init__(self, *, name: str, max_age_s: float, max_items: int = 0):
        self.name = name
        self.max_age_ns = seconds_to_ns(max_age_s)
        self.max_items = max(0, int(max_items))
        self._samples: deque[BufferedSample] = deque()
        self._total_appended = 0
        self._total_pruned = 0

    def __len__(self) -> int:
        return len(self._samples)

    def append(
        self,
        *,
        timestamp_ns: int | None,
        payload: Any,
        sequence_id: Any = None,
        unix_ns: int | None = None,
    ) -> bool:
        if not isinstance(timestamp_ns, int):
            return False
        self._samples.append(
            BufferedSample(
                timestamp_ns=timestamp_ns,
                payload=payload,
                sequence_id=sequence_id,
                unix_ns=unix_ns,
            )
        )
        self._total_appended += 1
        self.prune(reference_ns=timestamp_ns)
        return True

    def prune(self, *, reference_ns: int | None = None) -> None:
        if not self._samples:
            return
        if reference_ns is None:
            reference_ns = self._samples[-1].timestamp_ns
        if self.max_age_ns > 0:
            min_timestamp_ns = reference_ns - self.max_age_ns
            while self._samples and self._samples[0].timestamp_ns < min_timestamp_ns:
                self._samples.popleft()
                self._total_pruned += 1
        if self.max_items > 0:
            while len(self._samples) > self.max_items:
                self._samples.popleft()
                self._total_pruned += 1

    def slice_window(self, *, start_ns: int | None, end_ns: int | None = None) -> list[BufferedSample]:
        if start_ns is None:
            return []
        return [
            sample
            for sample in self._samples
            if sample.timestamp_ns >= start_ns and (end_ns is None or sample.timestamp_ns <= end_ns)
        ]

    def latest_timestamp_ns(self) -> int | None:
        if not self._samples:
            return None
        return self._samples[-1].timestamp_ns

    def stats(self) -> dict[str, Any]:
        first = self._samples[0].timestamp_ns if self._samples else None
        latest = self.latest_timestamp_ns()
        span_ms = (latest - first) / 1e6 if isinstance(first, int) and isinstance(latest, int) else None
        return {
            "name": self.name,
            "samples": len(self._samples),
            "max_age_s": self.max_age_ns / 1e9,
            "max_items": self.max_items,
            "first_timestamp_ns": first,
            "latest_timestamp_ns": latest,
            "span_ms": span_ms,
            "total_appended": self._total_appended,
            "total_pruned": self._total_pruned,
        }
