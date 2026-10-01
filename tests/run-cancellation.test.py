#!/usr/bin/env python3
import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest


APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion"
sys.path.insert(0, str(APP_DIR))
live = importlib.import_module("swb_live")


@unittest.skipUnless(os.name == "posix" and pathlib.Path("/proc").is_dir(), "requires Linux process groups")
class RunCancellationTests(unittest.TestCase):
    def test_registered_process_group_is_terminated_and_persisted(self):
        with tempfile.TemporaryDirectory(prefix="spark-stop-", dir=str(pathlib.Path.home())) as root:
            root_path = pathlib.Path(root)
            project = root_path / "isolated_project"
            project.mkdir()
            (project / "gtree.dat").write_text("# isolated test\n", encoding="utf-8")
            fixture = root_path / "gsub-fixture.py"
            fixture.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            live.RUN_STATE_ROOT = str(root_path / "run-state")
            process = subprocess.Popen(
                [sys.executable, str(fixture), str(project)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            run_id = "cancel-test-001"
            try:
                live._save_run_state({
                    "runId": run_id, "taskId": "task-test", "project": str(project),
                    "node": 1, "pid": process.pid, "pgid": process.pid, "status": "running",
                })
                result = live.stop_run(str(project), run_id)
                deadline = time.time() + 3
                while process.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(result["stopped"])
                self.assertIsNotNone(process.poll())
                self.assertEqual(live._load_run_state(run_id)["status"], "cancelled")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, 9)


if __name__ == "__main__":
    unittest.main()
