from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from umift_laptop_alignment.orchestration.datasets import (
    compatibility_issues,
    dataset_summary,
    initialize_dataset_metadata,
    list_datasets,
    resolve_dataset_run,
)
from umift_laptop_alignment.orchestration.run_layout import create_run
from umift_laptop_alignment.pipeline.postprocess.orchestrator import PostprocessOrchestrator


class DatasetManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runs_root = Path(self.temp_dir.name) / "runs"
        self.compatibility = {
            "export_format": "umift-replay-buffer-zarr",
            "export_profile": "config/export_profiles/umift_replay_buffer_v0.json",
            "coinft_mode": "calibrated",
            "coinft_left_hardware": "left_0019",
            "coinft_right_hardware": "right_0012",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_dataset_catalog_uses_existing_run_layout(self) -> None:
        paths = create_run(runs_root=self.runs_root, run_id="run_test", task_name="legacy task")
        initialize_dataset_metadata(
            paths.run_dir,
            display_name="Insert USB v1",
            task_name="insert_usb",
            notes="batch one",
            compatibility=self.compatibility,
        )
        (paths.raw_dir / "global_camera" / "episode_000003").mkdir(parents=True)
        zarr = paths.exports_dir / "umift_replay_buffer_zarr" / "dataset.zarr"
        zarr.mkdir(parents=True)

        summary = dataset_summary(paths.run_dir)
        catalog = list_datasets(self.runs_root)

        self.assertEqual(summary["display_name"], "Insert USB v1")
        self.assertEqual(summary["task_name"], "insert_usb")
        self.assertEqual(summary["episode_count"], 1)
        self.assertEqual(summary["latest_source_episode_index"], 3)
        self.assertEqual(summary["zarr_count"], 1)
        self.assertEqual(catalog[0]["run_id"], "run_test")

    def test_resolve_dataset_requires_direct_child_of_runs_root(self) -> None:
        paths = create_run(runs_root=self.runs_root, run_id="run_test")
        self.assertEqual(resolve_dataset_run(self.runs_root, "run_test"), paths.run_dir)
        nested = paths.run_dir / "nested"
        nested.mkdir()
        (nested / "RUN_MANIFEST.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            resolve_dataset_run(self.runs_root, nested)

    def test_compatibility_reports_only_changed_contract_fields(self) -> None:
        current = dict(self.compatibility)
        current["coinft_right_hardware"] = "right_changed"
        issues = compatibility_issues(self.compatibility, current)
        self.assertEqual(len(issues), 1)
        self.assertIn("right CoinFT hardware changed", issues[0])


class PostprocessLockTest(unittest.TestCase):
    def test_different_episodes_in_same_dataset_can_run_in_parallel(self) -> None:
        orchestrator = PostprocessOrchestrator(object())
        guard = threading.Lock()
        active = 0
        max_active = 0

        def fake_run(run_dir: Path, *, episode_index=None):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"status": "ok", "episode_index": episode_index}

        orchestrator._run_locked = fake_run
        run_dir = Path(tempfile.gettempdir()) / "same_dataset"
        threads = [
            threading.Thread(target=orchestrator.run, args=(run_dir,), kwargs={"episode_index": index})
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active, 2)

    def test_duplicate_episode_submission_is_serialized(self) -> None:
        orchestrator = PostprocessOrchestrator(object())
        guard = threading.Lock()
        active = 0
        max_active = 0

        def fake_run(run_dir: Path, *, episode_index=None):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"status": "ok", "episode_index": episode_index}

        orchestrator._run_locked = fake_run
        run_dir = Path(tempfile.gettempdir()) / "same_dataset_same_episode"
        threads = [
            threading.Thread(target=orchestrator.run, args=(run_dir,), kwargs={"episode_index": 3})
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
