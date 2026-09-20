"""GUI lifecycle adapter for the authoritative iPhone H.264 V2 receiver."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Protocol

from umift_laptop_alignment.capture.receivers.iphone.archive_receiver_v2 import (
    ArchiveReceiverObserver,
    ArchiveReceiverV2,
)
from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import ProtocolError
from umift_laptop_alignment.capture.receivers.iphone.usbmux import (
    UsbmuxError,
    choose_device,
    connect_device_port,
)
from umift_laptop_alignment.capture.receivers.iphone.video_segment_writer import VideoArchiveError


class GuiReceiverV2Callbacks(ArchiveReceiverObserver, Protocol):
    def iphone_v2_status(self, status: str) -> None: ...

    def iphone_v2_log(self, text: str) -> None: ...

    def iphone_v2_finished(self) -> None: ...


class GuiReceiverV2(threading.Thread):
    def __init__(
        self,
        *,
        callbacks: GuiReceiverV2Callbacks,
        stop_event: threading.Event,
        output_root: Path,
        transport: str,
        device: str,
        device_port: int,
        host: str,
        port: int,
        retry_s: float,
        ack_interval_frames: int = 3,
    ) -> None:
        super().__init__(name="iphone-v2-receiver", daemon=True)
        self.callbacks = callbacks
        self.stop_event = stop_event
        self.output_root = output_root
        self.transport = transport
        self.device = device
        self.device_port = device_port
        self.host = host
        self.port = port
        self.retry_s = max(0.1, retry_s)
        self.ack_interval_frames = max(1, ack_interval_frames)
        self.sock: socket.socket | None = None

    def request_stop(self) -> None:
        self.stop_event.set()
        sock = self.sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def run(self) -> None:
        receiver = self._make_receiver()
        try:
            while not self.stop_event.is_set():
                try:
                    self.sock = self._connect()
                    self.callbacks.iphone_v2_status(f"connected via {self.transport}")
                    self.callbacks.iphone_v2_log(f"iPhone V2 connected via {self.transport}")
                    receiver.run_socket(self.sock)
                    if not self.stop_event.is_set():
                        self.callbacks.iphone_v2_status("reconnecting")
                        self.callbacks.iphone_v2_log("iPhone V2 stream ended; reconnecting")
                except (OSError, UsbmuxError) as exc:
                    if not self.stop_event.is_set():
                        self.callbacks.iphone_v2_status("waiting for iPhone V2 stream")
                        self.callbacks.iphone_v2_log(f"waiting for iPhone V2 stream: {exc}")
                except ProtocolError as exc:
                    session_changed = (
                        "session changed from" in str(exc)
                        or "receiver already owns session" in str(exc)
                    )
                    if session_changed and not self.stop_event.is_set():
                        receiver.close()
                        receiver = self._make_receiver()
                        self.callbacks.iphone_v2_status("reconnecting for new session")
                        self.callbacks.iphone_v2_log("iPhone V2 session changed; archived the previous session")
                    else:
                        receiver.mark_failed(str(exc))
                        self.callbacks.iphone_v2_status("error")
                        self.callbacks.iphone_v2_log(f"iPhone V2 integrity failure: {exc}")
                        break
                except VideoArchiveError as exc:
                    receiver.mark_failed(str(exc))
                    self.callbacks.iphone_v2_status("error")
                    self.callbacks.iphone_v2_log(f"iPhone V2 integrity failure: {exc}")
                    break
                finally:
                    self._close_socket()
                if not self.stop_event.is_set():
                    self.stop_event.wait(self.retry_s)
        finally:
            receiver.close()
            self.callbacks.iphone_v2_finished()

    def _make_receiver(self) -> ArchiveReceiverV2:
        return ArchiveReceiverV2(
            self.output_root,
            ack_interval_frames=self.ack_interval_frames,
            observer=self.callbacks,
        )

    def _connect(self) -> socket.socket:
        if self.transport == "tcp":
            return socket.create_connection((self.host, self.port), timeout=10)
        device = choose_device(self.device or None)
        return connect_device_port(device, self.device_port)

    def _close_socket(self) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
