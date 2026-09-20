#!/usr/bin/env python3
"""Run the authoritative iPhone H.264 Protocol V2 archive receiver."""

from _bootstrap import add_module_root_to_path

add_module_root_to_path()

from umift_laptop_alignment.capture.receivers.iphone.archive_receiver_v2 import main


if __name__ == "__main__":
    raise SystemExit(main())
