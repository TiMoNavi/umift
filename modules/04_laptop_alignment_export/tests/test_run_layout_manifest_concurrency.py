from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from umift_laptop_alignment.orchestration.run_layout import register_artifact


class RunLayoutManifestConcurrencyTest(unittest.TestCase):
    def test_concurrent_artifact_registration_preserves_every_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            worker_count = 8
            artifacts_per_worker = 12
            start = threading.Barrier(worker_count)
            errors: list[BaseException] = []

            def worker(worker_index: int) -> None:
                try:
                    start.wait()
                    for artifact_index in range(artifacts_per_worker):
                        role = f"worker_{worker_index:02d}_{artifact_index:02d}"
                        register_artifact(
                            run_dir,
                            stream=f"stream_{worker_index:02d}",
                            role=role,
                            path=run_dir / "raw" / role,
                            metadata={"worker": worker_index, "artifact": artifact_index},
                        )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            manifest_path = run_dir / "RUN_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for worker_index in range(worker_count):
                artifacts = manifest["streams"][f"stream_{worker_index:02d}"]["artifacts"]
                self.assertEqual(len(artifacts), artifacts_per_worker)
            self.assertEqual(list(run_dir.glob(".RUN_MANIFEST.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
