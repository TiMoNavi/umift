#!/usr/bin/env python3
"""Multi-stream Episode alignment CLI."""

from _bootstrap import add_module_root_to_path

add_module_root_to_path()

from umift_laptop_alignment.pipeline.alignment.runner import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
