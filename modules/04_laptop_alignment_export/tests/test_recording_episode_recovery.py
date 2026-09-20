from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
import numpy as np
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from umift_laptop_alignment.app.receiver_web_gui import (
    COINFT_GUI_PUBLISH_INTERVAL_S,
    EPISODE_INDEX_STATE_FILE,
    HTML,
    ReceiverBackend,
    ServerConfig,
    d435_recording_start_timeout_s,
    episode_indices_on_disk,
    reserve_next_episode_index,
)
from umift_laptop_alignment.capture.receivers.coinft.serial_worker import (
    open_exclusive_episode_files,
)
from umift_laptop_alignment.capture.receivers.coinft.calibration import CoinFTCalibrationConfig
from umift_laptop_alignment.capture.receivers.d435.capture_d435i import (
    D435ContinuousFrameAcquirer,
    open_recording_session,
    restart_idle_stream_pipeline,
    start_stream_pipeline_with_recovery,
    write_recording_frame,
)
from umift_laptop_alignment.orchestration.datasets import initialize_dataset_metadata
from umift_laptop_alignment.orchestration.run_layout import create_run


def backend_for(run_dir: Path) -> ReceiverBackend:
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        d435_preview_dir=run_dir / "preview",
        output_root=run_dir / "iphone",
        runs_root=run_dir.parent,
        auto_start=False,
        coinft_source="serial",
        coinft_port="",
        coinft_baud=115200,
        coinft_simulate=False,
        coinft_tare=False,
        coinft_config="",
    )
    backend = ReceiverBackend(config)
    backend.patch_state(run_dir=str(run_dir), run_id=run_dir.name)
    backend.observe_recording_transition_for_live_zarr = lambda transition: None
    return backend


