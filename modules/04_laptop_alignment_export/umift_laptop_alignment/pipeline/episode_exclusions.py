"""Persistent quality decisions for episodes excluded from derived exports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from umift_laptop_alignment.orchestration.run_layout import read_json, utc_now_iso, write_json


EXCLUSIONS_RELATIVE_PATH = Path("quality") / "EPISODE_EXCLUSIONS.json"


def exclusions_path(run_dir: Path) -> Path:
    return run_dir.expanduser().resolve() / EXCLUSIONS_RELATIVE_PATH


def load_episode_exclusions(run_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(exclusions_path(run_dir), {})
    rows = payload.get("exclusions", []) if isinstance(payload, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def exclude_episode(
    run_dir: Path,
    *,
    source_episode_index: int | None,
    session_id: str | None,
    zarr_episode_index: int | None = None,
    dataset: str | None = None,
) -> dict[str, Any]:
    if not isinstance(source_episode_index, int) and not session_id:
        raise ValueError("episode exclusion requires a source episode index or session id")
    path = exclusions_path(run_dir)
    rows = load_episode_exclusions(run_dir)
    for row in rows:
        if session_id and row.get("session_id") == session_id:
            return row
        if isinstance(source_episode_index, int) and row.get("source_episode_index") == source_episode_index:
            return row
    entry = {
        "source_episode_index": source_episode_index,
        "session_id": session_id,
        "zarr_episode_index_at_exclusion": zarr_episode_index,
        "dataset": dataset,
        "excluded_at": utc_now_iso(),
        "reason": "rejected_in_zarr_inspector",
    }
    rows.append(entry)
    write_json(
        path,
        {
            "schema_version": 1,
            "updated_at": entry["excluded_at"],
            "exclusions": rows,
        },
    )
    return entry


def excluded_session_ids(run_dir: Path) -> set[str]:
    return {
        str(row["session_id"])
        for row in load_episode_exclusions(run_dir)
        if isinstance(row.get("session_id"), str) and row.get("session_id")
    }


def filter_excluded_timeline_rows(run_dir: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded = excluded_session_ids(run_dir)
    if not excluded:
        return rows
    result = []
    for row in rows:
        iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
        if iphone.get("session_id") in excluded:
            continue
        result.append(row)
    return result
