"""Exporter plugin registry."""

from __future__ import annotations

from typing import Iterable

from umift_laptop_alignment.pipeline.export.base import ExporterPlugin


class ExporterRegistry:
    def __init__(self, exporters: Iterable[ExporterPlugin] = ()) -> None:
        self._exporters: dict[str, ExporterPlugin] = {}
        for exporter in exporters:
            self.register(exporter)

    def register(self, exporter: ExporterPlugin) -> None:
        if exporter.name in self._exporters:
            raise ValueError(f"Duplicate exporter plugin: {exporter.name}")
        self._exporters[exporter.name] = exporter

    def get(self, name: str) -> ExporterPlugin:
        try:
            return self._exporters[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._exporters))
            raise KeyError(f"Unknown exporter format {name!r}. Available: {available}") from exc

    def names(self) -> list[str]:
        return sorted(self._exporters)

    def plugins(self) -> list[ExporterPlugin]:
        return [self._exporters[name] for name in self.names()]


def build_default_registry() -> ExporterRegistry:
    from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import UmiFTReplayBufferExporter

    registry = ExporterRegistry()
    registry.register(UmiFTReplayBufferExporter())
    return registry
