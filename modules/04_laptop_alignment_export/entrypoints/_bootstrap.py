"""Entrypoint bootstrap helpers."""

from __future__ import annotations

import sys
from pathlib import Path


def add_module_root_to_path() -> Path:
    module_root = Path(__file__).resolve().parents[1]
    module_root_str = str(module_root)
    if module_root_str not in sys.path:
        sys.path.insert(0, module_root_str)
    return module_root
