#!/usr/bin/env python3
"""Shared run-directory helpers for module 04.

The capture layer should faithfully write raw device outputs.  Later scripts
normalize, align, and export from this run directory without mutating raw data.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl


MODULE_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_DIR.parents[1]
DEFAULT_RUNS_ROOT = REPO_ROOT / "runs"

RUN_MANIFEST_NAME = "RUN_MANIFEST.json"
SYNC_LOG_NAME = "SYNC_LOG.jsonl"
SCHEMA_VERSION = 1

_MANIFEST_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    raw_dir: Path
    normalized_dir: Path
    aligned_dir: Path
    exports_dir: Path
    logs_dir: Path
    manifest_json: Path
    sync_log_jsonl: Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("_")


def default_run_id(task_name: str = "") -> str:
    suffix = slugify(task_name)
    base = f"run_{timestamp_id()}"
    return f"{base}_{suffix}" if suffix else base


def json_line(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + os.linesep


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + os.linesep, encoding="utf-8")


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + os.linesep)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


@contextmanager
def manifest_lock(manifest_path: Path):
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _MANIFEST_THREAD_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def run_paths(run_dir: Path) -> RunPaths:
    run_dir = run_dir.expanduser().resolve()
    return RunPaths(
        run_dir=run_dir,
        raw_dir=run_dir / "raw",
        normalized_dir=run_dir / "normalized",
        aligned_dir=run_dir / "aligned",
        exports_dir=run_dir / "exports",
        logs_dir=run_dir / "logs",
        manifest_json=run_dir / RUN_MANIFEST_NAME,
        sync_log_jsonl=run_dir / "logs" / SYNC_LOG_NAME,
    )


def ensure_run_layout(run_dir: Path, *, create_manifest: bool = True) -> RunPaths:
    paths = run_paths(run_dir)
    for directory in (
        paths.run_dir,
        paths.raw_dir,
        paths.raw_dir / "iphone_stream",
        paths.raw_dir / "global_camera",
        paths.raw_dir / "coinft",
        paths.normalized_dir,
        paths.aligned_dir,
        paths.exports_dir,
        paths.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if create_manifest and not paths.manifest_json.exists():
        with manifest_lock(paths.manifest_json):
            if not paths.manifest_json.exists():
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "row_type": "run_manifest",
                    "run_id": paths.run_dir.name,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "timebase": {
                        "primary": "mac_monotonic_ns",
                        "meaning": "Mac time.monotonic_ns() timeline used for cross-device alignment.",
                    },
                    "layout": {
                        "raw": "raw/",
                        "normalized": "normalized/",
                        "aligned": "aligned/",
                        "exports": "exports/",
                        "logs": "logs/",
                    },
                    "streams": {},
                    "artifacts": {},
                }
                write_json_atomic(paths.manifest_json, manifest)

    if not paths.sync_log_jsonl.exists():
        paths.sync_log_jsonl.write_text("", encoding="utf-8")
    return paths


def create_run(
    *,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    run_id: str | None = None,
    task_name: str = "",
    operator: str = "",
    notes: str = "",
    force: bool = False,
) -> RunPaths:
    run_id = run_id or default_run_id(task_name)
    paths = run_paths(runs_root.expanduser().resolve() / run_id)
    if paths.run_dir.exists() and any(paths.run_dir.iterdir()) and not force:
        raise FileExistsError(f"Run directory already exists and is not empty: {paths.run_dir}")

    ensure_run_layout(paths.run_dir)
    update_manifest(
        paths.run_dir,
        {
            "schema_version": SCHEMA_VERSION,
            "row_type": "run_manifest",
            "run_id": paths.run_dir.name,
            "task_name": task_name,
            "operator": operator,
            "notes": notes,
            "updated_at": utc_now_iso(),
        }
    )
    append_sync_log(
        paths.run_dir,
        {
            "event": "run_created",
            "run_id": paths.run_dir.name,
            "task_name": task_name,
            "operator": operator,
            "notes": notes,
        },
    )
    return paths


def append_sync_log(run_dir: Path, payload: dict[str, Any]) -> None:
    paths = ensure_run_layout(run_dir)
    row = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now_iso(),
        **payload,
    }
    with paths.sync_log_jsonl.open("a", encoding="utf-8") as handle:
        handle.write(json_line(row))


def update_manifest(run_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    paths = ensure_run_layout(run_dir)
    with manifest_lock(paths.manifest_json):
        manifest = read_json(paths.manifest_json, {})
        deep_update(manifest, updates)
        manifest["updated_at"] = utc_now_iso()
        write_json_atomic(paths.manifest_json, manifest)
        return manifest


def deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value


def register_artifact(
    run_dir: Path,
    *,
    stream: str,
    role: str,
    path: Path,
    metadata: dict[str, Any] | None = None,
) -> None:
    paths = ensure_run_layout(run_dir)
    artifact = {
        "path": relpath(path, paths.run_dir),
        "updated_at": utc_now_iso(),
    }
    if metadata:
        artifact.update(metadata)
    update_manifest(
        paths.run_dir,
        {
            "streams": {
                stream: {
                    "updated_at": utc_now_iso(),
                    "artifacts": {
                        role: artifact,
                    },
                }
            }
        },
    )
    append_sync_log(
        paths.run_dir,
        {
            "event": "artifact_registered",
            "stream": stream,
            "role": role,
            "path": artifact["path"],
            "metadata": metadata or {},
        },
    )
