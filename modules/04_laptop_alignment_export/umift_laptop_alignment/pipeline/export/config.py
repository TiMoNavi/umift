"""Config loading helpers for exporter plugins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _parse_simple_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_simple_yaml_scalar(part) for part in inner.split(",")]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _load_simple_yaml_mapping(text: str) -> dict[str, Any]:
    """Tiny YAML fallback for local export profiles.

    It intentionally supports only the profile subset we use: nested mappings
    by indentation plus scalar values and inline lists. Install PyYAML for full
    YAML support.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if "\t" in raw_line:
            raise SystemExit(f"YAML export config fallback does not support tabs at line {line_number}.")
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        key, separator, raw_value = stripped.partition(":")
        if not separator or not key.strip():
            raise SystemExit(f"YAML export config fallback expected 'key: value' at line {line_number}.")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise SystemExit(f"YAML export config fallback found invalid indentation at line {line_number}.")
        parent = stack[-1][1]
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            parent[key] = _parse_simple_yaml_scalar(raw_value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def load_export_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    path = path.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            return _load_simple_yaml_mapping(text)
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    raise SystemExit(f"Unsupported export config extension: {path.suffix}")
