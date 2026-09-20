"""RGB loading and resizing for UMI-FT replay-buffer export."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SequentialH264Decoder:
    """Decode an Annex-B segment by index without relying on broken raw-H264 seeking."""

    def __init__(self, path: Path, cv2: Any) -> None:
        self.path = path
        self.cv2 = cv2
        self.capture = cv2.VideoCapture(str(path))
        self.next_index = 0
        self.last_index: int | None = None
        self.last_frame: Any | None = None

    def read(self, frame_index: int) -> Any | None:
        if frame_index < 0:
            return None
        if frame_index == self.last_index:
            return self.last_frame
        if frame_index < self.next_index:
            self.release()
            self.capture = self.cv2.VideoCapture(str(self.path))
            self.next_index = 0
            self.last_index = None
            self.last_frame = None
        if not self.capture.isOpened():
            return None
        while self.next_index <= frame_index:
            ok, frame = self.capture.read()
            if not ok:
                return None
            self.last_index = self.next_index
            self.last_frame = frame
            self.next_index += 1
        return self.last_frame

    def release(self) -> None:
        self.capture.release()


def load_iphone_rgb(
    run_dir: Path,
    rgb: dict[str, Any],
    cv2: Any,
    decoders: dict[Path, SequentialH264Decoder],
) -> Any | None:
    video_path = rgb.get("video_path")
    frame_index = rgb.get("frame_index")
    if not isinstance(video_path, str) or not isinstance(frame_index, int):
        return None
    absolute = run_dir / video_path
    decoder = decoders.get(absolute)
    if decoder is None:
        decoder = SequentialH264Decoder(absolute, cv2)
        decoders[absolute] = decoder
    return decoder.read(frame_index)


def load_d435_rgb(run_dir: Path, d435: dict[str, Any], cv2: Any, captures: dict[Path, Any]) -> Any | None:
    rgb = d435.get("rgb", {}) if isinstance(d435.get("rgb"), dict) else {}
    video_path = rgb.get("video_path")
    frame_index = rgb.get("frame_index")
    if not isinstance(video_path, str) or not isinstance(frame_index, int):
        return None
    absolute = run_dir / video_path
    capture = captures.get(absolute)
    if capture is None:
        capture = cv2.VideoCapture(str(absolute))
        captures[absolute] = capture
    if not capture.isOpened():
        return None
    capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = capture.read()
    return frame if ok else None


def resize_bgr_to_rgb(image: Any, cv2: Any, np: Any, size: tuple[int, int]) -> Any:
    resized = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    return rgb.astype(np.uint8)
