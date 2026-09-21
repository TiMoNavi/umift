#!/usr/bin/env python3
"""Check the portable Python runtime without connecting to any hardware."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import platform
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-ffmpeg', action='store_true', help='Check GUI-only setup without video preview')
    args = parser.parse_args()
    errors = []
    print(f'Python: {sys.version.split()[0]} ({platform.machine()})\nInterpreter: {sys.executable}\nProject: {ROOT}')
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        errors.append('Use 64-bit CPython 3.11 or 3.12 for the pinned requirements.')
    if sys.maxsize <= 2**32:
        errors.append('A 64-bit Python interpreter is required.')
    modules = {'opencv-contrib-python': 'cv2', 'pyserial': 'serial', 'PyYAML': 'yaml'}
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        if '==' not in line or line.startswith('#'):
            continue
        package, expected = line.split('==', 1)
        try:
            module = importlib.import_module(modules.get(package, package))
            actual = importlib.metadata.version(package)
            if actual != expected:
                errors.append(f'{package}: expected {expected}, installed {actual}; reinstall requirements.txt')
            else:
                print(f'OK {package} {actual}')
            if package == 'opencv-contrib-python' and not hasattr(module, 'aruco'):
                errors.append('cv2.aruco is missing; remove other OpenCV variants and reinstall requirements.txt')
        except Exception as exc:
            errors.append(f'{package}: {exc}')
    for conflicting in ('opencv-python', 'opencv-python-headless', 'opencv-contrib-python-headless'):
        try:
            importlib.metadata.version(conflicting)
        except importlib.metadata.PackageNotFoundError:
            continue
        errors.append(f'Remove {conflicting}: it shares cv2 with opencv-contrib-python.')
    sys.path.insert(0, str(ROOT / 'modules/04_laptop_alignment_export'))
    try:
        importlib.import_module('umift_laptop_alignment.app.receiver_web_gui')
        from umift_laptop_alignment.orchestration.run_layout import manifest_lock, write_json_atomic
        from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import dataset_export_lock
        with tempfile.TemporaryDirectory(prefix='umift-env-') as tmp:
            path = Path(tmp) / 'manifest.json'
            with manifest_lock(path), dataset_export_lock(Path(tmp) / 'export'):
                write_json_atomic(path, {'check': True})
        print('OK backend import, manifest/export locks, atomic JSON write')
    except Exception as exc:
        errors.append(f'Backend/filesystem: {exc}')
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        print(f'OK ffmpeg: {ffmpeg}')
    elif not args.skip_ffmpeg:
        errors.append('ffmpeg executable is missing from PATH (see docs/INSTALL.md).')
    else:
        print('SKIP ffmpeg; iPhone video preview requires it.')
    if sys.platform != 'darwin':
        print('INFO D435 GUI control requires macOS; iPhone USB reception is not yet ported to Windows.')
    for error in errors:
        print(f'FAIL {error}', file=sys.stderr)
    print(f'Environment check: {len(errors)} error(s). Hardware connectivity was not tested.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
