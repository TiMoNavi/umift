"""Episode mapping utilities for ForceFlow exports."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt


def episode_ids_from_rows(rows: list[dict[str, Any]]) -> npt.NDArray[np.uint16]:
    episode = np.zeros((len(rows),), dtype=np.uint16)
    for index, row in enumerate(rows):
        value = row.get("episode_index")
        if isinstance(value, int) and 0 <= value < 65535:
            episode[index] = value
    return episode


def episode_ends_from_ids(episode: npt.NDArray[np.integer[Any]]) -> npt.NDArray[np.uint32]:
    episode_ids = np.asarray(episode)
    if episode_ids.ndim != 1:
        raise ValueError(f"episode must have shape (N,), got {episode_ids.shape}")
    if episode_ids.shape[0] == 0:
        return np.asarray([], dtype=np.uint32)

    transitions = np.flatnonzero(episode_ids[1:] != episode_ids[:-1]) + 1
    ends = np.concatenate([transitions, np.asarray([episode_ids.shape[0]])])
    return ends.astype(np.uint32)
