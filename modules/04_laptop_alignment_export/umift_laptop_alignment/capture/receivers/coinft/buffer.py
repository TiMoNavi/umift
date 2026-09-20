"""CoinFT/Teensy raw sample buffers."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.capture.buffers.timestamped_ring import BufferedSample, TimestampedRingBuffer


class CoinFTRawBuffer(TimestampedRingBuffer):
    """Timestamped buffer for raw CoinFT/Teensy samples."""

    def append_sample(self, sample: dict[str, Any], *, timestamp_ns: int | None, sequence_id: Any = None) -> bool:
        unix_ns = sample.get("host_receive_unix_ns")
        return self.append(
            timestamp_ns=timestamp_ns,
            unix_ns=unix_ns if isinstance(unix_ns, int) else None,
            sequence_id=sequence_id,
            payload=sample,
        )


__all__ = ["BufferedSample", "CoinFTRawBuffer"]