class RecordingEpisodeRecoveryTest(unittest.TestCase):
    @staticmethod
    def fake_d435_pipeline(frame_count: int):
        class FakeFrame:
            def __init__(self, frame_number: int, *, depth: bool = False) -> None:
                self.frame_number = frame_number
                self.data = (
                    np.zeros((2, 2), dtype="uint16")
                    if depth
                    else np.zeros((2, 2, 3), dtype="uint8")
                )

            def get_data(self):
                return self.data

            def get_timestamp(self) -> float:
                return self.frame_number * 33.333

            def get_frame_number(self) -> int:
                return self.frame_number

            def get_frame_timestamp_domain(self) -> str:
                return "fake_global_time"

            def supports_frame_metadata(self, _key) -> bool:
                return False

        class FakeFrameSet:
            def __init__(self, frame_number: int) -> None:
                self.color = FakeFrame(frame_number)
                self.depth = FakeFrame(frame_number, depth=True)

            def get_color_frame(self):
                return self.color

            def get_depth_frame(self):
                return self.depth

        class FakePipeline:
            def __init__(self) -> None:
                self.next_frame = 1
                self.stop_count = 0

            def wait_for_frames(self, *, timeout_ms: int):
                if self.next_frame > frame_count:
                    raise RuntimeError("fake stream exhausted")
                result = FakeFrameSet(self.next_frame)
                self.next_frame += 1
                return result

            def stop(self) -> None:
                self.stop_count += 1

        return FakePipeline()

    def test_d435_acquisition_continues_while_downstream_is_slow(self) -> None:
        pipeline = self.fake_d435_pipeline(20)
        fake_rs = SimpleNamespace(
            frame_metadata_value=SimpleNamespace(sensor_timestamp="sensor_timestamp")
        )
        with patch(
            "umift_laptop_alignment.capture.receivers.d435.capture_d435i.rs",
            fake_rs,
        ):
            acquirer = D435ContinuousFrameAcquirer(
                pipeline=pipeline,
                frame_timeout_ms=20,
                queue_capacity=25,
            )
            acquirer.start()
            deadline = time.monotonic() + 1.0
            while acquirer.acquired_frame_count < 20 and time.monotonic() < deadline:
                time.sleep(0.005)

            # Simulates a slow encoder/preroll/preview consumer: acquisition
            # has already drained every camera frame before consumption starts.
            self.assertEqual(acquirer.acquired_frame_count, 20)
            rows = [acquirer.get(0.1) for _ in range(20)]
            self.assertEqual(
                [row.metadata["color_frame_number"] for row in rows],
                list(range(1, 21)),
            )
            self.assertEqual(acquirer.snapshot()["maxQueueDepth"], 20)
            acquirer.stop()

    def test_d435_queue_overflow_is_a_hard_error(self) -> None:
        pipeline = self.fake_d435_pipeline(20)
        fake_rs = SimpleNamespace(
            frame_metadata_value=SimpleNamespace(sensor_timestamp="sensor_timestamp")
        )
        with patch(
            "umift_laptop_alignment.capture.receivers.d435.capture_d435i.rs",
            fake_rs,
        ):
            acquirer = D435ContinuousFrameAcquirer(
                pipeline=pipeline,
                frame_timeout_ms=20,
                queue_capacity=2,
            )
            acquirer.start()
            deadline = time.monotonic() + 1.0
            while acquirer.error is None and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertIsInstance(acquirer.error, RuntimeError)
            self.assertIn("refusing silent frame loss", str(acquirer.error))
            self.assertEqual(acquirer.snapshot()["queueDepth"], 2)
            acquirer.stop()

    def test_d435_sensor_frame_gap_is_recorded_for_quality_gate(self) -> None:
        class FakeRgbWriter:
            def write(self, _image) -> None:
                return

        class FakeCsvWriter:
            def __init__(self) -> None:
                self.rows = []

            def writerow(self, row) -> None:
                self.rows.append(row)

        csv_writer = FakeCsvWriter()
        with tempfile.TemporaryFile() as depth_file:
            session = {
                "frame_index": 0,
                "rgb_writer": FakeRgbWriter(),
                "depth_file": depth_file,
                "csv_writer": csv_writer,
            }
            image = np.zeros((2, 2), dtype="uint16")
            for frame_number in (10, 11, 13):
                write_recording_frame(
                    args=SimpleNamespace(),
                    session=session,
                    color_frame=None,
                    depth_frame=None,
                    color_image=np.zeros((2, 2, 3), dtype="uint8"),
                    depth_image=image,
                    host_receive_monotonic_ns=frame_number,
                    host_receive_time_s=float(frame_number),
                    frame_metadata={
                        "color_frame_number": frame_number,
                        "depth_frame_number": frame_number,
                    },
                )

            self.assertEqual(session["frame_index"], 3)
            self.assertEqual(session["sensor_integrity"]["color_missing_frames"], 1)
            self.assertEqual(session["sensor_integrity"]["depth_missing_frames"], 1)
            self.assertEqual(len(csv_writer.rows), 3)

    def test_postprocess_alignment_quality_updates_top_level_sync_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = backend_for(Path(tmpdir) / "run_test")
            backend.update_postprocess_state({
                "status": "ok",
                "alignment_quality": {
                    "ok": True,
                    "reasons": [],
                    "ratios": {"all_valid": 0.91},
                },
            })

            quality = backend.state_snapshot()["sync_quality"]

        self.assertTrue(quality["ok"])
        self.assertEqual(quality["status"], "ready")
        self.assertEqual(quality["ratios"]["all_valid"], 0.91)
        self.assertIsInstance(quality["checked_at_unix_ns"], int)

    def test_gui_coinft_model_paths_are_used_without_writing_source_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run_test"
            model_dir = root / "selected_models"
            model_dir.mkdir()
            norm = {
                "mu_x": [0.0] * 12,
                "sd_x": [1.0] * 12,
                "mu_y": [0.0] * 6,
                "sd_y": [1.0] * 6,
            }
            paths: dict[str, str] = {}
            for side in ("left", "right"):
                model = model_dir / f"{side}_MLP.onnx"
                norm_path = model_dir / f"{side}_norm.json"
                model.write_bytes(b"test-onnx")
                norm_path.write_text(json.dumps(norm), encoding="utf-8")
                paths[f"{side}_model_path"] = str(model)
                paths[f"{side}_norm_path"] = str(norm_path)

            backend = backend_for(run_dir)
            result = backend.activate_coinft_model_folder({**paths, "model_set_id": "gui-runtime"})

            self.assertEqual(result["model_set_id"], "gui-runtime")
            self.assertEqual(backend.coinft_calibration_config.mode, "calibrated")
            self.assertEqual(
                backend.coinft_calibration_config.slot("left").model_path,
                Path(paths["left_model_path"]).resolve(),
            )
            self.assertFalse((root / "models" / "current").exists())

    def test_coinft_gui_sample_cadence_matches_200_hz_collection(self) -> None:
        self.assertAlmostEqual(COINFT_GUI_PUBLISH_INTERVAL_S, 1.0 / 200.0)

    def test_recording_gate_blocks_until_coinft_startup_zeroing_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = backend_for(Path(tmpdir) / "run_test")
            backend.coinft_calibration_config = CoinFTCalibrationConfig(mode="calibrated", slots={})
            now_ns = time.monotonic_ns()
            backend.d435_stream_snapshot = lambda **kwargs: {
                "ok": True,
                "running": True,
                "files_fresh": True,
                "rgb_age_ms": 1.0,
                "depth_age_ms": 1.0,
                "last_error": None,
            }
            backend.patch_state(
                running=True,
                stats={"frame_count": 1},
                alignment={"status": "receiver_aligned"},
                latest_iphone_sample_monotonic_ns=now_ns,
                latest_iphone_pose={"world_calibrated": True, "valid": True},
                recording_control={"armed": True, "active": False},
                teensy={
                    "running": True,
                    "live_sample_count": 100,
                    "latest_sample_monotonic_ns": now_ns,
                    "calibration_status": "runtime_settling",
                    "calibration_ready": False,
                    "last_error": None,
                    "missing_side": None,
                },
            )

            gate = backend.check_recording_gate()

        self.assertFalse(gate["ok"])
        self.assertFalse(gate["streams"]["teensy"]["ok"])
        self.assertIn("CoinFT startup zeroing is not ready (runtime_settling)", gate["reasons"])

    def test_iphone_preview_recovers_after_buffer_eviction_and_generation_reset(self) -> None:
        self.assertIn(
            'iphoneSourceBuffer.addEventListener("updateend", pumpIphoneAppendQueue)',
            HTML,
        )
        self.assertIn(
            "manifest.latest_fragment_index < iphoneLastFragmentIndex",
            HTML,
        )
        self.assertIn("error.httpStatus === 404", HTML)
        self.assertIn("Resyncing iPhone H.264 preview", HTML)

    def test_coinft_restart_stops_and_starts_only_coinft_worker(self) -> None:
        class FinishedWorker:
            def __init__(self) -> None:
                self.join_timeouts: list[float] = []

            def join(self, timeout: float) -> None:
                self.join_timeouts.append(timeout)

            def is_alive(self) -> bool:
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = backend_for(Path(tmpdir) / "run_test")
            worker = FinishedWorker()
            calls: list[str] = []
            backend.teensy_worker = worker
            backend.teensy_stop_event = threading.Event()
            backend.stop_teensy = lambda: calls.append("stop_coinft") or True
            backend.start_coinft = lambda: calls.append("start_coinft") or True

            result = backend.restart_coinft(stop_timeout_s=0.1)

            self.assertEqual(calls, ["stop_coinft", "start_coinft"])
            self.assertEqual(worker.join_timeouts, [0.1])
            self.assertTrue(result["ok"])
            self.assertFalse(backend.state_snapshot()["recording_control"]["active"])

    def test_coinft_restart_event_only_restarts_coinft(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            backend = backend_for(run_dir)
            calls: list[dict[str, object]] = []
            backend.restart_coinft = lambda: calls.append({"restart": True}) or {
                "ok": True,
                "stopped": True,
                "started": True,
                "reason": None,
            }

            persisted = backend.handle_iphone_recording_event(
                {"code": "coinft_restart_requested", "index": 4, "reason": "record_countdown_requested"},
                {"timestamp": {"aligned_monotonic_ns": 100, "aligned_unix_ns": 200}},
            )

            self.assertFalse(persisted)
            self.assertEqual(calls, [{"restart": True}])
            control = backend.state_snapshot()["recording_control"]
            self.assertFalse(control["active"])
            self.assertIsNone(control["episode_index"])

    def test_new_iphone_session_aborts_stale_active_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = backend_for(Path(tmpdir) / "run_test")
            backend.patch_state(
                session_id="old_session",
                recording_control={
                    "armed": True,
                    "active": True,
                    "episode_index": 4,
                    "last_event": "record_start_committed",
                    "blocked": False,
                    "blocked_reason": None,
                    "source": "iphone_recording_event",
                },
            )
            with patch.object(backend.iphone_event_executor, "submit") as submit:
                episode_index = backend.handle_iphone_source_session_change("new_session")

            self.assertEqual(episode_index, 4)
            control = backend.state_snapshot()["recording_control"]
            self.assertFalse(control["active"])
            self.assertEqual(control["last_event"], "source_session_lost")
            self.assertTrue(control["blocked"])
            self.assertIn("old_session", control["blocked_reason"])
            submit.assert_called_once()

    def test_restart_allocates_after_existing_sensor_episode_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            (run_dir / "raw/global_camera/episode_000000").mkdir(parents=True)
            (run_dir / "raw/coinft/episode_000001").mkdir(parents=True)

            self.assertEqual(episode_indices_on_disk(run_dir), {0, 1})
            self.assertEqual(reserve_next_episode_index(run_dir, requested_index=0), 2)

            state = json.loads((run_dir / "raw" / EPISODE_INDEX_STATE_FILE).read_text())
            self.assertEqual(state["last_reserved_episode_index"], 2)
            self.assertEqual(state["next_episode_index"], 3)

    def test_failed_reserved_episode_is_not_reused_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            first = reserve_next_episode_index(run_dir)
            second = reserve_next_episode_index(run_dir)
            self.assertEqual((first, second), (0, 1))

    def test_d435_start_failure_keeps_recording_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            (run_dir / "raw/global_camera/episode_000000").mkdir(parents=True)
            (run_dir / "raw/coinft/episode_000001").mkdir(parents=True)
            backend = backend_for(run_dir)
            backend.check_recording_gate = lambda: {
                "ok": True,
                "status": "ready",
                "reasons": [],
                "streams": {},
            }
            backend.start_d435_recording = lambda overrides=None: False

            accepted = backend.handle_iphone_recording_event(
                {"code": "record_start", "index": None},
                {
                    "timestamp": {
                        "aligned_monotonic_ns": 100,
                        "aligned_unix_ns": 200,
                    }
                },
            )

            self.assertFalse(accepted)
            control = backend.state_snapshot()["recording_control"]
            self.assertFalse(control["active"])
            self.assertEqual(control["episode_index"], 2)
            self.assertEqual(control["last_event"], "record_start_rejected")

    def test_record_start_never_initializes_d435_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.ensure_active_run = lambda *args, **kwargs: run_dir
            backend.d435_stream_snapshot = lambda **kwargs: {
                "running": False,
                "ok": False,
            }
            start_calls = []
            backend.start_d435_preview = lambda: start_calls.append(True) or True

            started = backend.start_d435_recording({"episode_index": 4})

            self.assertFalse(started)
            self.assertEqual(start_calls, [])
            self.assertIn(
                "not initialized during Arm All",
                backend.state_snapshot()["logs"][-1]["text"],
            )

    def test_d435_recording_start_wait_covers_writer_setup_and_preroll(self) -> None:
        self.assertEqual(d435_recording_start_timeout_s(1.5), 6.0)
        self.assertEqual(d435_recording_start_timeout_s(4.0), 11.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.ensure_active_run = lambda *args, **kwargs: run_dir
            backend.d435_stream_snapshot = lambda **kwargs: {
                "running": True,
                "pid": 123,
            }
            backend.d435_control_request = lambda *args, **kwargs: "start-request"
            observed_timeouts: list[float] = []

            def acknowledge(request_id, *, recording_active, timeout_s):
                observed_timeouts.append(timeout_s)
                return {
                    "lastRequestId": request_id,
                    "recordingActive": recording_active,
                    "lastError": None,
                }

            backend.wait_for_d435_request = acknowledge
            with patch(
                "umift_laptop_alignment.app.receiver_web_gui.register_artifact"
            ):
                started = backend.start_d435_recording(
                    {"episode_index": 4, "recording_preroll_s": 1.5}
                )

            self.assertTrue(started)
            self.assertEqual(observed_timeouts, [6.0])

    def test_recording_becomes_active_only_after_d435_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            backend = backend_for(run_dir)
            active_during_d435_start: list[bool] = []
            backend.check_recording_gate = lambda: {
                "ok": True,
                "status": "ready",
                "reasons": [],
                "streams": {},
            }

            def start_d435(overrides=None):
                active_during_d435_start.append(
                    bool(backend.state_snapshot()["recording_control"]["active"])
                )
                return True

            backend.start_d435_recording = start_d435
            backend.schedule_post_start_gate_check = lambda **kwargs: None

            accepted = backend.handle_iphone_recording_event(
                {"code": "record_start", "index": None},
                {
                    "timestamp": {
                        "aligned_monotonic_ns": 100,
                        "aligned_unix_ns": 200,
                    }
                },
            )

            self.assertTrue(accepted)
            self.assertEqual(active_during_d435_start, [False])
            self.assertTrue(backend.state_snapshot()["recording_control"]["active"])

    def test_discard_removes_current_episode_and_releases_its_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            backend = backend_for(run_dir)
            backend.check_recording_gate = lambda: {
                "ok": True,
                "status": "ready",
                "reasons": [],
                "streams": {},
            }
            backend.start_d435_recording = lambda overrides=None: True
            backend.stop_d435_recording = lambda: True
            backend.schedule_post_start_gate_check = lambda **kwargs: None
            backend.refresh_preflight_state = lambda: {}

            self.assertTrue(
                backend.handle_iphone_recording_event(
                    {"code": "record_start", "index": None},
                    {"timestamp": {"aligned_monotonic_ns": 100, "aligned_unix_ns": 200}},
                )
            )
            episode_index = backend.recording_episode_index()
            self.assertEqual(episode_index, 0)
            for relative in (
                "raw/global_camera/episode_000000",
                "raw/coinft/episode_000000",
                "pipeline/live_zarr_rows/episode_000000",
            ):
                path = run_dir / relative
                path.mkdir(parents=True)
                (path / "raw.bin").write_bytes(b"discard")

            self.assertFalse(
                backend.handle_iphone_recording_event(
                    {"code": "record_discard", "index": 1},
                    {"timestamp": {"aligned_monotonic_ns": 300, "aligned_unix_ns": 400}},
                )
            )
            self.assertTrue(backend.state_snapshot()["recording_control"]["discard_pending"])

            self.assertTrue(
                backend.complete_iphone_recording_discard(
                    run_dir=run_dir,
                    episode_index=episode_index,
                    iphone_cleanup={"status": "rolled_back", "removed_payload_files": 2, "rolled_back_rows": 9},
                )
            )
            self.assertFalse((run_dir / "raw/global_camera/episode_000000").exists())
            self.assertFalse((run_dir / "raw/coinft/episode_000000").exists())
            self.assertFalse((run_dir / "pipeline/live_zarr_rows/episode_000000").exists())
            state = json.loads((run_dir / "raw" / EPISODE_INDEX_STATE_FILE).read_text())
            self.assertEqual(state["next_episode_index"], 0)
            control = backend.state_snapshot()["recording_control"]
            self.assertIsNone(control["episode_index"])
            self.assertEqual(control["last_event"], "record_discard_committed")

    def test_coinft_exclusive_open_preserves_existing_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "episode_000000"
            output_dir.mkdir()
            csv_path = output_dir / "raw_coinft_stream.csv"
            csv_path.write_text("original\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                open_exclusive_episode_files(output_dir)

            self.assertEqual(csv_path.read_text(encoding="utf-8"), "original\n")

    def test_d435_exclusive_open_rejects_existing_episode_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "episode_000000"
            output_dir.mkdir()
            marker = output_dir / "rgb.mp4"
            marker.write_bytes(b"original")

            with self.assertRaises(FileExistsError):
                open_recording_session(
                    args=None,
                    device={},
                    profile=None,
                    output_dir=output_dir,
                    control_request={},
                )

            self.assertEqual(marker.read_bytes(), b"original")

    def test_d435_preview_uses_control_script_managed_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            control_script = run_dir / "control_capture_macos.sh"
            control_script.parent.mkdir(parents=True)
            control_script.touch()
            status_file = run_dir / "d435_status.json"
            backend = backend_for(run_dir)
            backend.d435_stream_snapshot = lambda **kwargs: {"running": False}
            backend.refresh_preflight_state = lambda: {}

            completed = [
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="started pid=12345\n", stderr=""),
            ]
            with (
                patch(
                    "umift_laptop_alignment.app.receiver_web_gui.INSTALLED_D435_CONTROL_SCRIPT",
                    control_script,
                ),
                patch(
                    "umift_laptop_alignment.app.receiver_web_gui.D435_STREAM_STATUS_FILE",
                    status_file,
                ),
                patch(
                    "umift_laptop_alignment.app.receiver_web_gui.subprocess.run",
                    side_effect=completed,
                ) as run_mock,
            ):
                started = backend.start_d435_preview()

            self.assertTrue(started)
            self.assertEqual(run_mock.call_args_list[-1].args[0][3], "start")
            self.assertIn("--stream", run_mock.call_args_list[-1].args[0])
            d435 = backend.state_snapshot()["d435"]
            self.assertEqual(d435["pid"], 12345)
            self.assertTrue(d435["managed_by_control_script"])

    def test_d435_preview_stop_calls_privileged_control_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            backend = backend_for(run_dir)
            backend.patch_state(d435={"running": True, "managed_by_control_script": True})
            backend.d435_control_request = lambda command: "stop_request"
            backend.wait_for_d435_request = lambda *args, **kwargs: {}
            backend.refresh_preflight_state = lambda: {}
            result = subprocess.CompletedProcess([], 0, stdout="stopped pid=12345\n", stderr="")
            with patch(
                "umift_laptop_alignment.app.receiver_web_gui.subprocess.run",
                return_value=result,
            ) as run_mock:
                stopped = backend.stop_d435_preview()

            self.assertTrue(stopped)
            self.assertEqual(run_mock.call_args.args[0][-1], "stop")

    def test_fresh_preview_files_do_not_make_stopped_d435_look_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.config.d435_preview_dir.mkdir(parents=True)
            (backend.config.d435_preview_dir / "latest_rgb.jpg").write_bytes(b"rgb")
            (backend.config.d435_preview_dir / "latest_depth.png").write_bytes(b"depth")
            missing_status = run_dir / "missing_status.json"

            with patch(
                "umift_laptop_alignment.app.receiver_web_gui.D435_STREAM_STATUS_FILE",
                missing_status,
            ):
                snapshot = backend.d435_stream_snapshot(update_state=False)

            self.assertTrue(snapshot["files_fresh"])
            self.assertFalse(snapshot["running"])
            self.assertFalse(snapshot["ok"])

    def test_fresh_d435_status_clears_stale_recording_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.config.d435_preview_dir.mkdir(parents=True)
            (backend.config.d435_preview_dir / "latest_rgb.jpg").write_bytes(b"rgb")
            (backend.config.d435_preview_dir / "latest_depth.png").write_bytes(b"depth")
            status_file = run_dir / "d435_status.json"
            status_file.write_text(
                json.dumps({"running": True, "recordingActive": False, "lastError": None}),
                encoding="utf-8",
            )
            backend.patch_state(d435_recording={"running": True, "pid": 12345})

            with patch(
                "umift_laptop_alignment.app.receiver_web_gui.D435_STREAM_STATUS_FILE",
                status_file,
            ):
                snapshot = backend.d435_stream_snapshot(update_state=True)

            self.assertFalse(snapshot["recording_active"])
            self.assertFalse(backend.state_snapshot()["d435_recording"]["running"])

    def test_arm_waits_for_inflight_stop_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            stop_entered = threading.Event()
            release_stop = threading.Event()
            operations: list[str] = []

            def stop_receiver() -> bool:
                operations.append("stop_receiver_begin")
                stop_entered.set()
                release_stop.wait(timeout=2.0)
                operations.append("stop_receiver_end")
                return True

            backend.stop_receiver = stop_receiver
            backend.stop_teensy = lambda: operations.append("stop_coinft") or True
            backend.stop_d435_preview = lambda: operations.append("stop_d435") or True
            backend.ensure_active_run = lambda *args, **kwargs: run_dir
            backend.start_receiver = lambda *args, **kwargs: operations.append("arm_receiver") or True
            backend.start_coinft = lambda: operations.append("arm_coinft") or True
            backend.start_d435_preview = lambda: operations.append("arm_d435") or True
            backend.d435_stream_snapshot = lambda **kwargs: {"ok": True, "running": True, "last_error": None}
            backend.refresh_preflight_state = lambda: {"ok": True, "status": "ready", "reasons": [], "streams": {}}

            stop_thread = threading.Thread(target=backend.stop_capture)
            arm_thread = threading.Thread(target=backend.arm_capture, args=({"run_dir": str(run_dir)},))
            stop_thread.start()
            self.assertTrue(stop_entered.wait(timeout=1.0))
            arm_thread.start()
            time.sleep(0.05)
            self.assertNotIn("arm_receiver", operations)

            release_stop.set()
            stop_thread.join(timeout=2.0)
            arm_thread.join(timeout=2.0)

            self.assertFalse(stop_thread.is_alive())
            self.assertFalse(arm_thread.is_alive())
            self.assertLess(operations.index("stop_d435"), operations.index("arm_receiver"))

    def test_stop_capture_disarms_recording_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.patch_state(
                recording_control={
                    **backend.state_snapshot()["recording_control"],
                    "armed": True,
                    "last_event": "armed",
                }
            )
            backend.stop_receiver = lambda: True
            backend.stop_teensy = lambda: True
            backend.stop_d435_preview = lambda: True
            backend.refresh_preflight_state = lambda: {}

            result = backend.stop_capture()

            control = backend.state_snapshot()["recording_control"]
            self.assertTrue(result["d435_stopped"])
            self.assertFalse(control["armed"])
            self.assertFalse(control["active"])
            self.assertEqual(control["last_event"], "stopped")

    def test_arm_reports_healthy_existing_d435_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.ensure_active_run = lambda *args, **kwargs: run_dir
            backend.start_receiver = lambda *args, **kwargs: False
            backend.start_coinft = lambda: False
            backend.start_d435_preview = lambda: False
            backend.d435_stream_snapshot = lambda **kwargs: {"ok": True, "running": True, "last_error": None}
            backend.refresh_preflight_state = lambda: {"ok": True, "status": "ready", "reasons": [], "streams": {}}

            result = backend.arm_capture({"run_dir": str(run_dir)})

            self.assertFalse(result["d435_preview_started"])
            self.assertTrue(result["d435_ready"])
            self.assertEqual(result["d435_action"], "already_running")

    def test_arm_failure_rolls_back_armed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir()
            backend = backend_for(run_dir)
            backend.ensure_active_run = lambda *args, **kwargs: run_dir
            backend.start_receiver = lambda *args, **kwargs: True
            backend.start_coinft = lambda: True
            backend.start_d435_preview = lambda: True
            backend.d435_stream_snapshot = lambda **kwargs: {
                "ok": False,
                "running": False,
                "last_error": "Frame didn't arrive within 5000",
            }
            backend.wait_for_d435_stream_ready = lambda **kwargs: backend.d435_stream_snapshot()
            backend.refresh_preflight_state = lambda: {"ok": False, "status": "blocked", "reasons": [], "streams": {}}

            result = backend.arm_capture({"run_dir": str(run_dir)})

            control = backend.state_snapshot()["recording_control"]
            self.assertFalse(result["armed"])
            self.assertFalse(result["d435_ready"])
            self.assertEqual(result["d435_action"], "failed")
            self.assertFalse(control["armed"])
            self.assertTrue(control["blocked"])
            self.assertEqual(control["last_event"], "arm_failed")
            self.assertIn("Frame didn't arrive", control["blocked_reason"])

    def test_d435_startup_timeout_resets_and_reopens_pipeline(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.reset_count = 0

            def hardware_reset(self) -> None:
                self.reset_count += 1

        class FakeProfile:
            def __init__(self, device) -> None:
                self.device = device

            def get_device(self):
                return self.device

        class FakePipeline:
            def __init__(self, result) -> None:
                self.result = result
                self.stop_count = 0

            def wait_for_frames(self, *, timeout_ms: int):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

            def stop(self) -> None:
                self.stop_count += 1

        device = FakeDevice()
        first = FakePipeline(RuntimeError("Frame didn't arrive within 5000"))
        second = FakePipeline(object())
        attempts = iter(((first, FakeProfile(device)), (second, FakeProfile(device))))
        status_events = []
        sleeps = []

        pipeline, _, frames = start_stream_pipeline_with_recovery(
            start_attempt=lambda: next(attempts),
            frame_timeout_ms=5000,
            recovery_attempts=2,
            reset_delay_s=2.0,
            status_callback=lambda *args: status_events.append(args),
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

        self.assertIs(pipeline, second)
        self.assertIs(frames, second.result)
        self.assertEqual(first.stop_count, 1)
        self.assertEqual(device.reset_count, 1)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(status_events[0][0], 1)

    def test_idle_d435_timeout_resets_and_restarts_pipeline(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.reset_count = 0

            def hardware_reset(self) -> None:
                self.reset_count += 1

        class FakeProfile:
            def __init__(self, device) -> None:
                self.device = device

            def get_device(self):
                return self.device

        class FakePipeline:
            def __init__(self) -> None:
                self.stop_count = 0

            def stop(self) -> None:
                self.stop_count += 1

        device = FakeDevice()
        pipeline = FakePipeline()
        restarted = (object(), object(), object())
        sleeps = []

        result = restart_idle_stream_pipeline(
            pipeline=pipeline,
            profile=FakeProfile(device),
            restart_stream=lambda: restarted,
            reset_delay_s=2.0,
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )

        self.assertEqual(result, restarted)
        self.assertEqual(pipeline.stop_count, 1)
        self.assertEqual(device.reset_count, 1)
        self.assertEqual(sleeps, [2.0])

    def test_dataset_selection_is_restored_after_backend_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir) / "runs"
            seed_dir = runs_root / "seed"
            backend = backend_for(seed_dir)
            compatibility = backend.dataset_compatibility_signature()
            first = create_run(runs_root=runs_root, run_id="run_first")
            second = create_run(runs_root=runs_root, run_id="run_second")
            initialize_dataset_metadata(first.run_dir, display_name="First", task_name="first", notes="", compatibility=compatibility)
            initialize_dataset_metadata(second.run_dir, display_name="Second", task_name="second", notes="", compatibility=compatibility)

            backend.select_dataset(str(second.run_dir))
            restored = ReceiverBackend(backend.config)

            selection = restored.state_snapshot()["dataset_selection"]
            self.assertTrue(selection["selected"])
            self.assertEqual(Path(selection["run_dir"]), second.run_dir)

    def test_unique_nonlegacy_dataset_is_selected_without_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir) / "runs"
            seed_dir = runs_root / "seed"
            backend = backend_for(seed_dir)
            compatibility = backend.dataset_compatibility_signature()
            create_run(runs_root=runs_root, run_id="run_legacy")
            current = create_run(runs_root=runs_root, run_id="run_current")
            initialize_dataset_metadata(
                current.run_dir,
                display_name="Current",
                task_name="current",
                notes="",
                compatibility=compatibility,
            )

            restored = ReceiverBackend(backend.config)

            selection = restored.state_snapshot()["dataset_selection"]
            self.assertTrue(selection["selected"])
            self.assertEqual(Path(selection["run_dir"]), current.run_dir)


if __name__ == "__main__":
    unittest.main()
