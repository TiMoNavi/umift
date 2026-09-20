"""Small exporter plugin interface for run-level outputs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


ExportConfig = dict[str, Any]
ExportManifest = dict[str, Any]


class ExporterPlugin(ABC):
    """Format-specific exporter.

    Exporters own output schema details. The recording pipeline should only
    select an exporter by name and pass a run directory plus config.
    """

    name: str
    description: str
    version: str = "0"

    def default_config(self) -> ExportConfig:
        return {}

    def required_inputs(self) -> list[str]:
        return []

    @abstractmethod
    def export(self, run_dir: Path, *, config: ExportConfig | None = None) -> ExportManifest:
        raise NotImplementedError

    def validate(self, output_dir: Path, *, config: ExportConfig | None = None) -> ExportManifest:
        return {
            "format": self.name,
            "valid": True,
            "output_dir": str(output_dir),
        }


def merged_config(defaults: ExportConfig, overrides: ExportConfig | None) -> ExportConfig:
    """Merge nested dict config values without mutating either input."""

    result: ExportConfig = dict(defaults)
    if not overrides:
        return result
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged_config(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result
