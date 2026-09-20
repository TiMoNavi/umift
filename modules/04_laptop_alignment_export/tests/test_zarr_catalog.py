from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umift_laptop_alignment.pipeline.episode_exclusions import load_episode_exclusions
from umift_laptop_alignment.pipeline.inspect.catalog import ZarrCatalog, deletion_blockers


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ZarrCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_dir = self.root / "run_001"
        (self.run_dir / "raw").mkdir(parents=True)
        write_json(self.run_dir / "RUN_MANIFEST.json", {"streams": {}})
        self.dataset = self.run_dir / "exports" / "umift_replay_buffer_zarr" / "sample.zarr"
        self.dataset.mkdir(parents=True)
        self.catalog = ZarrCatalog([self.root])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_episode_context_maps_logical_to_capture_episode(self) -> None:
        write_json(
            self.dataset / "UMIFT_EXPORT_MANIFEST.json",
            {
                "episodes": [
                    {
                        "episode_index": 2,
                        "source_episode_index": 3,
                        "iphone_session_id": "iphone_session",
                        "start_aligned_monotonic_ns": 1234,
                        "end_aligned_monotonic_ns": 5678,
                    }
                ]
            },
        )

        context = self.catalog._episode_context(self.dataset)

        self.assertEqual(context[2]["source_episode_index"], 3)
        self.assertEqual(context[2]["session_id"], "iphone_session")

    def test_run_datasets_includes_dataset_without_zarr(self) -> None:
        empty_run = self.root / "run_002"
        (empty_run / "raw").mkdir(parents=True)
        write_json(
            empty_run / "RUN_MANIFEST.json",
            {
                "created_at": "2026-07-17T10:00:00Z",
                "dataset": {
                    "display_name": "Socket insertion",
                    "task_name": "insert_socket",
                },
            },
        )

        rows = self.catalog.run_datasets()
        row = next(item for item in rows if item["run_dir"] == str(empty_run.resolve()))

        self.assertEqual(row["display_name"], "Socket insertion")
        self.assertEqual(row["episode_count"], 0)
        self.assertEqual(row["zarr_count"], 0)
        self.assertEqual(row["zarr_paths"], [])

    def test_deletion_blockers_reject_active_capture_on_same_dataset(self) -> None:
        blockers = deletion_blockers(
            {
                "state": {
                    "running": True,
                    "run_dir": str(self.run_dir),
                    "recording_control": {"active": True},
                    "postprocess": {"status": "running"},
                    "export_control": {"status": "exporting"},
                    "d435": {"running": True},
                    "coinft": {"running": True},
                }
            },
            self.run_dir,
        )

        self.assertIn("iPhone receiver is running", blockers)
        self.assertIn("an Episode is actively recording", blockers)
        self.assertIn("D435 capture is running", blockers)
        self.assertIn("CoinFT capture is running", blockers)
        self.assertIn("postprocess is running", blockers)
        self.assertIn("Zarr export is running", blockers)

    def test_deletion_blockers_ignore_other_dataset(self) -> None:
        blockers = deletion_blockers(
            {"state": {"running": True, "run_dir": str(self.root / "run_other")}},
            self.run_dir,
        )

        self.assertEqual(blockers, [])

    def test_delete_episode_removes_corresponding_raw_and_preserves_other_episode(self) -> None:
        import numpy as np
        import zarr

        deleted_coinft = self.run_dir / "raw" / "coinft" / "episode_000003" / "raw.csv"
        deleted_d435 = self.run_dir / "raw" / "global_camera" / "episode_000003" / "rgb.mp4"
        deleted_iphone = self.run_dir / "raw" / "iphone_stream" / "iphone_session" / "raw.jsonl"
        retained_raw = self.run_dir / "raw" / "coinft" / "episode_000000" / "raw.csv"
        for path in (deleted_coinft, deleted_d435, deleted_iphone, retained_raw):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raw", encoding="utf-8")
        for path in (
            self.run_dir / "normalized" / "episodes" / "episode_000003" / "rows.jsonl",
            self.run_dir / "aligned" / "episodes" / "episode_000003" / "timeline.jsonl",
            self.run_dir / "pipeline" / "live_zarr_rows" / "episode_000003" / "rows.jsonl",
            self.run_dir / "normalized" / "episodes" / "episode_000000" / "rows.jsonl",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("derived", encoding="utf-8")
        root = zarr.open_group(str(self.dataset), mode="w")
        data = root.create_group("data")
        root.create_group("meta")
        for logical_index, source_index, session_id in (
            (0, 0, "retained_session"),
            (1, 3, "iphone_session"),
        ):
            group = data.create_group(f"episode_{logical_index}")
            group.attrs.update(
                {
                    "zarr_episode_index": logical_index,
                    "source_episode_index": source_index,
                    "iphone_session_id": session_id,
                }
            )
            group.create_dataset("rgb_0", data=np.zeros((2, 2, 2, 3), dtype=np.uint8))
            group.create_dataset("ts_pose_fb_0", data=np.zeros((2, 7), dtype=np.float32))
            group.create_dataset("gripper_0", data=np.zeros((2, 1), dtype=np.float32))
            group.create_dataset("wrench_left_0", data=np.zeros((2, 6), dtype=np.float32))
        export_manifest = {
            "episodes": [
                {"episode_index": 0, "source_episode_index": 0, "iphone_session_id": "retained_session"},
                {"episode_index": 1, "source_episode_index": 3, "iphone_session_id": "iphone_session"},
            ]
        }
        write_json(self.dataset / "UMIFT_EXPORT_MANIFEST.json", export_manifest)
        write_json(self.dataset.parent / "UMIFT_EXPORT_MANIFEST.json", export_manifest)
        write_json(self.dataset.parent / "EXPORT_CONFIG.json", {})
        write_json(
            self.run_dir / "RUN_MANIFEST.json",
            {
                "streams": {
                    "coinft": {
                        "artifacts": {
                            "deleted": {"path": "raw/coinft/episode_000003"},
                            "retained": {"path": "raw/coinft/episode_000000"},
                            "normalized": {"path": "normalized/episodes/episode_000003/rows.jsonl"},
                        }
                    },
                    "iphone": {
                        "artifacts": {
                            "deleted": {"path": "raw/iphone_stream/iphone_session"},
                        }
                    },
                    "alignment": {
                        "artifacts": {"timeline": {"path": "aligned/episodes/episode_000003/timeline.jsonl"}},
                    },
                }
            },
        )
        result = self.catalog.delete_episode(str(self.dataset), "episode_1")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["raw_deleted"])
        self.assertFalse(deleted_coinft.exists())
        self.assertFalse(deleted_d435.exists())
        self.assertFalse(deleted_iphone.exists())
        self.assertTrue(retained_raw.exists())
        self.assertTrue(self.dataset.exists())
        self.assertFalse((self.run_dir / "normalized/episodes/episode_000003/rows.jsonl").exists())
        self.assertFalse((self.run_dir / "aligned/episodes/episode_000003/timeline.jsonl").exists())
        self.assertTrue((self.run_dir / "normalized/episodes/episode_000000/rows.jsonl").exists())
        self.assertFalse((self.run_dir / "pipeline" / "live_zarr_rows" / "episode_000003").exists())
        data = zarr.open_group(str(self.dataset), mode="r")["data"]
        self.assertEqual(list(data.group_keys()), ["episode_0"])
        exclusions = load_episode_exclusions(self.run_dir)
        self.assertEqual(exclusions[0]["source_episode_index"], 3)
        self.assertEqual(exclusions[0]["session_id"], "iphone_session")
        manifest = json.loads((self.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertIn("retained", manifest["streams"]["coinft"]["artifacts"])
        self.assertNotIn("deleted", manifest["streams"]["coinft"]["artifacts"])
        self.assertNotIn("alignment", manifest["streams"])
        self.assertEqual(manifest["postprocess"]["stage"], "episode_deleted")
        export_manifest = json.loads((self.dataset / "UMIFT_EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(export_manifest["episode_count"], 1)

    def test_delete_only_episode_removes_export_bundle_and_raw(self) -> None:
        deleted_coinft = self.run_dir / "raw" / "coinft" / "episode_000003" / "raw.csv"
        deleted_iphone = self.run_dir / "raw" / "iphone_stream" / "iphone_session" / "raw.jsonl"
        for path in (deleted_coinft, deleted_iphone):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raw", encoding="utf-8")
        (self.dataset.parent / "UMIFT_EXPORT_MANIFEST.json").write_text("{}", encoding="utf-8")
        episode = {
            "name": "episode_0",
            "zarr_episode_index": 0,
            "source_episode_index": 3,
            "session_id": "iphone_session",
        }

        with patch.object(self.catalog, "describe", return_value={"episodes": [episode]}):
            result = self.catalog.delete_episode(str(self.dataset), "episode_0")

        self.assertEqual(result["status"], "ok")
        self.assertFalse(self.dataset.parent.exists())
        self.assertFalse(deleted_coinft.exists())
        self.assertFalse(deleted_iphone.exists())
        self.assertTrue(self.run_dir.exists())
        manifest = json.loads((self.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["postprocess"]["status"], "idle")
        self.assertEqual(manifest["postprocess"]["remaining_episode_count"], 0)

    def test_resolve_rejects_dataset_outside_roots(self) -> None:
        outside = self.root.parent / "outside.zarr"
        with self.assertRaises(ValueError):
            self.catalog.resolve(str(outside))

    def test_delete_zarr_clears_all_data_but_preserves_dataset(self) -> None:
        raw_file = self.run_dir / "raw" / "source.bin"
        raw_file.write_bytes(b"raw")
        normalized = self.run_dir / "normalized" / "rows.jsonl"
        aligned = self.run_dir / "aligned" / "timeline.jsonl"
        pipeline = self.run_dir / "pipeline" / "live_zarr_rows" / "episode_000000" / "rows.jsonl"
        exclusion = self.run_dir / "quality" / "EPISODE_EXCLUSIONS.json"
        episode_state = self.run_dir / "raw" / "episode_index_state.json"
        for path in (normalized, aligned, pipeline):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("derived", encoding="utf-8")
        write_json(exclusion, {"exclusions": [{"source_episode_index": 0}]})
        write_json(episode_state, {"next_episode_index": 4})
        dataset_metadata = {
            "display_name": "Socket insertion",
            "task_name": "insert_socket",
            "compatibility": {"export_format": "umift-replay-buffer-zarr"},
        }
        write_json(
            self.run_dir / "RUN_MANIFEST.json",
            {
                "dataset": dataset_metadata,
                "streams": {
                    "coinft": {
                        "calibration": {"mode": "calibrated"},
                        "artifacts": {"raw": {"path": "raw/source.bin"}},
                    },
                    "export": {
                        "artifacts": {
                            "zarr": {"path": "exports/umift_replay_buffer_zarr"},
                        }
                    }
                }
            },
        )
        (self.dataset.parent / "UMIFT_EXPORT_MANIFEST.json").write_text("{}", encoding="utf-8")

        result = self.catalog.delete_zarr(str(self.dataset))

        self.assertEqual(result["status"], "ok")
        self.assertFalse(self.dataset.exists())
        self.assertFalse(raw_file.exists())
        self.assertFalse(normalized.exists())
        self.assertFalse(aligned.exists())
        self.assertFalse(pipeline.exists())
        self.assertFalse(exclusion.exists())
        self.assertFalse(episode_state.exists())
        self.assertEqual(list((self.run_dir / "raw").rglob("episode_*")), [])
        self.assertEqual([path for path in (self.run_dir / "raw").rglob("*") if path.is_file()], [])
        self.assertEqual(list((self.run_dir / "exports").rglob("*")), [])
        manifest = json.loads((self.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset"], dataset_metadata)
        self.assertEqual(manifest["postprocess"]["status"], "idle")
        self.assertEqual(manifest["postprocess"]["last_operation"], "dataset_data_cleared")
        self.assertEqual(manifest["streams"], {"coinft": {"calibration": {"mode": "calibrated"}}})
        self.assertEqual(result["next_episode_index"], 0)

    def test_delete_zarr_can_clear_run_when_zarr_is_already_missing(self) -> None:
        self.catalog._remove_path(self.dataset)
        raw_file = self.run_dir / "raw" / "coinft" / "episode_000000" / "raw.csv"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text("raw", encoding="utf-8")

        result = self.catalog.delete_zarr(run_dir=self.run_dir)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(raw_file.exists())
        self.assertTrue(self.run_dir.exists())
        self.assertTrue((self.run_dir / "RUN_MANIFEST.json").exists())
        self.assertEqual(self.catalog.run_datasets()[0]["episode_count"], 0)


if __name__ == "__main__":
    unittest.main()
