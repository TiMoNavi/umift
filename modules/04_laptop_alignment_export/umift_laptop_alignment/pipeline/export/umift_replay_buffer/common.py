"""Small shared helpers for UMI-FT replay-buffer export."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterator


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def numeric(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return default


def seconds_from_start(value: Any, start_ns: int) -> float:
    if not isinstance(value, int):
        return 0.0
    return (int(value) - start_ns) / 1_000_000_000.0


def cumulative_int64(values: list[int], np: Any) -> Any:
    return np.cumsum(np.asarray(values, dtype=np.int64))
