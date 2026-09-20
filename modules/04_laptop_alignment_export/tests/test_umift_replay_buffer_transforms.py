from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import UmiFTReplayBufferExporter
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.pose import export_pose7_from_iphone_pose
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.spatial import pose7_to_se3
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.wrench import coinft_pair_to_tcp


class UmiFTReplayBufferTransformTest(unittest.TestCase):
    def test_identity_iphone_pose_yields_gripper_center_tcp_extrinsic(self) -> None:
        exporter = UmiFTReplayBufferExporter()
        config = exporter.default_config()
        pose = {
            "camera_in_user_world_transform": np.eye(4).tolist(),
        }

        exported = export_pose7_from_iphone_pose(pose, config["pose"], np)

        expected = np.asarray(config["pose"]["iphone_camera_t_gripper_center_tcp"])
        np.testing.assert_allclose(pose7_to_se3(exported, np), expected, atol=1e-9)
        np.testing.assert_allclose(exported[:3], [0.039711, -0.094423, -0.257957], atol=1e-9)
        np.testing.assert_allclose(np.abs(exported[3:]), [0.0, 1.0, 0.0, 0.0], atol=1e-9)

    def test_full_matrix_takes_precedence_over_conflicting_legacy_fields(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        world_t_iphone = np.eye(4)
        world_t_iphone[:3, 3] = [1.0, 2.0, 3.0]
        pose = {
            "camera_in_user_world_transform": world_t_iphone.tolist(),
            "x_m": 100.0,
            "y_m": 200.0,
            "z_m": 300.0,
            "quaternion_qwxyz": [1.0, 0.0, 0.0, 0.0],
        }

        exported = export_pose7_from_iphone_pose(pose, config, np)

        expected = world_t_iphone @ np.asarray(config["iphone_camera_t_gripper_center_tcp"])
        np.testing.assert_allclose(pose7_to_se3(exported, np), expected, atol=1e-9)

    def test_relative_tcp_motion_is_invariant_to_world_frame_change(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        current = np.eye(4)
        current[:3, 3] = [0.2, -0.1, 0.4]
        sample = np.eye(4)
        angle = np.deg2rad(35.0)
        sample[:3, :3] = [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
        sample[:3, 3] = [0.6, 0.3, 0.1]
        world_change = np.eye(4)
        world_change[:3, :3] = [
            [0.0, -1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        world_change[:3, 3] = [2.0, -4.0, 1.0]

        def exported_tcp(source: np.ndarray) -> np.ndarray:
            pose7 = export_pose7_from_iphone_pose(
                {"camera_in_user_world_transform": source.tolist()}, config, np
            )
            return pose7_to_se3(pose7, np)

        current_tcp = exported_tcp(current)
        sample_tcp = exported_tcp(sample)
        changed_current_tcp = exported_tcp(world_change @ current)
        changed_sample_tcp = exported_tcp(world_change @ sample)

        relative = np.linalg.inv(current_tcp) @ sample_tcp
        changed_relative = np.linalg.inv(changed_current_tcp) @ changed_sample_tcp
        np.testing.assert_allclose(changed_relative, relative, atol=1e-9)

    def test_invalid_gripper_center_tcp_extrinsic_is_rejected(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        config["iphone_camera_t_gripper_center_tcp"][0][0] = 2.0

        with self.assertRaisesRegex(SystemExit, "rotation must be orthonormal"):
            export_pose7_from_iphone_pose(
                {"camera_in_user_world_transform": np.eye(4).tolist()}, config, np
            )

    def test_reflection_extrinsic_is_rejected(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        config["iphone_camera_t_gripper_center_tcp"] = np.diag(
            [-1.0, 1.0, 1.0, 1.0]
        ).tolist()

        with self.assertRaisesRegex(SystemExit, r"rotation determinant must be \+1"):
            export_pose7_from_iphone_pose(
                {"camera_in_user_world_transform": np.eye(4).tolist()}, config, np
            )

    def test_legacy_pose_transform_config_is_rejected(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        config["iphone15_pro_i_t_g"] = np.eye(4).tolist()

        with self.assertRaisesRegex(SystemExit, "Legacy UMI-FT pose config"):
            export_pose7_from_iphone_pose(
                {"camera_in_user_world_transform": np.eye(4).tolist()}, config, np
            )

    def test_invalid_full_source_matrix_is_not_hidden_by_legacy_fields(self) -> None:
        config = UmiFTReplayBufferExporter().default_config()["pose"]
        pose = {
            "camera_in_user_world_transform": np.diag([-1.0, 1.0, 1.0, 1.0]).tolist(),
            "x_m": 0.0,
            "y_m": 0.0,
            "z_m": 0.0,
            "quaternion_qwxyz": [1.0, 0.0, 0.0, 0.0],
        }

        with self.assertRaisesRegex(SystemExit, r"rotation determinant must be \+1"):
            export_pose7_from_iphone_pose(pose, config, np)

    def test_coinft_wrench_is_transformed_to_tcp_frame(self) -> None:
        exporter = UmiFTReplayBufferExporter()
        config = exporter.default_config()
        left = np.asarray([1.0, 2.0, 3.0, 0.1, 0.2, 0.3], dtype=np.float64)
        right = np.asarray([-1.0, 2.0, -3.0, -0.1, 0.2, -0.3], dtype=np.float64)

        left_tcp, right_tcp = coinft_pair_to_tcp(left, right, 0.08, config["force"], np)

        self.assertEqual(left_tcp.shape, (6,))
        self.assertEqual(right_tcp.shape, (6,))
        self.assertFalse(np.allclose(left_tcp, left))
        self.assertFalse(np.allclose(right_tcp, right))

    def test_run_level_full_export_is_removed(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Run-level full export was removed"):
            UmiFTReplayBufferExporter().export(Path("/tmp/not-used"))


if __name__ == "__main__":
    unittest.main()
