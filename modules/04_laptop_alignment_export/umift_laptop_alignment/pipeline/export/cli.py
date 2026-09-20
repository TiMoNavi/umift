"""CLI entrypoint for Episode-level incremental exporters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from umift_laptop_alignment.pipeline.export.config import load_export_config
from umift_laptop_alignment.pipeline.export.registry import build_default_registry


def main() -> int:
    registry = build_default_registry()
    parser = argparse.ArgumentParser(description="Incrementally export one completed UMIFT Episode.")
    parser.add_argument("run_dir", type=Path, nargs="?")
    parser.add_argument("episode_index", type=int, nargs="?")
    parser.add_argument("--format", choices=registry.names(), default="umift-replay-buffer-zarr")
    parser.add_argument("--config", type=Path, help="Optional exporter config JSON/YAML file.")
    parser.add_argument("--list-formats", action="store_true")
    args = parser.parse_args()

    if args.list_formats:
        for exporter in registry.plugins():
            print(f"{exporter.name}\t{exporter.description}")
        return 0
    if args.run_dir is None or args.episode_index is None:
        raise SystemExit("run_dir and episode_index are required unless --list-formats is used.")

    exporter = registry.get(args.format)
    incremental_export = getattr(exporter, "export_episode", None)
    if not callable(incremental_export):
        raise SystemExit(f"Exporter {args.format!r} does not support Episode-level incremental export.")
    manifest = incremental_export(
        args.run_dir.expanduser().resolve(),
        args.episode_index,
        config=load_export_config(args.config),
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
