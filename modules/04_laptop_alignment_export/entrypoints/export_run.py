#!/usr/bin/env python3
"""Exporter plugin CLI wrapper."""

from _bootstrap import add_module_root_to_path

add_module_root_to_path()

from umift_laptop_alignment.pipeline.export.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
