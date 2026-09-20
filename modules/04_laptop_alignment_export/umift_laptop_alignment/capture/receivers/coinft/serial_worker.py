"""Live CoinFT/Teensy USB-serial raw collection worker."""

from __future__ import annotations

import csv
import glob
import os
import struct
import threading
import time
import json
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]

from umift_laptop_alignment.capture.buffers.timestamped_ring import preroll_start_ns
from umift_laptop_alignment.orchestration.run_layout import register_artifact, timestamp_id
from umift_laptop_alignment.capture.receivers.coinft.calibration import (
    CoinFTCalibrationConfig,
    CoinFTCalibrationRuntime,
    SIDES,
    WRENCH_AXES,
    calibrated_csv_fieldnames,
    calibration_result_csv_fields,
    calibration_status_summary,
    raw_coinft_config,
)
from umift_laptop_alignment.capture.receivers.coinft.buffer import CoinFTRawBuffer


HEADER = b"\x00\x00"
BAUD_RATE = 115200
COINFT_CHANNELS = 12
METADATA_LEN = 8
PACKET_LEN = len(HEADER) + METADATA_LEN + COINFT_CHANNELS * 2 * 2
UINT32_MOD = 2**32
DEFAULT_RECORDING_PREROLL_S = 1.5
DEFAULT_STREAM_BUFFER_S = 3.0
DEFAULT_SERIAL_TIMEOUT_S = 0.02
DEFAULT_PACKET_TIMEOUT_S = 1.0
DEFAULT_MAX_SAMPLES = 6000
MAX_ITEMS_PER_BUFFER_SECOND = 2000
RECOVERY_TIMEOUTS = 3
REOPEN_TIMEOUTS = 12
DIAGNOSTIC_TIMEOUT_S = 1.8


class PacketTimeout(TimeoutError):
    pass


