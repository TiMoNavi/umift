"""Recording transition events shared by the main collection chain."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecordingTransition:
    """Canonical recording state transition from the iPhone-controlled timeline."""

    event: str
    episode_index: int
    created_unix_ns: int
    event_aligned_monotonic_ns: int | None = None
    event_aligned_unix_ns: int | None = None
    run_dir: str | None = None
    source: str = "iphone_recording_event"
    reason: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
