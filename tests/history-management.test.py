#!/usr/bin/env python3
"""Isolated regression coverage for EmberTCAD history state and clearing."""

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest


DEFAULT_SERVICE = pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion" / "research_service.py"
SERVICE_PATH = pathlib.Path(os.environ.get("EMBER_SERVICE_PATH", str(DEFAULT_SERVICE)))


class HistoryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="embertcad-history-test-")
        root = pathlib.Path(self.temp.name)
        workspace = root / "STDB"
        workspace.mkdir()
        os.environ["AITCAD_PROJECT_ROOT"] = str(workspace)
        os.environ["AITCAD_RESEARCH_ROOT"] = str(root / "research")
        os.environ["AITCAD_RUN_STROOT"] = str(root / "missing-sentaurus")
        spec = importlib.util.spec_from_file_location("embertcad_history_service", SERVICE_PATH)
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def backdate(self, task):
        old_time = "2020-01-01T00:00:00Z"
        task["updatedAt"] = old_time
        connection = self.service.database()
        try:
            connection.execute(
                "UPDATE tasks SET updated_at=?, payload_json=? WHERE id=?",
                (old_time, json.dumps(task, ensure_ascii=False, sort_keys=True), task["taskId"]),
            )
            connection.commit()
        finally:
            connection.close()

    def test_waiting_state_is_not_reported_as_running(self):
        self.service.save_task({
            "project": "demo/waiting", "kind": "workspace-task", "question": "review",
            "status": "approval-required", "events": [],
        })
        project = self.service.project_history()[0]
        self.assertEqual(project["activeCount"], 0)
        self.assertEqual(project["pendingCount"], 1)
        self.assertEqual(project["displayStatus"], "pending")

    def test_stale_run_is_interrupted_and_live_pid_survives_clear(self):
        stale = self.service.save_task({
            "project": "demo/stale", "kind": "workspace-task", "question": "old",
            "status": "running", "runId": "stale-run-1234", "activePid": 99999999, "events": [],
        })
        live = self.service.save_task({
            "project": "demo/live", "kind": "workspace-task", "question": "live",
            "status": "running", "runId": "live-run-12345", "activePid": os.getpid(), "events": [],
        })
        finished = self.service.save_task({
            "project": "demo/done", "kind": "workspace-task", "question": "done",
            "status": "completed", "events": [],
        })
        self.backdate(stale)
        self.backdate(live)
        report_dir = pathlib.Path(self.service.REPORT_ROOT) / finished["taskId"]
        report_dir.mkdir(parents=True)
        (report_dir / "task-report.md").write_text("report", encoding="utf-8")

        self.assertIn(stale["taskId"], self.service.reconcile_stale_tasks())
        self.assertEqual(self.service.load_task(stale["taskId"])["status"], "interrupted")
        self.assertEqual(self.service.load_task(live["taskId"])["status"], "running")
        result = self.service.clear_history()
        self.assertEqual(result["keptActive"], 1)
        self.assertEqual(result["deletedTasks"], 2)
        self.assertEqual(result["deletedReports"], 1)
        self.assertFalse(report_dir.exists())
        self.assertEqual(self.service.load_task(live["taskId"])["status"], "running")


if __name__ == "__main__":
    unittest.main()
