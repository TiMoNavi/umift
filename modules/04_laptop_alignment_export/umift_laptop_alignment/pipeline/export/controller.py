"""Small stateful controller for selecting and running exporter plugins."""

from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any

from umift_laptop_alignment.pipeline.export.base import merged_config
from umift_laptop_alignment.pipeline.export.config import load_export_config
from umift_laptop_alignment.pipeline.export.registry import ExporterRegistry, build_default_registry


DEFAULT_EXPORT_FORMAT = "umift-replay-buffer-zarr"
DEFAULT_PROFILE_RELATIVE = Path("config/export_profiles/umift_replay_buffer_v0.json")


class ExportController:
    """Owns export format/profile selection for the main capture pipeline.

    The GUI should only update this controller's state and trigger ``export``.
    Schema-specific decisions stay inside exporter plugins and profile files.
    """

    def __init__(self, module_root: Path, registry: ExporterRegistry | None = None) -> None:
        self.module_root = module_root.expanduser().resolve()
        self.registry = registry or build_default_registry()
        self.profiles_dir = self.module_root / "config" / "export_profiles"
        self.lock = threading.RLock()
        self.selected_format = self.default_format()
        self.selected_profile: Path | None = self.default_profile()
        profile_format = self.profile_format(self.selected_profile)
        if profile_format and profile_format in self.registry.names():
            self.selected_format = profile_format
        self.status = "idle"
        self.last_manifest: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.updated_unix_ns = time.time_ns()

    def default_format(self) -> str:
        return DEFAULT_EXPORT_FORMAT if DEFAULT_EXPORT_FORMAT in self.registry.names() else self.registry.names()[0]

    def default_profile(self) -> Path | None:
        default_path = self.module_root / DEFAULT_PROFILE_RELATIVE
        return default_path if default_path.exists() else None

    def relpath(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return str(path.resolve().relative_to(self.module_root))
        except ValueError:
            return str(path)

    def resolve_profile_path(self, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.module_root / path
        return path.resolve()

    def read_profile_metadata(self, path: Path) -> dict[str, Any]:
        try:
            data = load_export_config(path)
        except OSError as exc:
            return {"name": path.stem, "path": self.relpath(path), "error": str(exc)}
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - metadata should not break discovery.
            return {"name": path.stem, "path": self.relpath(path), "error": str(exc)}
        export = data.get("export", {}) if isinstance(data.get("export"), dict) else {}
        return {
            "name": str(export.get("name") or path.stem),
            "path": self.relpath(path),
            "format": export.get("format"),
            "output_name": export.get("output_name"),
        }

    def profile_format(self, path: Path | None) -> str | None:
        if path is None or not path.exists():
            return None
        metadata = self.read_profile_metadata(path)
        value = metadata.get("format")
        return str(value) if value else None

    def available_formats(self) -> list[dict[str, Any]]:
        return [
            {
                "name": exporter.name,
                "description": exporter.description,
                "version": exporter.version,
                "required_inputs": exporter.required_inputs(),
            }
            for exporter in self.registry.plugins()
        ]

    def available_profiles(self) -> list[dict[str, Any]]:
        if not self.profiles_dir.exists():
            return []
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.profiles_dir.glob("*")):
            if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
                continue
            profiles.append(self.read_profile_metadata(path))
        return profiles

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "selected_format": self.selected_format,
                "selected_profile": self.relpath(self.selected_profile),
                "available_formats": self.available_formats(),
                "available_profiles": self.available_profiles(),
                "profiles_dir": self.relpath(self.profiles_dir),
                "status": self.status,
                "last_manifest": copy.deepcopy(self.last_manifest),
                "last_error": self.last_error,
                "updated_unix_ns": self.updated_unix_ns,
            }

    def configure(self, *, format_name: str | None = None, profile: str | Path | None = None) -> dict[str, Any]:
        with self.lock:
            if format_name is not None:
                format_name = str(format_name).strip()
                if format_name:
                    self.registry.get(format_name)
                    self.selected_format = format_name
                    current_profile_format = self.profile_format(self.selected_profile)
                    if current_profile_format and current_profile_format != self.selected_format:
                        self.selected_profile = None
                    if self.selected_profile is None and self.selected_format == DEFAULT_EXPORT_FORMAT:
                        default_profile = self.default_profile()
                        if self.profile_format(default_profile) == self.selected_format:
                            self.selected_profile = default_profile
            if profile is not None:
                profile_path = self.resolve_profile_path(profile)
                if profile_path is not None and not profile_path.exists():
                    raise FileNotFoundError(f"Export profile does not exist: {profile_path}")
                profile_format = self.profile_format(profile_path)
                if profile_format:
                    self.registry.get(profile_format)
                    if format_name and str(format_name).strip() and profile_format != self.selected_format:
                        raise ValueError(
                            f"Export profile format {profile_format!r} does not match selected format {self.selected_format!r}"
                        )
                    self.selected_format = profile_format
                self.selected_profile = profile_path
            self.status = "configured"
            self.last_error = None
            self.updated_unix_ns = time.time_ns()
            return self.snapshot()

    def mark_exporting(self) -> dict[str, Any]:
        with self.lock:
            self.status = "exporting"
            self.last_error = None
            self.updated_unix_ns = time.time_ns()
            return self.snapshot()

    def export_episode(
        self,
        run_dir: Path,
        source_episode_index: int,
        *,
        format_name: str | None = None,
        profile: str | Path | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.configure(format_name=format_name, profile=profile)
            selected_format = self.selected_format
            selected_profile = self.selected_profile

        with self.lock:
            self.status = "exporting"
            self.last_error = None
            self.updated_unix_ns = time.time_ns()
        try:
            config = load_export_config(selected_profile)
            config = merged_config(config, config_overrides or {})
            exporter = self.registry.get(selected_format)
            incremental_export = getattr(exporter, "export_episode", None)
            if not callable(incremental_export):
                raise SystemExit(
                    f"Exporter {selected_format!r} does not support Episode-level incremental export."
                )
            manifest = incremental_export(
                run_dir.expanduser().resolve(),
                source_episode_index,
                config=config,
            )
        except (Exception, SystemExit) as exc:
            with self.lock:
                self.status = "error"
                self.last_error = str(exc)
                self.last_manifest = None
                self.updated_unix_ns = time.time_ns()
            raise
        with self.lock:
            self.status = "ok"
            self.last_error = None
            self.last_manifest = copy.deepcopy(manifest)
            self.updated_unix_ns = time.time_ns()
        return manifest