def open_exclusive_episode_files(output_dir: Path) -> tuple[Any, Any, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = output_dir / "raw_coinft_packets.bin"
    csv_path = output_dir / "raw_coinft_stream.csv"
    raw_file = None
    csv_file = None
    try:
        raw_file = raw_path.open("xb")
        csv_file = csv_path.open("x", newline="", encoding="utf-8")
    except Exception:
        if raw_file is not None:
            raw_file.close()
        if csv_file is not None:
            csv_file.close()
        raise
    return raw_file, csv_file, raw_path, csv_path


def detect_default_port() -> str | None:
    env_port = os.environ.get("UMIFT_COINFT_PORT", "").strip()
    if env_port:
        return env_port
    patterns = (
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.SLAB_USBtoUART*",
        "/dev/cu.wchusbserial*",
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
    )
    candidates: list[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    candidates = sorted(dict.fromkeys(candidates))
    return candidates[0] if candidates else None


def read_exact(ser: Any, byte_count: int, deadline_s: float) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        if time.monotonic() >= deadline_s:
            raise PacketTimeout(f"timed out reading {byte_count} bytes")
        chunk = ser.read(remaining)
        if not chunk:
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_packet(ser: Any, timeout_s: float) -> tuple[bytes, int]:
    deadline_s = time.monotonic() + timeout_s
    window = bytearray()
    skipped = 0
    while time.monotonic() < deadline_s:
        byte = ser.read(1)
        if not byte:
            continue
        window += byte
        if len(window) > len(HEADER):
            window = window[-len(HEADER):]
            skipped += 1
        if bytes(window) == HEADER:
            body = read_exact(ser, PACKET_LEN - len(HEADER), deadline_s)
            return HEADER + body, skipped
    raise PacketTimeout("no complete CoinFT packet found")


def unpack_packet(packet: bytes) -> tuple[int, int, tuple[int, ...], tuple[int, ...]]:
    if len(packet) != PACKET_LEN:
        raise ValueError(f"expected {PACKET_LEN} bytes, got {len(packet)}")
    if packet[: len(HEADER)] != HEADER:
        raise ValueError("packet header mismatch")

    body = packet[len(HEADER) :]
    sequence_id, teensy_time_us = struct.unpack("<II", body[:METADATA_LEN])
    values = struct.unpack("<" + "H" * (COINFT_CHANNELS * 2), body[METADATA_LEN:])
    left = tuple(int(v) for v in values[:COINFT_CHANNELS])
    right = tuple(int(v) for v in values[COINFT_CHANNELS:])
    return int(sequence_id), int(teensy_time_us), left, right


def estimate_sequence_gap(last_sequence: int | None, sequence_id: int) -> tuple[int, bool]:
    if last_sequence is None:
        return 0, False
    expected = (last_sequence + 1) % UINT32_MOD
    if sequence_id == expected:
        return 0, False
    gap = (sequence_id - expected) % UINT32_MOD
    if gap < 1_000_000:
        return int(gap), False
    return 0, True


def csv_fieldnames(include_calibration: bool = False) -> list[str]:
    fields = [
        "packet_index",
        "host_receive_monotonic_ns",
        "host_receive_unix_ns",
        "sequence_id",
        "teensy_time_us",
        *[f"left_c{i}" for i in range(1, COINFT_CHANNELS + 1)],
        *[f"right_c{i}" for i in range(1, COINFT_CHANNELS + 1)],
    ]
    if include_calibration:
        fields.extend(calibrated_csv_fieldnames())
    return fields


def packet_row(
    *,
    packet_index: int,
    host_receive_monotonic_ns: int,
    host_receive_unix_ns: int,
    sequence_id: int,
    teensy_time_us: int,
    left: tuple[int, ...],
    right: tuple[int, ...],
) -> dict[str, int]:
    row: dict[str, int] = {
        "packet_index": packet_index,
        "host_receive_monotonic_ns": host_receive_monotonic_ns,
        "host_receive_unix_ns": host_receive_unix_ns,
        "sequence_id": sequence_id,
        "teensy_time_us": teensy_time_us,
    }
    row.update({f"left_c{i}": value for i, value in enumerate(left, start=1)})
    row.update({f"right_c{i}": value for i, value in enumerate(right, start=1)})
    return row


def six_float_list(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 6:
        return None
    out: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)):
            return None
        out.append(float(item))
    return out


def dual_wrench_from_result(calibration_result: dict[str, Any]) -> list[list[float]] | None:
    left = six_float_list(calibration_result.get("left_wrench"))
    right = six_float_list(calibration_result.get("right_wrench"))
    if left is None or right is None:
        return None
    return [left, right]


def parse_teensy_diagnostics(lines: list[str]) -> dict[str, Any]:
    active_ports: list[str] = []
    port_rows: dict[str, dict[str, Any]] = {}
    selected_left: str | None = None
    selected_right: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("Serial") and " active=" in line:
            parts = line.split()
            name = parts[0]
            row: dict[str, Any] = {"line": line}
            for part in parts[1:]:
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                row[key] = value
            port_rows[name] = row
            if row.get("active") == "yes":
                active_ports.append(name)
        elif line.startswith("left="):
            value = line.split("=", 1)[1].strip()
            selected_left = None if value == "none" else value
        elif line.startswith("right="):
            value = line.split("=", 1)[1].strip()
            selected_right = None if value == "none" else value
    return {
        "active_ports": active_ports,
        "ports": port_rows,
        "selected_left_port": selected_left,
        "selected_right_port": selected_right,
        "raw_lines": lines,
    }


def infer_missing_side(
    diagnostic: dict[str, Any] | None,
    *,
    expected_left_port: str | None,
    expected_right_port: str | None,
) -> str | None:
    if not diagnostic:
        return None
    active_ports = set(diagnostic.get("active_ports", []))
    if expected_left_port and expected_right_port:
        left_active = expected_left_port in active_ports
        right_active = expected_right_port in active_ports
        if left_active and right_active:
            return None
        if not left_active and not right_active:
            return "both"
        if not left_active:
            return "left"
        if not right_active:
            return "right"
    if len(active_ports) == 0:
        return "both"
    if len(active_ports) == 1:
        return "unknown_one_side"
    return None


class CoinFTSerialWorker(threading.Thread):
    """Read true CoinFT raw packets from a Teensy USB serial bridge."""

    def __init__(
        self,
        backend: Any,
        stop_event: threading.Event,
        *,
        port: str = "",
        baud: int = BAUD_RATE,
        simulate: bool = False,
        tare: bool = False,
        no_control_commands: bool = False,
        leave_streaming: bool = False,
        serial_timeout_s: float = DEFAULT_SERIAL_TIMEOUT_S,
        packet_timeout_s: float = DEFAULT_PACKET_TIMEOUT_S,
        calibration_config: CoinFTCalibrationConfig | None = None,
    ):
        super().__init__(daemon=True)
        self.backend = backend
        self.stop_event = stop_event
        self.port = port
        self.baud = int(baud)
        self.simulate = bool(simulate)
        self.tare = bool(tare)
        self.no_control_commands = bool(no_control_commands)
        self.leave_streaming = bool(leave_streaming)
        self.serial_timeout_s = float(serial_timeout_s)
        self.packet_timeout_s = float(packet_timeout_s)
        self.calibration_config = calibration_config or raw_coinft_config()
        self.recovery_count = 0

    def update_teensy_state(self, updates: dict[str, Any]) -> None:
        if hasattr(self.backend, "update_coinft_state"):
            self.backend.update_coinft_state(updates)
            return
        with self.backend.lock:
            state = {**self.backend.state.get("teensy", {}), **updates}
            self.backend.state["teensy"] = state
            snapshot = dict(self.backend.state)
        self.backend.broadcast({"type": "state", "state": snapshot})

    def send_start_commands(self, ser: Any) -> None:
        if self.no_control_commands:
            return
        ser.write(b"i")
        time.sleep(0.2)
        ser.reset_input_buffer()
        if self.tare:
            ser.write(b"t")
            time.sleep(0.1)
        ser.write(b"m" if self.simulate else b"s")
        time.sleep(0.05)

    def stop_stream(self, ser: Any) -> None:
        if self.no_control_commands or self.leave_streaming:
            return
        try:
            ser.write(b"i")
            time.sleep(0.02)
        except Exception:
            pass

    def recover_stream(self, ser: Any, reason: str) -> str:
        self.recovery_count += 1
        message = (
            f"{reason}; attempting CoinFT stream recovery #{self.recovery_count}. "
            "Expected 58-byte dual-CoinFT packets; if this repeats, check power/GND/TX/RX on both CoinFT boards."
        )
        self.backend.log(message)
        try:
            self.stop_stream(ser)
            time.sleep(0.05)
            ser.reset_input_buffer()
            self.send_start_commands(ser)
            return message
        except Exception as exc:  # noqa: BLE001
            return f"{message} Recovery command failed: {exc}"

    def run_teensy_diagnostics(self, ser: Any) -> dict[str, Any] | None:
        if self.no_control_commands or self.simulate:
            return None
        try:
            ser.reset_input_buffer()
            ser.write(b"d")
            deadline = time.monotonic() + DIAGNOSTIC_TIMEOUT_S
            lines: list[str] = []
            while time.monotonic() < deadline:
                raw_line = ser.readline()
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                lines.append(line)
                if line == "coinft_diag end":
                    break
            diagnostic = parse_teensy_diagnostics(lines)
            self.backend.log(
                "CoinFT diagnostic: "
                f"active={diagnostic.get('active_ports')} "
                f"selected_left={diagnostic.get('selected_left_port')} "
                f"selected_right={diagnostic.get('selected_right_port')}"
            )
            return diagnostic
        except Exception as exc:  # noqa: BLE001
            self.backend.log(f"CoinFT diagnostic failed: {exc}")
            return None

    def episode_output_dir(self, episode_index: int) -> Path:
        with self.backend.lock:
            run_dir = self.backend.state.get("run_dir")
        if not run_dir:
            run_dir = str(self.backend.ensure_active_run())
        return Path(str(run_dir)) / "raw" / "coinft" / f"episode_{episode_index:06d}"

    def run(self) -> None:
        if serial is None:
            self.update_teensy_state(
                {
                    "running": False,
                    "source": "coinft_serial",
                    "last_error": "pyserial is not installed",
                    "status": "error",
                }
            )
            self.backend.log("CoinFT serial unavailable: pyserial is not installed")
            self.backend.teensy_finished()
            return

        port = self.port.strip() or detect_default_port()
        if not port:
            self.update_teensy_state(
                {
                    "running": False,
                    "source": "coinft_serial",
                    "serial_port": None,
                    "last_error": "No Teensy USB serial port found; expected /dev/cu.usbmodem* or set UMIFT_COINFT_PORT",
                    "status": "no_port",
                }
            )
            self.backend.log("CoinFT serial unavailable: no Teensy USB serial port found")
            self.backend.teensy_finished()
            return

        options = self.backend.capture_options_snapshot()
        stream_buffer_s = float(options.get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S))
        coinft_buffer = CoinFTRawBuffer(
            name="coinft",
            max_age_s=stream_buffer_s,
            max_items=max(DEFAULT_MAX_SAMPLES, int(stream_buffer_s * MAX_ITEMS_PER_BUFFER_SECOND)),
        )

        live_sample_count = 0
        recorded_sample_count = 0
        episode_recorded_count = 0
        timeout_count = 0
        consecutive_timeout_count = 0
        stream_reopen_count = 0
        skipped_byte_count = 0
        dropped_sequence_count = 0
        out_of_order_sequence_count = 0
        last_sequence: int | None = None
        started = time.monotonic()
        # Use the Teensy clock for the displayed sample rate.  Host receive
        # timestamps can bunch up when USB packets are drained in bursts,
        # which makes ``count / wall_time`` report impossible rates (hundreds
        # of Hz for a nominal 100 Hz stream).
        first_teensy_time_us: int | None = None
        last_teensy_time_us: int | None = None
        active_episode_index: int | None = None
        persisted_sequences: set[int] = set()
        csv_file: Any | None = None
        raw_file: Any | None = None
        writer: csv.DictWriter[Any] | None = None
        csv_path: Path | None = None
        raw_path: Path | None = None
        include_calibration = bool(self.calibration_config.calibrated)
        calibration_runtime: CoinFTCalibrationRuntime | None = None
        calibration_runtime_error: str | None = None
        expected_left_port: str | None = None
        expected_right_port: str | None = None
        last_diagnostic: dict[str, Any] | None = None
        missing_side: str | None = None

        try:
            calibration_runtime = CoinFTCalibrationRuntime(self.calibration_config)
        except Exception as exc:  # noqa: BLE001
            calibration_runtime_error = str(exc)
            include_calibration = True

        def close_writers() -> None:
            nonlocal csv_file, raw_file, writer
            if csv_file is not None:
                csv_file.flush()
                csv_file.close()
            if raw_file is not None:
                raw_file.flush()
                raw_file.close()
            csv_file = None
            raw_file = None
            writer = None

        def write_sample(payload: dict[str, Any]) -> bool:
            nonlocal recorded_sample_count, episode_recorded_count
            if writer is None or raw_file is None:
                return False
            row = payload.get("row")
            raw_packet = payload.get("raw_packet")
            sequence = row.get("sequence_id") if isinstance(row, dict) else None
            if not isinstance(row, dict) or not isinstance(raw_packet, bytes) or not isinstance(sequence, int):
                return False
            if sequence in persisted_sequences:
                return False
            out = dict(row)
            out["packet_index"] = episode_recorded_count
            raw_file.write(raw_packet)
            writer.writerow(out)
            persisted_sequences.add(sequence)
            recorded_sample_count += 1
            episode_recorded_count += 1
            return True

        try:
            self.update_teensy_state(
                {
                    "running": True,
                    "source": "coinft_serial",
                    "serial_port": port,
                    "baud": self.baud,
                    "status": "opening",
                    "last_error": None,
                    "live_sample_count": 0,
                    "recorded_sample_count": 0,
                    "fps": 0.0,
                    **calibration_status_summary(self.calibration_config),
                    "calibration_status": "error"
                    if calibration_runtime_error
                    else ("warming_up" if self.calibration_config.calibrated else "raw_only"),
                    "calibration_ready": not self.calibration_config.calibrated,
                    "calibration_error": calibration_runtime_error,
                }
            )
            self.backend.log(f"CoinFT serial opening {port} @ {self.baud}")
            with serial.Serial(port, self.baud, timeout=self.serial_timeout_s) as ser:
                if not self.simulate and not self.no_control_commands:
                    startup_diagnostic = self.run_teensy_diagnostics(ser)
                    if startup_diagnostic is not None:
                        expected_left_port = startup_diagnostic.get("selected_left_port")
                        expected_right_port = startup_diagnostic.get("selected_right_port")
                        last_diagnostic = startup_diagnostic
                        missing_side = infer_missing_side(
                            startup_diagnostic,
                            expected_left_port=expected_left_port,
                            expected_right_port=expected_right_port,
                        )
                        self.update_teensy_state(
                            {
                                "expected_left_port": expected_left_port,
                                "expected_right_port": expected_right_port,
                                "diagnostic": startup_diagnostic,
                                "active_ports": startup_diagnostic.get("active_ports"),
                                "missing_side": missing_side,
                            }
                        )
                self.send_start_commands(ser)
                self.update_teensy_state({"status": "streaming"})
                self.backend.log("CoinFT serial streaming")
                while not self.stop_event.is_set():
                    try:
                        packet, skipped = read_packet(ser, self.packet_timeout_s)
                    except PacketTimeout:
                        timeout_count += 1
                        consecutive_timeout_count += 1
                        recovery_message = None
                        status = "waiting_for_packet"
                        last_error = (
                            "No complete 58-byte dual CoinFT packet found. "
                            "Likely causes: one CoinFT stopped sending, UART/power/GND/TX/RX contact is unstable, "
                            "or Teensy is no longer emitting the framed dual packet."
                        )
                        diagnostic = None
                        if consecutive_timeout_count >= RECOVERY_TIMEOUTS and consecutive_timeout_count % RECOVERY_TIMEOUTS == 0:
                            diagnostic = self.run_teensy_diagnostics(ser)
                            if diagnostic is not None:
                                last_diagnostic = diagnostic
                                if expected_left_port is None and diagnostic.get("selected_left_port") and diagnostic.get("selected_right_port"):
                                    expected_left_port = diagnostic.get("selected_left_port")
                                    expected_right_port = diagnostic.get("selected_right_port")
                                missing_side = infer_missing_side(
                                    diagnostic,
                                    expected_left_port=expected_left_port,
                                    expected_right_port=expected_right_port,
                                )
                                if missing_side == "left":
                                    last_error = "Left CoinFT is not active; check the higher-numbered Teensy UART side wiring/power."
                                elif missing_side == "right":
                                    last_error = "Right CoinFT is not active; check the lower-numbered Teensy UART side wiring/power."
                                elif missing_side == "both":
                                    last_error = "No CoinFT UART is active; check CoinFT power, ground, and Teensy UART wiring."
                                elif missing_side == "unknown_one_side":
                                    last_error = "Only one CoinFT UART is active, but expected left/right ports are not known yet."
                        if consecutive_timeout_count >= REOPEN_TIMEOUTS and consecutive_timeout_count % REOPEN_TIMEOUTS == 0:
                            stream_reopen_count += 1
                            recovery_message = self.recover_stream(
                                ser,
                                f"CoinFT stream timed out {consecutive_timeout_count} times",
                            )
                            status = "recovering"
                        elif consecutive_timeout_count >= RECOVERY_TIMEOUTS and consecutive_timeout_count % RECOVERY_TIMEOUTS == 0:
                            recovery_message = self.recover_stream(
                                ser,
                                f"CoinFT stream timed out {consecutive_timeout_count} times",
                            )
                            status = "recovering"
                        if consecutive_timeout_count % 3 == 0:
                            self.update_teensy_state(
                                {
                                    "status": status,
                                    "timeout_count": timeout_count,
                                    "consecutive_timeout_count": consecutive_timeout_count,
                                    "recovery_count": self.recovery_count,
                                    "stream_reopen_count": stream_reopen_count,
                                    "last_error": last_error,
                                    "recovery_message": recovery_message,
                                    "expected_left_port": expected_left_port,
                                    "expected_right_port": expected_right_port,
                                    "diagnostic": last_diagnostic,
                                    "active_ports": last_diagnostic.get("active_ports") if isinstance(last_diagnostic, dict) else None,
                                    "missing_side": missing_side,
                                }
                            )
                            self.backend.refresh_preflight_state()
                        continue

                    host_receive_monotonic_ns = time.monotonic_ns()
                    host_receive_unix_ns = time.time_ns()
                    consecutive_timeout_count = 0
                    missing_side = None
                    try:
                        sequence_id, teensy_time_us, left, right = unpack_packet(packet)
                    except ValueError as exc:
                        self.update_teensy_state({"last_error": str(exc), "status": "packet_error"})
                        continue

                    if first_teensy_time_us is None:
                        first_teensy_time_us = teensy_time_us
                    last_teensy_time_us = teensy_time_us

                    gap, out_of_order = estimate_sequence_gap(last_sequence, sequence_id)
                    # A 0x00 0x00 sync word can occur inside sensor payloads.
                    # If that happens, discard the bogus continuity estimate
                    # and restart the rate window at this valid packet instead
                    # of accumulating million-sample gaps and a false FPS.
                    if out_of_order:
                        dropped_sequence_count += 1
                        first_teensy_time_us = teensy_time_us
                        last_teensy_time_us = teensy_time_us
                        last_sequence = sequence_id
                        continue
                    last_sequence = sequence_id
                    skipped_byte_count += skipped
                    dropped_sequence_count += gap
                    out_of_order_sequence_count += int(out_of_order)
                    row = packet_row(
                        packet_index=live_sample_count,
                        host_receive_monotonic_ns=host_receive_monotonic_ns,
                        host_receive_unix_ns=host_receive_unix_ns,
                        sequence_id=sequence_id,
                        teensy_time_us=teensy_time_us,
                        left=left,
                        right=right,
                    )
                    calibration_result: dict[str, Any] = {}
                    if calibration_runtime_error:
                        calibration_result = {
                            "calibration_status": "error",
                            "calibration_error": calibration_runtime_error,
                        }
                    elif calibration_runtime is not None:
                        calibration_result = calibration_runtime.process(
                            left,
                            right,
                            timestamp_s=host_receive_monotonic_ns / 1e9,
                        )
                    if include_calibration:
                        row.update(calibration_result_csv_fields(calibration_result))
                    payload = {"row": row, "raw_packet": packet}
                    coinft_buffer.append(
                        timestamp_ns=host_receive_monotonic_ns,
                        unix_ns=host_receive_unix_ns,
                        sequence_id=sequence_id,
                        payload=payload,
                    )

                    recording_window = self.backend.recording_window_snapshot()
                    recording_active = bool(recording_window.get("active"))
                    episode_index = int(recording_window.get("episode_index") or 0)
                    if recording_active and (writer is None or active_episode_index != episode_index):
                        close_writers()
                        active_episode_index = episode_index
                        episode_recorded_count = 0
                        persisted_sequences = set()
                        output_dir = self.episode_output_dir(episode_index)
                        try:
                            raw_file, csv_file, raw_path, csv_path = open_exclusive_episode_files(output_dir)
                        except Exception as exc:  # noqa: BLE001
                            close_writers()
                            active_episode_index = None
                            raw_path = None
                            csv_path = None
                            reason = f"refusing to overwrite episode {episode_index}: {type(exc).__name__}: {exc}"
                            self.update_teensy_state(
                                {
                                    "recording": False,
                                    "status": "recording_error",
                                    "last_error": reason,
                                    "output_csv": None,
                                    "output_raw_packets": None,
                                }
                            )
                            self.backend.log(f"CoinFT {reason}")
                            fail_writer = getattr(self.backend, "fail_active_recording_writer", None)
                            if callable(fail_writer):
                                fail_writer("CoinFT", episode_index, reason)
                            continue
                        writer = csv.DictWriter(csv_file, fieldnames=csv_fieldnames(include_calibration))
                        writer.writeheader()
                        with self.backend.lock:
                            run_dir = self.backend.state.get("run_dir")
                        if run_dir:
                            register_artifact(
                                Path(str(run_dir)),
                                stream="coinft",
                                role=f"raw_episode_{episode_index:06d}",
                                path=output_dir,
                                metadata={
                                    "kind": "directory",
                                    "source": "coinft_serial",
                                    "serial_port": port,
                                    "baud": self.baud,
                                    "packet_len": PACKET_LEN,
                                    "episode_index": episode_index,
                                    "trigger": "iphone_recording_event",
                                    "recording_preroll_s": recording_window.get("recording_preroll_s"),
                                    "coinft_calibration": self.calibration_config.to_manifest_dict(),
                                },
                            )
                            if calibration_runtime is not None:
                                calibration_path = output_dir / "coinft_calibration_runtime.json"
                                calibration_path.write_text(
                                    json.dumps(calibration_runtime.runtime_metadata(), indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8",
                                )
                                register_artifact(
                                    Path(str(run_dir)),
                                    stream="coinft",
                                    role=f"calibration_runtime_episode_{episode_index:06d}",
                                    path=calibration_path,
                                    metadata={
                                        "kind": "file",
                                        "source": "coinft_serial",
                                        "episode_index": episode_index,
                                    },
                                )
                        start_ns = preroll_start_ns(
                            recording_window.get("event_start_aligned_monotonic_ns")
                            if isinstance(recording_window.get("event_start_aligned_monotonic_ns"), int)
                            else host_receive_monotonic_ns,
                            float(recording_window.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S)),
                        )
                        flushed = 0
                        for sample in coinft_buffer.slice_window(start_ns=start_ns):
                            if isinstance(sample.payload, dict) and write_sample(sample.payload):
                                flushed += 1
                        if csv_file is not None:
                            csv_file.flush()
                        if raw_file is not None:
                            raw_file.flush()
                        self.backend.log(f"CoinFT recording opened episode {episode_index} at {output_dir}")
                        self.backend.update_buffer_state("teensy", coinft_buffer.stats(), {"last_preroll_flushed": flushed})

                    if not recording_active and writer is not None:
                        close_writers()
                    if recording_active and writer is not None:
                        write_sample(payload)
                        if recorded_sample_count % 20 == 0:
                            if csv_file is not None:
                                csv_file.flush()
                            if raw_file is not None:
                                raw_file.flush()

                    live_sample_count += 1
                    packet_index = live_sample_count - 1
                    host_elapsed = max(0.001, time.monotonic() - started)
                    device_elapsed = None
                    if first_teensy_time_us is not None and last_teensy_time_us is not None:
                        delta_us = (last_teensy_time_us - first_teensy_time_us) & 0xFFFFFFFF
                        if delta_us >= 1000:
                            device_elapsed = delta_us / 1_000_000.0
                    elapsed = device_elapsed or host_elapsed
                    left_values = [row[f"left_c{i}"] for i in range(1, COINFT_CHANNELS + 1)]
                    right_values = [row[f"right_c{i}"] for i in range(1, COINFT_CHANNELS + 1)]
                    left_mean = sum(left_values) / len(left_values)
                    right_mean = sum(right_values) / len(right_values)
                    raw_12ch = [left_values, right_values]
                    raw_mean = [left_mean, right_mean]
                    dual_wrench = dual_wrench_from_result(calibration_result)
                    left_wrench = dual_wrench[0] if dual_wrench is not None else None
                    right_wrench = dual_wrench[1] if dual_wrench is not None else None
                    latest = {
                        "host_receive_monotonic_ns": host_receive_monotonic_ns,
                        "host_receive_unix_ns": host_receive_unix_ns,
                        "sequence_id": sequence_id,
                        "packet_index": packet_index,
                        "side_order": list(SIDES),
                        "wrench_axes": list(WRENCH_AXES),
                        "raw_12ch": raw_12ch,
                        "raw_mean": raw_mean,
                        "wrench": dual_wrench,
                    }
                    state = {
                        "running": True,
                        "source": "coinft_serial",
                        "recording": recording_active,
                        "serial_port": port,
                        "baud": self.baud,
                        "status": "streaming",
                        "live_sample_count": live_sample_count,
                        "recorded_sample_count": recorded_sample_count,
                        "fps": (max(0, live_sample_count - 1) / elapsed),
                        "left_mean": left_mean,
                        "right_mean": right_mean,
                        "side_order": list(SIDES),
                        "wrench_axes": list(WRENCH_AXES),
                        "raw_12ch": raw_12ch,
                        "raw_mean": raw_mean,
                        "wrench": dual_wrench,
                        "latest": latest,
                        "output_csv": str(csv_path) if csv_path else None,
                        "output_raw_packets": str(raw_path) if raw_path else None,
                        "latest_sample_monotonic_ns": host_receive_monotonic_ns,
                        "timeout_count": timeout_count,
                        "consecutive_timeout_count": consecutive_timeout_count,
                        "recovery_count": self.recovery_count,
                        "stream_reopen_count": stream_reopen_count,
                        "recovery_message": None,
                        "expected_left_port": expected_left_port,
                        "expected_right_port": expected_right_port,
                        "diagnostic": last_diagnostic,
                        "active_ports": last_diagnostic.get("active_ports") if isinstance(last_diagnostic, dict) else None,
                        "missing_side": missing_side,
                        "skipped_byte_count": skipped_byte_count,
                        "dropped_sequence_count": dropped_sequence_count,
                        "out_of_order_sequence_count": out_of_order_sequence_count,
                        "last_sequence_id": sequence_id,
                        "last_error": None,
                        **calibration_status_summary(self.calibration_config),
                        "calibration_status": calibration_result.get(
                            "calibration_status",
                            "error" if calibration_runtime_error else ("raw_only" if not self.calibration_config.calibrated else "unknown"),
                        ),
                        "calibration_ready": bool(
                            calibration_result.get("calibration_ready", not self.calibration_config.calibrated)
                        ),
                        "calibration_error": calibration_result.get("calibration_error") or calibration_runtime_error,
                        "stability_passes": calibration_result.get("stability_passes"),
                        "stability_required_passes": calibration_result.get("stability_required_passes"),
                        "stability_mean_delta_counts": calibration_result.get("stability_mean_delta_counts"),
                        "stability_noise_std_counts": calibration_result.get("stability_noise_std_counts"),
                        "zero_force_max_abs_n": calibration_result.get("zero_force_max_abs_n"),
                        "zero_force_limit_n": calibration_result.get("zero_force_limit_n"),
                    }
                    if left_wrench is not None:
                        state["left_wrench"] = left_wrench
                    if right_wrench is not None:
                        state["right_wrench"] = right_wrench
                    sample = {
                        "packet_index": packet_index,
                        "host_receive_monotonic_ns": host_receive_monotonic_ns,
                        "sequence_id": sequence_id,
                        "left": left_values,
                        "right": right_values,
                        "side_order": list(SIDES),
                        "wrench_axes": list(WRENCH_AXES),
                        "wrench": dual_wrench,
                        "left_wrench": left_wrench,
                        "right_wrench": right_wrench,
                        "raw_mean": raw_mean,
                        "calibration_status": state.get("calibration_status"),
                        "calibration_ready": state.get("calibration_ready"),
                        "zero_force_max_abs_n": state.get("zero_force_max_abs_n"),
                        "zero_force_limit_n": state.get("zero_force_limit_n"),
                        "left_mean": left_mean,
                        "right_mean": right_mean,
                        "fps": state["fps"],
                    }
                    if hasattr(self.backend, "publish_coinft_sample"):
                        self.backend.publish_coinft_sample(state, sample)
                    else:
                        with self.backend.lock:
                            self.backend.state["teensy"] = state
                            snapshot = dict(self.backend.state)
                        self.backend.broadcast({"type": "state", "state": snapshot})
                        self.backend.broadcast({"type": "teensy_sample", "sample": sample})
                    # Gate freshness has a one-second threshold; recomputing it
                    # for every ten high-rate force samples only floods the GUI.
                    if live_sample_count % 50 == 0:
                        self.backend.refresh_preflight_state()
                    if live_sample_count % 30 == 0:
                        self.backend.update_buffer_state("teensy", coinft_buffer.stats())
                self.stop_stream(ser)
        except Exception as exc:  # noqa: BLE001
            self.update_teensy_state(
                {
                    "running": False,
                    "source": "coinft_serial",
                    "serial_port": port,
                    "last_error": str(exc),
                    "status": "error",
                }
            )
            self.backend.log(f"CoinFT serial error: {exc}")
        finally:
            close_writers()
            self.backend.teensy_finished()
