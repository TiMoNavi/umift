#!/usr/bin/env python3
"""Find broken and non-portable paths inside a UMIFT deliverable.

Run from the deliverable root:

    python3 tools/validate_deliverable_paths.py

The checker is dependency-free so it can run before the project environment is
installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".h", ".hpp", ".ini", ".json", ".md", ".py",
    ".sh", ".swift", ".txt", ".yaml", ".yml",
}
SKIP_DIRS = {".git", ".pio", "__pycache__", "runs", "third_party"}
PLACEHOLDERS = ("<PATH_TO_DELIVERABLES>", "<original-developer-machine>", "<conda-env>")
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|Volumes)/[^\s\"'`<>]+"
)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PATH_FIELD_RE = re.compile(r"(?:path|file|manifest|onnx|norm|firmware|source)", re.I)
YAML_PATH_LINE_RE = re.compile(
    r"^\s*[A-Za-z0-9_.-]*(?:path|file|manifest|onnx|norm|firmware|calibration|normalization)[A-Za-z0-9_.-]*\s*:\s*['\"]?([^'\"#]+?)\s*['\"]?\s*$",
    re.I,
)
TRAILING_PATH_PUNCTUATION = ".,;:)]}"


@dataclass(frozen=True)
class Finding:
    kind: str
    path: Path
    line: int
    detail: str


def iter_files(root: Path) -> Iterable[Path]:
    for directory, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            name for name in directory_names if name not in SKIP_DIRS
        )
        for name in sorted(file_names):
            path = Path(directory) / name
            if name not in {".DS_Store", "FILE_MANIFEST.sha256"} and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
        if b"\x00" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def clean_target(raw_target: str) -> str:
    target = raw_target.strip()
    if " " in target and not target.startswith("<"):
        target = target.split(None, 1)[0]
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    return target.split("#", 1)[0].rstrip(TRAILING_PATH_PUNCTUATION)


def looks_like_link_target(target: str) -> bool:
    """Reject markdown-looking code fragments such as ``[](const ...)``."""
    if any(token in target for token in ("::", "*", "=", "\\")):
        return False
    return (
        target.startswith((".", "/", "~"))
        or "/" in target
        or Path(target).suffix.lower() in TEXT_SUFFIXES
        or target.endswith(("/", ".md", ".html"))
    )


def resolve_target(source: Path, target: str) -> Path:
    candidate = Path(target).expanduser()
    return candidate if candidate.is_absolute() else (source.parent / candidate).resolve()


def add_external_path_findings(path: Path, text: str, findings: list[Finding], root: Path) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in ABSOLUTE_PATH_RE.finditer(line):
            raw = match.group(0).rstrip(TRAILING_PATH_PUNCTUATION)
            candidate = Path(raw).expanduser()
            try:
                inside_root = candidate.resolve().is_relative_to(root)
            except (OSError, ValueError):
                inside_root = False
            if not inside_root:
                findings.append(Finding("external-absolute-path", path, line_number, raw))
        if path.name == "validate_deliverable_paths.py":
            continue
        for placeholder in PLACEHOLDERS:
            if placeholder in line:
                findings.append(Finding("placeholder", path, line_number, placeholder))


def add_markdown_link_findings(path: Path, text: str, findings: list[Finding]) -> None:
    if path.suffix.lower() != ".md":
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in MARKDOWN_LINK_RE.finditer(line):
            target = clean_target(match.group(1))
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not looks_like_link_target(target):
                continue
            resolved = resolve_target(path, target)
            if not resolved.exists():
                findings.append(Finding("broken-markdown-link", path, line_number, target))


def walk_json_values(value: object, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from walk_json_values(child_value, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from walk_json_values(child, key)
    elif isinstance(value, str) and PATH_FIELD_RE.search(key):
        yield key, value


def add_json_path_findings(path: Path, text: str, findings: list[Finding]) -> None:
    if path.suffix.lower() != ".json":
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    for key, raw_value in walk_json_values(payload):
        value = raw_value.split("#", 1)[0]
        if not value or value.startswith(("http://", "https://", "<")):
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and "/" not in value and "\\" not in value:
            continue
        resolved = candidate if candidate.is_absolute() else (path.parent / candidate).resolve()
        if not resolved.exists():
            line = next((index for index, source_line in enumerate(text.splitlines(), 1) if raw_value in source_line), 1)
            findings.append(Finding("broken-json-path", path, line, f"{key}={raw_value}"))


def add_yaml_path_findings(path: Path, text: str, findings: list[Finding], root: Path) -> None:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = YAML_PATH_LINE_RE.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if not value or value.lower() in {"true", "false", "null", "none"} or value.startswith(("http://", "https://", "<", "$", "/tmp/", "/dev/")):
            continue
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            resolved = candidate
        elif value.startswith(("modules/", "reference/", "docs/", "schemas/", "tools/")):
            resolved = root / candidate
        else:
            resolved = (path.parent / candidate).resolve()
        if not resolved.exists():
            findings.append(Finding("broken-yaml-path", path, line_number, value))


def add_artifact_findings(root: Path, findings: list[Finding]) -> None:
    for directory, directory_names, file_names in os.walk(root):
        # A build directory can contain tens of thousands of files. Report the
        # directory itself and prune it before walking its generated contents.
        for generated_name in (".pio",):
            if generated_name in directory_names:
                path = Path(directory) / generated_name
                findings.append(Finding("generated-artifact", path, 1, generated_name))
                directory_names.remove(generated_name)
        if "__pycache__" in directory_names:
            directory_names.remove("__pycache__")
        for name in file_names:
            path = Path(directory) / name
            if name == ".DS_Store":
                findings.append(Finding("generated-artifact", path, 1, ".DS_Store"))
            elif name.endswith(".bak") or ".bak-" in name:
                findings.append(Finding("backup-file", path, 1, name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parents[1], type=Path)
    root = parser.parse_args().root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"deliverable root does not exist: {root}")

    findings: list[Finding] = []
    for path in iter_files(root):
        text = read_text(path)
        if text is None:
            continue
        add_external_path_findings(path, text, findings, root)
        add_markdown_link_findings(path, text, findings)
        add_json_path_findings(path, text, findings)
        add_yaml_path_findings(path, text, findings, root)
    add_artifact_findings(root, findings)

    if findings:
        for finding in findings:
            relative = finding.path.relative_to(root) if finding.path.is_relative_to(root) else finding.path
            print(f"{finding.kind}: {relative}:{finding.line}: {finding.detail}")
        print(f"\nFound {len(findings)} path/artifact issue(s).", file=sys.stderr)
        return 1
    print(f"Path validation passed: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
