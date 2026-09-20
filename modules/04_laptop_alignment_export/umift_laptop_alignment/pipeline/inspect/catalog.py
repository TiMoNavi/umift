"""Schema-driven catalog and playback access for Zarr datasets."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from umift_laptop_alignment.orchestration.run_layout import (
    append_sync_log,
    ensure_run_layout,
    manifest_lock,
    read_json,
    utc_now_iso,
    write_json_atomic,
)
from umift_laptop_alignment.orchestration.datasets import (
    dataset_summary,
    list_datasets as list_run_datasets,
)
from umift_laptop_alignment.pipeline.episode_exclusions import exclude_episode
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import UmiFTReplayBufferExporter

def deletion_blockers(capture_payload: dict[str, Any], run_dir: Path) -> list[str]:
    state = capture_payload.get("state", {}) if isinstance(capture_payload, dict) else {}
    if not isinstance(state, dict):
        return ["capture state response is invalid"]
    selection = state.get("dataset_selection", {}) if isinstance(state.get("dataset_selection"), dict) else {}
    active_run_text = selection.get("run_dir") or state.get("run_dir")
    if not isinstance(active_run_text, str) or not active_run_text:
        return []
    try:
        active_run = Path(active_run_text).expanduser().resolve()
    except OSError:
        return ["capture service reports an invalid run directory"]
    if active_run != run_dir.expanduser().resolve():
        return []

    blockers: list[str] = []
    recording = state.get("recording_control", {}) if isinstance(state.get("recording_control"), dict) else {}
    postprocess = state.get("postprocess", {}) if isinstance(state.get("postprocess"), dict) else {}
    export = state.get("export_control", {}) if isinstance(state.get("export_control"), dict) else {}
    d435 = state.get("d435", {}) if isinstance(state.get("d435"), dict) else {}
    d435_recording = state.get("d435_recording", {}) if isinstance(state.get("d435_recording"), dict) else {}
    coinft = state.get("coinft", {}) if isinstance(state.get("coinft"), dict) else {}
    if state.get("running"):
        blockers.append("iPhone receiver is running")
    if recording.get("active"):
        blockers.append("an Episode is actively recording")
    if d435.get("running") or d435_recording.get("running"):
        blockers.append("D435 capture is running")
    if coinft.get("running"):
        blockers.append("CoinFT capture is running")
    if postprocess.get("status") == "running":
        blockers.append("postprocess is running")
    if export.get("status") == "exporting":
        blockers.append("Zarr export is running")
    return list(dict.fromkeys(blockers))


class ZarrCatalog:
    def __init__(self, roots: list[Path]):
        self.roots = [root.expanduser().resolve() for root in roots]

    def datasets(self) -> list[dict[str, Any]]:
        result = []
        for root in self.roots:
            if not root.exists():
                continue
            candidates = [root] if root.name.endswith(".zarr") else sorted(root.rglob("*.zarr"))
            for path in candidates:
                if not (path / ".zgroup").exists() and not (path / "zarr.json").exists():
                    continue
                result.append(self.describe(path))
        return sorted(result, key=lambda item: item["modified_ns"], reverse=True)

    def run_datasets(self) -> list[dict[str, Any]]:
        by_run: dict[str, dict[str, Any]] = {}
        for root in self.roots:
            if root.name.endswith(".zarr"):
                run_dir = self._run_dir(root)
                if run_dir is not None:
                    by_run[str(run_dir)] = dataset_summary(run_dir)
                continue
            for row in list_run_datasets(root):
                by_run[str(row["run_dir"])] = row
        return sorted(
            by_run.values(),
            key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
            reverse=True,
        )

    def resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser().resolve()
        if not any(candidate == root or root in candidate.parents for root in self.roots):
            raise ValueError("dataset is outside configured inspector roots")
        if candidate.suffix != ".zarr":
            raise ValueError("dataset path must end with .zarr")
        return candidate

    def resolve_run(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve()
        if not any(candidate == root or root in candidate.parents for root in self.roots):
            raise ValueError("run is outside configured inspector roots")
        if not (candidate / "RUN_MANIFEST.json").is_file():
            raise ValueError("run directory is missing RUN_MANIFEST.json")
        return candidate

    def run_for_dataset(self, path: str | Path) -> Path:
        dataset = self.resolve(str(path))
        run_dir = self._run_dir(dataset)
        if run_dir is None:
            raise ValueError("dataset is not inside a recognized run directory")
        return run_dir

    def _open(self, path: Path):
        import zarr
        return zarr.open(str(path), mode="r")

    @staticmethod
    def _episode_index(name: str) -> int | None:
        try:
            return int(name.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            return None

    def _run_dir(self, dataset: Path) -> Path | None:
        for parent in dataset.parents:
            if (parent / "RUN_MANIFEST.json").exists() and (parent / "raw").is_dir():
                return parent
        return None

    def _episode_context(self, dataset: Path) -> dict[int, dict[str, Any]]:
        run_dir = self._run_dir(dataset)
        if run_dir is None:
            return {}

        context: dict[int, dict[str, Any]] = {}
        export_manifest = read_json(dataset / "UMIFT_EXPORT_MANIFEST.json", {})
        for row in export_manifest.get("episodes", []):
            if not isinstance(row, dict):
                continue
            logical_index = row.get("episode_index")
            source_index = row.get("source_episode_index")
            if not isinstance(logical_index, int):
                continue
            existing = context.setdefault(logical_index, {"zarr_episode_index": logical_index})
            if isinstance(source_index, int):
                existing["source_episode_index"] = source_index
            session_id = row.get("iphone_session_id")
            if isinstance(session_id, str):
                existing["session_id"] = session_id
            for key in ("start_aligned_monotonic_ns", "end_aligned_monotonic_ns"):
                if isinstance(row.get(key), int):
                    existing[key] = row[key]
        return context

    def _array_info(self, name: str, array: Any) -> dict[str, Any]:
        return {
            "name": name,
            "kind": "array",
            "shape": list(array.shape),
            "dtype": str(array.dtype),
            "chunks": list(array.chunks) if getattr(array, "chunks", None) else None,
            "attrs": dict(array.attrs),
        }

    def _tree(self, group: Any, prefix: str = "") -> list[dict[str, Any]]:
        entries = []
        for name in sorted(group.array_keys()):
            entries.append(self._array_info(f"{prefix}{name}", group[name]))
        for name in sorted(group.group_keys()):
            entries.append({
                "name": f"{prefix}{name}",
                "kind": "group",
                "children": self._tree(group[name], f"{prefix}{name}/"),
                "attrs": dict(group[name].attrs),
            })
        return entries

    def describe(self, path: Path) -> dict[str, Any]:
        root = self._open(path)
        episodes = []
        episode_context = self._episode_context(path)
        data = root.get("data")
        if data is not None:
            for name in sorted(data.group_keys(), key=lambda value: self._episode_index(value) or 0):
                episode = data[name]
                arrays = {key: self._array_info(key, episode[key]) for key in episode.array_keys()}
                lengths = {key: info["shape"][0] for key, info in arrays.items() if info["shape"]}
                quality = {
                    "status": "ok" if lengths else "unknown",
                    "lengths": lengths,
                    "length_mismatches": sorted({value for value in lengths.values()}) if lengths else [],
                }
                if len(set(lengths.values())) > 1:
                    quality["status"] = "review"
                logical_index = self._episode_index(name)
                context = dict(episode_context.get(logical_index, {})) if isinstance(logical_index, int) else {}
                attrs = dict(episode.attrs)
                context.setdefault("zarr_episode_index", attrs.get("zarr_episode_index", logical_index))
                context.setdefault("source_episode_index", attrs.get("source_episode_index"))
                context.setdefault("session_id", attrs.get("iphone_session_id"))
                context.setdefault("start_aligned_monotonic_ns", attrs.get("start_aligned_monotonic_ns"))
                context.setdefault("end_aligned_monotonic_ns", attrs.get("end_aligned_monotonic_ns"))
                source_index = context.get("source_episode_index")
                display_name = name
                if isinstance(source_index, int):
                    display_name = f"{name} | source episode_{source_index:06d}"
                episodes.append({
                    "name": name,
                    "display_name": display_name,
                    "arrays": arrays,
                    "quality": quality,
                    **context,
                })
        run_dir = self._run_dir(path)
        return {
            "id": str(path),
            "path": str(path),
            "relative_path": str(path),
            "run_dir": str(run_dir) if run_dir else None,
            "run_id": run_dir.name if run_dir else None,
            "modified_ns": path.stat().st_mtime_ns,
            "format": "zarr",
            "attrs": dict(root.attrs),
            "episodes": episodes,
            "tree": self._tree(root),
        }

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def _remove_run_paths(self, run_dir: Path, paths: list[Path]) -> list[str]:
        removed: list[str] = []
        for path in paths:
            candidate = path.expanduser().resolve()
            if candidate == run_dir or run_dir not in candidate.parents:
                raise ValueError(f"refusing to delete path outside run: {candidate}")
            if not candidate.exists() and not candidate.is_symlink():
                continue
            removed.append(str(candidate.relative_to(run_dir)))
            self._remove_path(candidate)
        return removed

    @staticmethod
    def _artifact_was_removed(path: str, removed: list[str]) -> bool:
        return any(path == item or path.startswith(item + "/") for item in removed)

    def _clean_manifest_artifacts(
        self,
        run_dir: Path,
        *,
        removed: list[str],
        postprocess: dict[str, Any],
    ) -> None:
        manifest_path = run_dir / "RUN_MANIFEST.json"
        with manifest_lock(manifest_path):
            manifest = read_json(manifest_path, {})
            streams = manifest.get("streams", {})
            if not isinstance(streams, dict):
                streams = {}
                manifest["streams"] = streams
            for stream_name, stream in list(streams.items()):
                if not isinstance(stream, dict):
                    continue
                artifacts = stream.get("artifacts")
                if isinstance(artifacts, dict):
                    for role, artifact in list(artifacts.items()):
                        artifact_path = artifact.get("path") if isinstance(artifact, dict) else None
                        if isinstance(artifact_path, str) and self._artifact_was_removed(artifact_path, removed):
                            artifacts.pop(role, None)
                    if not artifacts:
                        stream.pop("artifacts", None)
                if stream_name in {"normalization", "alignment", "export"} and "artifacts" not in stream:
                    streams.pop(stream_name, None)
            manifest["postprocess"] = postprocess
            manifest["updated_at"] = utc_now_iso()
            write_json_atomic(manifest_path, manifest)

    def _episode_raw_paths(
        self,
        run_dir: Path,
        source_index: int,
        session_id: str,
        *,
        remove_iphone_session: bool,
    ) -> list[Path]:
        iphone_root = (run_dir / "raw" / "iphone_stream").resolve()
        iphone_session = (iphone_root / session_id).resolve()
        if iphone_session.parent != iphone_root:
            raise ValueError("invalid iPhone session path")
        episode_dir = f"episode_{source_index:06d}"
        paths = [
            run_dir / "raw" / "global_camera" / episode_dir,
            run_dir / "raw" / "coinft" / episode_dir,
            run_dir / "pipeline" / "live_zarr_rows" / episode_dir,
            run_dir / "normalized" / "episodes" / episode_dir,
            run_dir / "aligned" / "episodes" / episode_dir,
        ]
        if remove_iphone_session:
            paths.append(iphone_session)
        return paths

    def _remove_export_bundle(self, run_dir: Path, dataset: Path) -> list[str]:
        export_bundle = dataset.parent
        if export_bundle.parent == run_dir / "exports":
            return self._remove_run_paths(run_dir, [export_bundle])
        return self._remove_run_paths(run_dir, [dataset])

    def delete_episode(self, path: str, episode_name: str) -> dict[str, Any]:
        dataset = self.resolve(path)
        if not dataset.exists():
            raise FileNotFoundError(f"dataset does not exist: {dataset}")
        run_dir = self._run_dir(dataset)
        if run_dir is None:
            raise ValueError("dataset is not inside a recognized run directory")
        description = self.describe(dataset)
        episode = next((row for row in description["episodes"] if row.get("name") == episode_name), None)
        if episode is None:
            raise KeyError(f"unknown episode {episode_name}")
        logical_index = episode.get("zarr_episode_index")
        if not isinstance(logical_index, int):
            logical_index = self._episode_index(episode_name)
        source_index = episode.get("source_episode_index")
        session_id = episode.get("session_id")
        if not isinstance(source_index, int):
            raise ValueError("cannot safely delete raw data: source episode mapping is missing")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("cannot safely delete raw data: iPhone session mapping is missing")
        exclusion = exclude_episode(
            run_dir,
            source_episode_index=source_index,
            session_id=session_id,
            zarr_episode_index=logical_index,
            dataset=str(dataset),
        )
        retained_episodes = [row for row in description["episodes"] if row is not episode]
        remove_iphone_session = not any(row.get("session_id") == session_id for row in retained_episodes)
        if retained_episodes:
            if not isinstance(logical_index, int):
                raise ValueError("cannot delete Episode without a logical Zarr index")
            manifest = UmiFTReplayBufferExporter().delete_episode(run_dir, dataset, logical_index)
        else:
            removed_export = self._remove_export_bundle(run_dir, dataset)
            manifest = {"status": "empty", "episode_count": 0}
        removed = self._remove_run_paths(
            run_dir,
            self._episode_raw_paths(
                run_dir,
                source_index,
                session_id,
                remove_iphone_session=remove_iphone_session,
            ),
        )
        if len(description["episodes"]) <= 1:
            removed = removed_export + removed
        deleted_at = utc_now_iso()
        self._clean_manifest_artifacts(
            run_dir,
            removed=removed,
            postprocess={
                "status": "ok" if len(description["episodes"]) > 1 else "idle",
                "stage": "episode_deleted",
                "deleted_at": deleted_at,
                "deleted_by": "zarr_inspector",
                "source_episode_index": source_index,
                "session_id": session_id,
                "remaining_episode_count": max(0, len(description["episodes"]) - 1),
                "raw_deleted": True,
                "iphone_raw_preserved_shared_session": not remove_iphone_session,
            },
        )
        append_sync_log(run_dir, {
            "event": "episode_deleted",
            "source": "zarr_inspector",
            "dataset": str(dataset),
            "episode": episode_name,
            "exclusion": exclusion,
            "removed": removed,
            "raw_deleted": True,
            "iphone_raw_preserved_shared_session": not remove_iphone_session,
        })
        return {
            "status": "ok",
            "run_dir": str(run_dir),
            "excluded": exclusion,
            "removed": removed,
            "export": manifest,
            "raw_deleted": True,
            "iphone_raw_preserved_shared_session": not remove_iphone_session,
            "dataset_preserved": str(run_dir),
        }

    def delete_zarr(self, path: str = "", *, run_dir: str | Path | None = None) -> dict[str, Any]:
        dataset = self.resolve(path) if path else None
        resolved_run = self.resolve_run(run_dir) if run_dir is not None else None
        if dataset is not None:
            if not dataset.exists() and resolved_run is None:
                raise FileNotFoundError(f"dataset does not exist: {dataset}")
            dataset_run = self._run_dir(dataset)
            if dataset_run is None:
                raise ValueError("dataset is not inside a recognized run directory")
            if resolved_run is not None and resolved_run != dataset_run:
                raise ValueError("dataset and run_dir do not match")
            resolved_run = dataset_run
        if resolved_run is None:
            raise ValueError("delete_zarr requires a dataset path or run_dir")
        run_dir_path = resolved_run
        removed = self._remove_run_paths(
            run_dir_path,
            [
                run_dir_path / "raw",
                run_dir_path / "normalized",
                run_dir_path / "aligned",
                run_dir_path / "pipeline",
                run_dir_path / "quality",
                run_dir_path / "exports",
            ],
        )
        ensure_run_layout(run_dir_path)
        manifest_path = run_dir_path / "RUN_MANIFEST.json"
        deleted_at = utc_now_iso()
        with manifest_lock(manifest_path):
            manifest = read_json(manifest_path, {})
            coinft = manifest.get("streams", {}).get("coinft", {}) if isinstance(manifest.get("streams"), dict) else {}
            calibration = coinft.get("calibration") if isinstance(coinft, dict) else None
            manifest["streams"] = {"coinft": {"calibration": calibration}} if isinstance(calibration, dict) else {}
            manifest["artifacts"] = {}
            manifest["postprocess"] = {
                "status": "idle",
                "stage": None,
                "last_operation": "dataset_data_cleared",
                "cleared_at": deleted_at,
                "cleared_by": "zarr_inspector",
                "removed": removed,
            }
            manifest["updated_at"] = deleted_at
            write_json_atomic(manifest_path, manifest)
        append_sync_log(run_dir_path, {
            "event": "dataset_data_cleared",
            "source": "zarr_inspector",
            "dataset": str(dataset) if dataset is not None else None,
            "removed": removed,
            "dataset_preserved": True,
            "next_episode_index": 0,
        })
        return {
            "status": "ok",
            "run_dir": str(run_dir_path),
            "removed": removed,
            "dataset_preserved": str(run_dir_path),
            "raw_deleted": True,
            "next_episode_index": 0,
        }

    def episode(self, path: str, episode: str) -> dict[str, Any]:
        dataset = self.resolve(path)
        description = self.describe(dataset)
        match = next((item for item in description["episodes"] if item["name"] == episode), None)
        if match is None:
            raise KeyError(f"unknown episode {episode}")
        return {"dataset": description, "episode": match}

    def series(self, path: str, episode: str, field: str, start: int = 0, count: int = 2000) -> dict[str, Any]:
        dataset = self.resolve(path)
        root = self._open(dataset)
        array = root["data"][episode][field]
        if len(array.shape) == 0:
            values = array[...].tolist()
        else:
            end = min(array.shape[0], max(start, 0) + max(count, 1))
            values = array[max(start, 0):end].tolist()
        return {"field": field, "shape": list(array.shape), "dtype": str(array.dtype), "start": start, "values": values}

    def frame(self, path: str, episode: str, field: str, index: int) -> tuple[bytes, str]:
        import cv2
        import numpy as np
        dataset = self.resolve(path)
        root = self._open(dataset)
        value = np.asarray(root["data"][episode][field][max(0, index)])
        if value.ndim == 3 and value.shape[-1] == 3:
            if np.issubdtype(value.dtype, np.floating):
                depth = np.nan_to_num(value[..., 0].astype(np.float32), nan=0.0)
                normalized = cv2.normalize(depth, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
                image = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
            else:
                image = cv2.cvtColor(value.astype(np.uint8), cv2.COLOR_RGB2BGR)
        elif value.ndim == 2:
            normalized = cv2.normalize(value.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
            image = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
        else:
            raise ValueError(f"field {field} is not an image array")
        ok, encoded = cv2.imencode(".jpg", image)
        if not ok:
            raise RuntimeError("failed to encode image")
        return encoded.tobytes(), "image/jpeg"
