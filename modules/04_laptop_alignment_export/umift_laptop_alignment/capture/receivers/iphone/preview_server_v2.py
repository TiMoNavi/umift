"""Local browser preview server for the authoritative iPhone fMP4 stream."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional


PREVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iPhone Main Camera</title>
<style>
:root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; background: #090b0f; color: #f2f4f7; display: grid; grid-template-rows: 44px minmax(0,1fr); }
header { display: flex; align-items: center; gap: 18px; padding: 0 14px; background: #151922; border-bottom: 1px solid #303744; font-size: 13px; white-space: nowrap; overflow: hidden; }
header strong { font-size: 14px; }
.metric { color: #b8c0cc; }
.metric span { color: #f2f4f7; font-variant-numeric: tabular-nums; }
#state[data-state="live"] { color: #62d68b; }
#state[data-state="error"] { color: #ff7a7a; }
main { min-height: 0; display: grid; place-items: center; overflow: hidden; }
video { width: 100%; height: 100%; object-fit: contain; background: #000; }
</style>
</head>
<body>
<header>
  <strong>iPhone Main Camera</strong>
  <div id="state" class="metric" data-state="waiting">waiting</div>
  <div class="metric">codec <span id="codec">--</span></div>
  <div class="metric">sequence <span id="sequence">--</span></div>
  <div class="metric">receiver age <span id="age">--</span></div>
  <div class="metric">buffer <span id="buffer">--</span></div>
</header>
<main><video id="video" muted autoplay playsinline></video></main>
<script>
const video = document.getElementById("video");
const stateEl = document.getElementById("state");
const codecEl = document.getElementById("codec");
const sequenceEl = document.getElementById("sequence");
const ageEl = document.getElementById("age");
const bufferEl = document.getElementById("buffer");
let mediaSource = null;
let sourceBuffer = null;
let sessionID = null;
let lastFragmentIndex = 0;
let syncing = false;
let appendQueue = [];

function setState(text, state) {
  stateEl.textContent = text;
  stateEl.dataset.state = state;
}

function sourceOpen(mediaSource) {
  if (mediaSource.readyState === "open") return Promise.resolve();
  return new Promise(resolve => mediaSource.addEventListener("sourceopen", resolve, {once:true}));
}

function pumpAppendQueue() {
  if (!sourceBuffer || sourceBuffer.updating || appendQueue.length === 0) return;
  const item = appendQueue.shift();
  const done = () => { sourceBuffer.removeEventListener("updateend", done); item.resolve(); pumpAppendQueue(); };
  sourceBuffer.addEventListener("updateend", done);
  try { sourceBuffer.appendBuffer(item.buffer); }
  catch (error) { sourceBuffer.removeEventListener("updateend", done); item.reject(error); pumpAppendQueue(); }
}

function appendBuffer(buffer) {
  return new Promise((resolve, reject) => { appendQueue.push({buffer, resolve, reject}); pumpAppendQueue(); });
}

async function fetchBuffer(path) {
  const response = await fetch(path, {cache:"no-store"});
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.arrayBuffer();
}

async function resetPlayer(manifest) {
  appendQueue = [];
  if (mediaSource) {
    try { mediaSource.endOfStream(); } catch (_) {}
  }
  const oldObjectURL = video.src;
  mediaSource = new MediaSource();
  video.src = URL.createObjectURL(mediaSource);
  if (oldObjectURL && oldObjectURL.startsWith("blob:")) URL.revokeObjectURL(oldObjectURL);
  await sourceOpen(mediaSource);
  if (!MediaSource.isTypeSupported(manifest.mime_type)) throw new Error(`unsupported ${manifest.mime_type}`);
  sourceBuffer = mediaSource.addSourceBuffer(manifest.mime_type);
  sourceBuffer.mode = "segments";
  await appendBuffer(await fetchBuffer("/preview/init.mp4"));
  sessionID = manifest.session_id;
  lastFragmentIndex = (manifest.bootstrap_fragment_index || 1) - 1;
}

function updatePlaybackPosition() {
  if (!sourceBuffer || sourceBuffer.buffered.length === 0) return;
  const start = sourceBuffer.buffered.start(0);
  const end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
  if (!Number.isFinite(video.currentTime) || video.currentTime < start || video.currentTime === 0) {
    video.currentTime = Math.max(start, end - 0.1);
  }
  // Only seek when meaningfully behind the live edge. A small threshold causes
  // repeated decoder flushes at high bitrates (1920x1440) and is the primary
  // source of visible stutter — keep it at 1.5s to absorb normal append jitter.
  if (end - video.currentTime > 1.5) {
    video.currentTime = Math.max(start, end - 0.1);
  }
  if (start < end - 12 && !sourceBuffer.updating) {
    try { sourceBuffer.remove(start, end - 10); } catch (_) {}
  }
  bufferEl.textContent = `${Math.max(0, end - video.currentTime).toFixed(2)}s`;
  video.play().catch(() => {});
}

async function syncPreview() {
  if (syncing) return;
  syncing = true;
  try {
    const response = await fetch("/api/manifest", {cache:"no-store"});
    const manifest = await response.json();
    if (!manifest.ready) {
      setState("waiting", "waiting");
      return;
    }
    codecEl.textContent = `${manifest.codec} ${manifest.width}x${manifest.height}@${manifest.frames_per_second}`;
    const ageMS = Math.max(0, Date.now() - manifest.updated_unix_ns / 1e6);
    ageEl.textContent = `${ageMS.toFixed(0)}ms`;
    if (sessionID !== manifest.session_id || !sourceBuffer) await resetPlayer(manifest);
    if (manifest.bootstrap_fragment_index > lastFragmentIndex + 1) await resetPlayer(manifest);
    const fragments = manifest.fragments
      .filter(row => row.index > lastFragmentIndex)
      .sort((a, b) => a.index - b.index);
    for (const fragment of fragments) {
      if (fragment.index !== lastFragmentIndex + 1) { await resetPlayer(manifest); break; }
      await appendBuffer(await fetchBuffer(`/preview/${fragment.filename}`));
      lastFragmentIndex = fragment.index;
      sequenceEl.textContent = String(fragment.last_sequence);
    }
    updatePlaybackPosition();
    setState("live", "live");
  } catch (error) {
    setState(error.message || String(error), "error");
  } finally {
    syncing = false;
  }
}

if (!("MediaSource" in window)) setState("MediaSource unavailable", "error");
else setInterval(syncPreview, 125);
</script>
</body>
</html>
"""


class PreviewServer:
    def __init__(
        self,
        preview_directory_provider: Callable[[], Optional[Path]],
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.preview_directory_provider = preview_directory_provider
        self.host = host
        self.port = port
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        provider = self.preview_directory_provider

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/" or self.path.startswith("/?"):
                    self._send(PREVIEW_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                preview_dir = provider()
                if self.path.startswith("/api/manifest"):
                    manifest_path = preview_dir / "manifest.json" if preview_dir else None
                    if manifest_path is None or not manifest_path.exists():
                        self._send(
                            json.dumps({"ready": False}).encode("utf-8"),
                            "application/json",
                        )
                        return
                    self._send(manifest_path.read_bytes(), "application/json")
                    return
                if self.path.startswith("/preview/"):
                    if preview_dir is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    filename = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
                    if not filename or Path(filename).name != filename:
                        self.send_error(HTTPStatus.BAD_REQUEST)
                        return
                    media_path = preview_dir / filename
                    if not media_path.exists():
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    content_type = "application/json" if filename.endswith(".json") else "video/mp4"
                    self._send(media_path.read_bytes(), content_type)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(self, payload: bytes, content_type: str) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="iphone-preview-v2", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
