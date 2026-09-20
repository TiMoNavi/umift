#!/usr/bin/env python3
"""Compatibility wrapper for umift_laptop_alignment.app.receiver_web_gui."""

from _bootstrap import add_module_root_to_path

add_module_root_to_path()

from umift_laptop_alignment.app.receiver_web_gui import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
