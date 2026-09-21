from __future__ import annotations

import json
import multiprocessing
import os
import threading
import urllib.request
from unittest.mock import patch

import pytest

from umift_laptop_alignment.orchestration.run_layout import manifest_lock, write_json_atomic
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import dataset_export_lock


def _lock(kind, directory):
    return manifest_lock(directory / 'RUN_MANIFEST.json') if kind == 'manifest' else dataset_export_lock(directory)


def _increment(kind, directory, ready, start):
    ready.put(True)
    if not start.wait(20):
        raise RuntimeError('Workers did not start')
    path = directory / 'counter.json'
    for _ in range(20):
        with _lock(kind, directory):
            value = json.loads(path.read_text(encoding='utf-8'))['count']
            write_json_atomic(path, {'count': value + 1})


@pytest.mark.parametrize('kind', ['manifest', 'export'])
def test_processes_preserve_all_updates_and_release_after_exception(tmp_path, kind):
    # Exercise the actual OS primitive with spawn, including on Windows CI.
    with pytest.raises(RuntimeError):
        with _lock(kind, tmp_path):
            raise RuntimeError('write failed')
    write_json_atomic(tmp_path / 'counter.json', {'count': 0})
    context = multiprocessing.get_context('spawn')
    ready, start = context.Queue(), context.Event()
    processes = [context.Process(target=_increment, args=(kind, tmp_path, ready, start)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
    assert json.loads((tmp_path / 'counter.json').read_text()) == {'count': 60}
    assert not list(tmp_path.glob('*.tmp'))


def test_atomic_write_without_unix_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(os, 'fchmod', raising=False)
    path = tmp_path / 'nested' / 'manifest.json'
    write_json_atomic(path, {'value': '中文'})
    write_json_atomic(path, {'value': 'updated'})
    assert json.loads(path.read_text(encoding='utf-8')) == {'value': 'updated'}
    assert not list(path.parent.glob('*.tmp'))


def test_gui_http_start_and_clean_exit_without_hardware(tmp_path, monkeypatch):
    from umift_laptop_alignment.app import receiver_web_gui as gui
    from http.server import ThreadingHTTPServer

    class SmokeServer(ThreadingHTTPServer):
        def serve_forever(self, poll_interval=0.5):
            # Run one real HTTP request, then simulate Ctrl-C through main's cleanup.
            worker = threading.Thread(target=self.handle_request, daemon=True)
            worker.start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.server_port}/', timeout=10) as response:
                    assert response.status == 200
                    assert b'<html' in response.read().lower()
            finally:
                worker.join(timeout=10)
            raise KeyboardInterrupt

    monkeypatch.setattr(gui, 'ThreadingHTTPServer', SmokeServer)
    monkeypatch.setattr('sys.argv', ['receiver_web_gui', '--host', '127.0.0.1', '--port', '0',
                                    '--no-auto-start', '--no-open', '--runs-root', str(tmp_path / 'runs'),
                                    '--output-root', str(tmp_path / 'iphone')])
    # Exercise the unsupported-platform path also on macOS; no sudo or devices.
    with patch.object(gui.sys, 'platform', 'win32'), patch.object(gui.subprocess, 'run') as external:
        assert gui.main() == 0
        assert gui.Handler.backend.start_d435_preview() is False
        assert 'requires macOS' in gui.Handler.backend.state['d435']['last_error']
        assert gui.Handler.backend.stop_d435_preview() is False
        external.assert_not_called()
