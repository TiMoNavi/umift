"""HTTP transport for the Module 04 browser GUI."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from umift_laptop_alignment.app.platform_support import open_directory
from typing import Any


WEB_INDEX_PATH = Path(__file__).with_name("web") / "index.html"
HTML = WEB_INDEX_PATH.read_text(encoding="utf-8")


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def write_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def serve_file(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


class Handler(BaseHTTPRequestHandler):
    backend: Any

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            data = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/events"):
            self.serve_events()
            return
        if self.path.startswith("/state"):
            self.backend.refresh_preflight_state()
            state = self.backend.state_snapshot()
            write_response(
                self,
                {
                    "state": state,
                    "config": {
                        "output_root": str(self.backend.config.output_root),
                        "d435_preview_dir": str(self.backend.config.d435_preview_dir),
                        "runs_root": str(self.backend.config.runs_root),
                    },
                },
            )
            return
        if self.path.startswith("/datasets"):
            write_response(self, {"datasets": self.backend.dataset_catalog()})
            return
        if self.path.startswith("/d435/rgb"):
            snapshot = self.backend.d435_stream_snapshot(update_state=False)
            if not snapshot.get("ok"):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, snapshot.get("last_error") or "D435 preview is not healthy")
                return
            serve_file(self, self.backend.config.d435_preview_dir / "latest_rgb.jpg", "image/jpeg")
            return
        if self.path.startswith("/d435/depth"):
            snapshot = self.backend.d435_stream_snapshot(update_state=False)
            if not snapshot.get("ok"):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, snapshot.get("last_error") or "D435 preview is not healthy")
                return
            serve_file(self, self.backend.config.d435_preview_dir / "latest_depth.png", "image/png")
            return
        if self.path.startswith("/iphone/preview/jpeg"):
            with self.backend.lock:
                preview_dir_value = self.backend.state.get("iphone_video", {}).get("preview_dir")
            if not preview_dir_value:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "iPhone preview not ready")
                return
            jpeg_path = Path(str(preview_dir_value)) / "latest.jpg"
            if not jpeg_path.exists():
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "iPhone JPEG preview not yet available")
                return
            serve_file(self, jpeg_path, "image/jpeg")
            return
        if self.path.startswith("/iphone/preview/manifest"):
            with self.backend.lock:
                preview_dir_value = self.backend.state.get("iphone_video", {}).get("preview_dir")
            manifest_path = Path(str(preview_dir_value)) / "manifest.json" if preview_dir_value else None
            if manifest_path is None or not manifest_path.exists():
                write_response(self, {"ready": False})
            else:
                serve_file(self, manifest_path, "application/json")
            return
        if self.path.startswith("/iphone/preview/"):
            with self.backend.lock:
                preview_dir_value = self.backend.state.get("iphone_video", {}).get("preview_dir")
            if not preview_dir_value:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            filename = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            if not filename or Path(filename).name != filename:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            serve_file(self, Path(str(preview_dir_value)) / filename, "video/mp4")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            payload = read_json_body(self)
        except json.JSONDecodeError:
            payload = {}
        if self.path == "/main/command":
            try:
                result = self.backend.handle_main_command(payload)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            write_response(self, result, 200 if result.get("ok") else 400)
            return
        if self.path == "/run/ensure":
            run_dir = self.backend.ensure_active_run(payload.get("run_dir", ""), payload.get("run_id", ""))
            write_response(self, {"ok": True, "run_dir": str(run_dir), "run_id": run_dir.name})
            return
        if self.path == "/arm":
            result = self.backend.arm_capture(payload)
            write_response(self, {"ok": True, **result})
            return
        if self.path == "/start":
            started = self.backend.start_receiver(payload)
            write_response(self, {"ok": True, "started": started})
            return
        if self.path == "/stop":
            stopped = self.backend.stop_receiver()
            write_response(self, {"ok": True, "stopped": stopped})
            return
        if self.path == "/teensy/start":
            started = self.backend.start_coinft()
            write_response(self, {"ok": True, "started": started})
            return
        if self.path == "/teensy/stop":
            stopped = self.backend.stop_teensy()
            write_response(self, {"ok": True, "stopped": stopped})
            return
        if self.path == "/d435/start":
            started = self.backend.start_d435_preview()
            write_response(self, {"ok": True, "started": started})
            return
        if self.path == "/d435/stop":
            stopped = self.backend.stop_d435_preview()
            write_response(self, {"ok": True, "stopped": stopped})
            return
        if self.path == "/normalize":
            report = self.backend.normalize_current_run()
            write_response(self, {"ok": True, "report": report})
            return
        if self.path == "/align":
            report = self.backend.align_current_run(payload)
            write_response(self, {"ok": True, "report": report})
            return
        if self.path == "/open-output":
            with self.backend.lock:
                path = self.backend.state.get("run_dir") or self.backend.state.get("session_dir") or str(self.backend.config.output_root)
            open_directory(path)
            write_response(self, {"ok": True, "path": path})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def serve_events(self) -> None:
        client = self.backend.add_client()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                message = client.get(timeout=15)
                if message is not None:
                    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    self.wfile.write(b"data: " + data + b"\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.backend.remove_client(client)
