"""User-facing dataset catalog backed by the existing run directory layout."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from umift_laptop_alignment.orchestration.run_layout import read_json, update_manifest, utc_now_iso


DATASET_METADATA_VERSION = 1


def episode_indices(run_dir: Path) -> list[int]:
    values: set[int] = set()
    for root in (
        run_dir / "raw" / "global_camera",
        run_dir / "raw" / "coinft",
        run_dir / "pipeline" / "live_zarr_rows",
    ):
        if not root.exists():
            continue
        for path in root.glob("episode_*"):
            try:
                values.add(int(path.name.rsplit("_", 1)[-1]))
            except ValueError:
                continue
    return sorted(values)


def zarr_paths(run_dir: Path) -> list[Path]:
    exports = run_dir / "exports"
    if not exports.exists():
        return []
    return sorted(path for path in exports.rglob("*.zarr") if path.is_dir())


def dataset_summary(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest = read_json(run_dir / "RUN_MANIFEST.json", {})
    dataset = manifest.get("dataset", {}) if isinstance(manifest.get("dataset"), dict) else {}
    indices = episode_indices(run_dir)
    zarrs = zarr_paths(run_dir)
    postprocess = manifest.get("postprocess", {}) if isinstance(manifest.get("postprocess"), dict) else {}
    return {
        "id": run_dir.name,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "display_name": str(dataset.get("display_name") or manifest.get("task_name") or run_dir.name),
        "task_name": str(dataset.get("task_name") or manifest.get("task_name") or ""),
        "notes": str(dataset.get("notes") or manifest.get("notes") or ""),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "episode_count": len(indices),
        "latest_source_episode_index": indices[-1] if indices else None,
        "zarr_count": len(zarrs),
        "zarr_paths": [str(path) for path in zarrs],
        "postprocess_status": postprocess.get("status") or "idle",
        "compatibility": dataset.get("compatibility") if isinstance(dataset.get("compatibility"), dict) else None,
        "legacy": not bool(dataset),
    }


def list_datasets(runs_root: Path) -> list[dict[str, Any]]:
    root = runs_root.expanduser().resolve()
    if not root.exists():
        return []
    result = []
    for run_dir in root.iterdir():
        if not run_dir.is_dir() or not (run_dir / "RUN_MANIFEST.json").exists():
            continue
        result.append(dataset_summary(run_dir))
    return sorted(result, key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)


def resolve_dataset_run(runs_root: Path, value: str | Path) -> Path:
    root = runs_root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate.parent != root:
        raise ValueError("dataset must be a direct child of the configured runs root")
    if not candidate.is_dir() or not (candidate / "RUN_MANIFEST.json").exists():
        raise FileNotFoundError(f"dataset run does not exist: {candidate}")
    return candidate


def initialize_dataset_metadata(
    run_dir: Path,
    *,
    display_name: str,
    task_name: str,
    notes: str,
    compatibility: dict[str, Any],
) -> dict[str, Any]:
    metadata = {
        "schema_version": DATASET_METADATA_VERSION,
        "display_name": display_name.strip(),
        "task_name": task_name.strip(),
        "notes": notes.strip(),
        "compatibility": compatibility,
        "created_at": utc_now_iso(),
    }
    update_manifest(
        run_dir,
        {
            "task_name": metadata["task_name"],
            "notes": metadata["notes"],
            "dataset": metadata,
        },
    )
    return metadata


def compatibility_issues(stored: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if not stored:
        return []
    issues = []
    labels = {
        "export_format": "export format",
        "export_profile": "export profile",
        "coinft_mode": "CoinFT mode",
        "coinft_left_hardware": "left CoinFT hardware",
        "coinft_right_hardware": "right CoinFT hardware",
    }
    for key, label in labels.items():
        expected = stored.get(key)
        actual = current.get(key)
        if expected is not None and actual is not None and expected != actual:
            issues.append(f"{label} changed: dataset={expected!r}, current={actual!r}")
    return issues
