"""iPhone raw stream buffers."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.capture.buffers.timestamped_ring import BufferedSample, TimestampedRingBuffer


class IPhoneRawBuffer(TimestampedRingBuffer):
    """Timestamped buffer for raw iPhone frame/event rows."""

    def append_row(self, row: dict[str, Any], *, timestamp_ns: int | None, sequence_id: Any = None) -> bool:
        return self.append(timestamp_ns=timestamp_ns, payload=row, sequence_id=sequence_id)


__all__ = ["BufferedSample", "IPhoneRawBuffer"]
