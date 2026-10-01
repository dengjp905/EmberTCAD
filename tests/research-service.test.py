#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


SERVICE_PATH = pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion" / "research_service.py"


class ResearchServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="aitcad-research-test-")
        cls.root = pathlib.Path(cls.temp.name)
        cls.workspace = cls.root / "STDB_class"
        cls.project = cls.workspace / "nmos_study" / "nmos_project"
        cls.project.mkdir(parents=True)
        (cls.project / "gtree.dat").write_text("# test\n", encoding="utf-8")
        (cls.project / "nmos_sde_dvs.cmd").write_text(
            '(sdegeo:create-rectangle (position 0 0 0) (position 1 1 0) "Silicon" "NMOS_region")\n'
            '(sdegeo:create-rectangle (position 0 0 0) (position 1 0.1 0) "SiO2" "GATE_oxide_region_right")\n',
            encoding="utf-8",
        )
        (cls.project / "sdevice_des.cmd").write_text(
            'Electrode { { Name="gate" Voltage=0.0 } }\n\n'
            'Physics {\n  Fermi\n}\n\n'
            'Plot {\n  Potential SpaceCharge\n}\n\n'
            'Math { Digits=5 }\nSolve { Poisson }\n',
            encoding="utf-8",
        )
        (cls.project / "nmos_fps.cmd").write_text(
            "line x location= 0 spacing= 0.01\nimplant Boron dose= 1e13 energy= 20\ndiffuse temperature= 1000 time= 10\n",
            encoding="utf-8",
        )

        (cls.project / "extract_ins.cmd").write_text(
            "cv_create IdVg \"gate OuterVoltage\" \"drain TotalCurrent\"\nft_scalar Vth 0.5\n",
            encoding="utf-8",
        )
        tutorial_root = cls.root / "Applications_Library" / "GettingStarted" / "sdevice" / "Traps" / "TrapDOS"
        tutorial_root.mkdir(parents=True)
        (tutorial_root / "sim1_des.cmd").write_text(
            'Physics { Traps((eNeutral Gaussian fromMidBandGap Conc=1e17)) }\n',
            encoding="utf-8",
        )
        os.environ["AITCAD_PROJECT_ROOT"] = str(cls.workspace)
        os.environ["AITCAD_RESEARCH_ROOT"] = str(cls.root / "research")
        os.environ["AITCAD_TUTORIAL_ROOT"] = str(cls.root / "Applications_Library" / "GettingStarted" / "sdevice")
        os.environ["AITCAD_APPLICATIONS_ROOT"] = str(cls.root / "Applications_Library")
        os.environ["AITCAD_SDEVICE_MANUAL"] = str(cls.root / "missing.pdf")
        os.environ["AITCAD_RUN_STROOT"] = str(cls.root / "missing-sentaurus")
        spec = importlib.util.spec_from_file_location("aitcad_research_service", SERVICE_PATH)
        cls.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.service)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_sdevice_name_is_not_misrouted_to_short_sde_token(self):
        self.assertEqual(
            self.service.normalize_tool(
                None,
                "请为这个 SDevice 工程增加界面 trap 物理模型并生成最小代码差异",
            ),
            "sdevice",
        )

    def test_svisual_is_not_misrouted_to_inspect(self):
        self.assertEqual(self.service.normalize_tool("svisual"), "svisual")
        self.assertEqual(
            self.service.normalize_tool(None, "Use SVisual to plot data and export the curve"),
            "svisual",
        )
        self.assertEqual(self.service.tool_config("svisual")["label"], "SVisual")

    def test_new_sentaurus_manual_layout_is_selected_when_present(self):
        legacy = self.root / "manuals" / "PDFManual" / "data" / "svisual_ug.pdf"
        modern = self.root / "manuals" / "olh_sentaurus" / "pdf" / "tcad_svisual_ug.pdf"
        modern.parent.mkdir(parents=True, exist_ok=True)
        modern.write_bytes(b"%PDF-1.4\n")
        self.assertEqual(
            self.service.first_existing_path(str(legacy), str(modern)),
            str(modern),
        )

    def test_natural_source_repair_request_routes_to_tool_change(self):
        self.assertEqual(
            self.service.infer_workspace_task_type(
                "请修复当前 SDevice 源文件中 Physics 与 Solve 之间的不一致问题"
            ),
            "tool-change",
        )

    def test_required_select_from_model_always_has_valid_default(self):
        values = self.service.normalize_plan_inputs([{
            "key": "approvalGate", "label": "批准门", "type": "select",
            "value": "用户批准后执行", "required": True,
            "options": ["reviewed", "blocked"],
        }])
        self.assertEqual(values[0]["value"], "reviewed")

    def test_project_context_is_never_required_user_input(self):
        raw = [{
            "key": "sdeCompleteError", "label": "SDE 完整报错信息", "type": "text",
            "value": "", "required": True, "description": "请粘贴完整错误日志",
        }, {
            "key": "targetVth", "label": "目标阈值", "type": "number",
            "value": 0.52, "unit": "V", "required": True,
        }]
        values = self.service.normalize_plan_inputs(raw)
        self.assertEqual([item["key"] for item in values], ["targetVth"])
        # Old persisted plans are protected at approval time too.
        approved = self.service.validate_approved_inputs(raw, {"targetVth": "0.55"})
        self.assertNotIn("sdeCompleteError", approved)
        self.assertEqual(approved["targetVth"], 0.55)

    def test_reference_pdf_is_indexed_by_page_and_attached_to_creation_task(self):
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4\nsynthetic test\n")

        class FakeProcess(object):
            def __init__(self, args):
                self.args = args
                self.returncode = 0

            def communicate(self):
                if self.args[0] == "pdfinfo":
                    return b"Pages: 2\n", b""
                return (("PN junction overview with silicon doping and two electrical contacts.\f"
                         "Reverse-bias electric field and breakdown voltage are extracted from the simulated junction.").encode("utf-8"), b"")

        with mock.patch.object(self.service.subprocess, "Popen", side_effect=lambda args, **_kwargs: FakeProcess(args)):
            reference = self.service.ingest_reference_pdf({"path": str(source)})["reference"]
        self.assertEqual(reference["pageCount"], 2)
        self.assertTrue(reference["textAvailable"])
        task = self.service.create_generation_task({
            "goal": "Create a 2D PN junction and study reverse-bias electric field and breakdown voltage",
            "references": [reference],
        })["task"]
        self.assertEqual(task["references"][0]["sha256"], reference["sha256"])
        evidence = self.service.reference_pdf_evidence(task["references"], task["question"])
        self.assertTrue(any(item["location"] == "PDF p.2" for item in evidence))

    def test_project_history_groups_many_records_into_one_project_row(self):
        project = "grouping/one-project"
        first = self.service.save_task({
            "project": project, "kind": "project-understanding", "question": "read",
            "status": "understood", "summary": "read complete",
        })
        second = self.service.save_task({
            "project": project, "kind": "workspace-task", "question": "run",
            "status": "completed", "summary": "run complete", "reportPath": "/tmp/report.md",
        })
        rows = [item for item in self.service.project_history() if item.get("project") == project]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["taskCount"], 2)
        self.assertEqual(rows[0]["reportCount"], 1)
        details = self.service.project_task_history(project)
        self.assertEqual({item["taskId"] for item in details}, {first["taskId"], second["taskId"]})

    def test_project_history_distinguishes_waiting_from_real_execution(self):
        project = "grouping/pending-project"
        self.service.save_task({
            "project": project, "kind": "workspace-task", "question": "review me",
            "status": "approval-required", "summary": "waiting for user approval",
        })
        row = next(item for item in self.service.project_history() if item.get("project") == project)
        self.assertEqual(row["activeCount"], 0)
        self.assertEqual(row["pendingCount"], 1)
        self.assertEqual(row["displayStatus"], "pending")

    def test_detects_real_gate_oxide_region_pair(self):
        result = self.service.detect_gate_oxide_interface(str(self.project))
        self.assertEqual(result["interface"], "NMOS_region/GATE_oxide_region_right")
        self.assertEqual(result["confidence"], "high")

    def test_tid_generation_is_replaceable_and_adds_diagnostics_once(self):
        source = (self.project / "sdevice_des.cmd").read_text(encoding="utf-8")
        first, _fields = self.service.generate_tid_content(source, "NMOS_region/GATE_oxide_region_right")
        second, _fields = self.service.generate_tid_content(first, "NMOS_region/GATE_oxide_region_right")
        self.assertEqual(first, second)
        self.assertEqual(first.count("AITCAD TID MODEL BEGIN"), 1)
        self.assertEqual(first.count("eInterfaceTrappedCharge"), 1)
        self.assertIn("TID_QoxPerKrad*TID_Dose_krad", first)
        self.assertIn("Conc=@<TID_Qox0+TID_QoxPerKrad*TID_Dose_krad>@", first)
        self.assertNotIn("#define _AITCAD_QOX_", first)

    def test_plan_is_persisted_and_report_marks_unexecuted_state(self):
        plan = self.service.plan_tid_task(
            str(self.project),
            "请根据 manual 和 tutorial 在 sdevice 里加 trap 模型，研究 TID 对 NMOS 阈值的影响",
            include_articles=False,
        )
        self.assertEqual(plan["status"], "review-required")
        self.assertFalse(plan["executionReady"])
        self.assertTrue(plan["calibration"]["required"])
        self.assertTrue(plan["modification"]["diff"])
        history = self.service.task_history(10)
        self.assertEqual(history[0]["taskId"], plan["taskId"])
        report = self.service.create_report(plan["taskId"])
        self.assertTrue(pathlib.Path(report["path"]).is_file())
        self.assertIn("No TID sweep has been executed", report["content"])

    def test_generated_project_report_hashes_blueprint_files(self):
        task = self.service.save_task({
            "project": "draft/generated-report", "kind": "workspace-task",
            "taskType": "project-create", "question": "create a project",
            "status": "completed", "summary": "created",
            "blueprint": {
                "name": "generated_report", "toolChain": [{"step": "geom", "tool": "sde"}],
                "files": [{"path": "geom_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'}],
                "evidence": [], "validationPlan": ["Run SDE"],
            },
        })
        report = self.service.create_report(task["taskId"])
        self.assertIn("Generated project blueprint", report["content"])
        self.assertIn("SHA-256", report["content"])
        self.assertTrue(pathlib.Path(report["path"]).is_file())

    def test_tutorial_search_is_version_local(self):
        results = self.service.search_tutorials("trap", 5)
        self.assertTrue(results)
        self.assertIn("TrapDOS", results[0]["relativePath"])

    def test_generic_source_discovery_covers_supported_tools(self):
        expected = {
            "sdevice": "sdevice_des.cmd",
            "sprocess": "nmos_fps.cmd",
            "sde": "nmos_sde_dvs.cmd",
            "inspect": "extract_ins.cmd",
        }
        for tool, filename in expected.items():
            with self.subTest(tool=tool):
                self.assertEqual(pathlib.Path(self.service.find_tool_input(str(self.project), tool)).name, filename)

    def test_generic_model_plan_records_dynamic_parameters_and_diff(self):
        task = self.service.generic_research_plan(
            str(self.project), "Add a field-dependent mobility model with documented parameters", False, "sdevice"
        )
        source = (self.project / "sdevice_des.cmd").read_text(encoding="utf-8")
        modified = source.replace("  Fermi\n", "  Fermi\n  Mobility( HighFieldSaturation )\n")
        result = self.service.record_model_proposal({
            "taskId": task["taskId"],
            "providerModel": "test-model",
            "proposal": {
                "summary": "Added a documented mobility model.",
                "physicalModel": "Field-dependent carrier mobility.",
                "assumptions": ["Parameters require calibration."],
                "validationPlan": ["Run the baseline SDevice node."],
                "parameters": [{
                    "name": "HighFieldModel", "value": "HighFieldSaturation", "unit": "",
                    "role": "Mobility high-field correction", "evidence": "E1", "confidence": "manual-syntax",
                    "userInputRequired": False, "bindToSwb": False,
                }],
                "modifiedContent": modified,
            },
        })
        recorded = result["task"]
        self.assertEqual(recorded["kind"], "tool-model")
        self.assertEqual(recorded["status"], "review-required")
        self.assertEqual(recorded["modelParameters"][0]["name"], "HighFieldModel")
        self.assertIn("Mobility( HighFieldSaturation )", recorded["modification"]["diff"])
        applied = self.service.record_application({
            "taskId": task["taskId"],
            "writeResult": {
                "relativePath": recorded["modification"]["relativePath"],
                "backupId": "backup-test-1",
                "originalSha256": recorded["modification"]["sourceSha256"],
                "updatedSha256": "updated-sha-test",
                "recoverable": True,
            },
            "parametersAdded": [],
            "parametersExisting": [],
        })
        self.assertEqual(applied["task"]["status"], "code-applied")
        self.service.record_validation({
            "taskId": task["taskId"],
            "validation": {
                "node": 7, "state": "done", "sourceSha256": "updated-sha-test",
                "summary": "SDevice node 7 completed.",
                "artifacts": [{"relativePath": "n7_des.log", "bytes": 123, "modifiedDuringRun": True}],
            },
        })
        report = self.service.create_report(task["taskId"])["content"]
        self.assertIn("backup-test-1", report)
        self.assertIn("n7_des.log", report)
        self.assertIn("updated-sha-test", report)

    def test_project_read_report_is_bounded_persistent_and_reusable(self):
        live_state = {
            "parameters": [{"name": "Vd", "default": "0.1", "values": "0.1", "step": "1"}],
            "nodes": [{"node": "1", "tool": "sde", "values": "0.1", "status": "done"}],
        }
        first = self.service.project_read_report({"project": str(self.project), "liveState": live_state})
        second = self.service.project_read_report({"project": str(self.project), "liveState": live_state})
        self.assertEqual(first["task"]["kind"], "project-understanding")
        self.assertGreaterEqual(first["report"]["coverage"], 70)
        self.assertEqual(first["task"]["taskId"], second["task"]["taskId"])
        self.assertTrue(second["cached"])
        self.assertIn("SDevice", [item["label"] for item in first["report"]["tools"]])

    def test_local_scan_is_not_claimed_as_ai_understanding(self):
        reading = self.service.project_read_report({"project": str(self.project), "liveState": {}})
        self.assertIn(reading["task"]["status"], ("local-scan-complete", "understood"))
        if not (reading["report"].get("aiAnalysis")):
            self.assertNotEqual(reading["task"]["status"], "understood")
        recorded = self.service.record_project_ai_analysis({
            "taskId": reading["task"]["taskId"],
            "providerModel": "test-thinking-model", "thinkingEnabled": True,
            "reasoningUsed": True, "reasoningTokens": 321, "sourceFilesAnalyzed": 4,
            "analysis": {
                "overview": "The model read the supplied project sources.",
                "deviceIntent": "NMOS transfer simulation", "toolchain": ["SDE -> SDevice"],
                "fileRoles": [{"file": "sdevice_des.cmd", "role": "device solve", "confidence": "high"}],
                "parameters": ["Vd=0.1 V"], "dependencies": ["SDevice consumes SDE TDR"],
                "existingResults": [], "risks": ["needs validation"], "unknowns": ["mesh convergence"],
                "suggestedQuestions": ["What is the target metric?"],
                "reasoningSummary": "Cross-checked file roles and node order.",
                "adaptiveSections": [{"type": "facts", "title": "Flow", "items": ["SDE -> SDevice"]}],
            },
        })["task"]
        self.assertEqual(recorded["status"], "understood")
        self.assertTrue(recorded["reasoning"]["used"])
        self.assertEqual(recorded["projectReport"]["aiStatus"], "complete")
        self.assertEqual(recorded["summary"], "The model read the supplied project sources.")

    def test_workspace_task_requires_plan_approval_before_execution(self):
        report = self.service.project_read_report({"project": str(self.project), "liveState": {}})
        created = self.service.create_workspace_task({
            "project": str(self.project),
            "goal": "请加入迁移率模型并运行节点验证",
            "projectReportTaskId": report["task"]["taskId"],
        })["task"]
        self.assertEqual(created["status"], "planning")
        planned = self.service.record_workspace_plan({
            "taskId": created["taskId"],
            "providerModel": "test-planner",
            "plan": {
                "title": "迁移率模型验证", "summary": "等待用户确认。", "taskType": "tool-change",
                "tool": "sdevice", "assumptions": ["使用当前工程"],
                "steps": ["检索依据", "生成差异", "运行验证"],
                "expectedOutputs": ["报告"], "risks": ["参数需要标定"],
                "planInputs": [
                    {"key": "toleranceV", "label": "容差", "type": "number", "value": 0.01, "unit": "V", "required": True},
                    {"key": "method", "label": "方法", "type": "select", "value": "max_gm", "options": ["max_gm", "constant_current"], "required": True},
                ],
                "adaptiveSections": [{"type": "checklist", "title": "SDevice 路线", "items": ["先审查"]}],
            },
        })["task"]
        self.assertEqual(planned["status"], "approval-required")
        self.assertEqual(planned["workflow"][3]["status"], "active")
        approved = self.service.approve_workspace_task({
            "taskId": created["taskId"],
            "approvedInputs": {"toleranceV": "0.025", "method": "constant_current"},
        })["task"]
        self.assertEqual(approved["status"], "execution-ready")
        self.assertEqual(approved["workflow"][4]["status"], "active")
        self.assertEqual(approved["approvedInputs"]["toleranceV"], 0.025)
        started = self.service.begin_workspace_execution({"taskId": created["taskId"]})["task"]
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["runId"])
        updated = self.service.update_workspace_execution({
            "taskId": created["taskId"], "runId": started["runId"],
            "currentIteration": 13, "currentPhase": "sdevice", "node": 42,
            "message": "第 13 次迭代正在运行",
        })["task"]
        self.assertEqual(updated["currentIteration"], 13)
        self.assertEqual(updated["activeNode"], 42)
        cancelled = self.service.cancel_workspace_task({
            "taskId": created["taskId"], "runId": started["runId"],
            "message": "test stop",
        })["task"]
        self.assertEqual(cancelled["status"], "cancelled")
        late = self.service.record_workspace_outcome({
            "taskId": created["taskId"], "runId": started["runId"],
            "summary": "late completion must not win",
        })
        self.assertTrue(late["ignored"])
        self.assertEqual(late["task"]["status"], "cancelled")
        resumed = self.service.resume_workspace_task({"taskId": created["taskId"]})["task"]
        self.assertEqual(resumed["status"], "execution-ready")
        self.assertIsNone(resumed["runId"])

    def test_zz_stale_reconciliation_and_clear_history_preserve_live_process(self):
        stale = self.service.save_task({
            "project": "history/stale", "kind": "workspace-task", "question": "old run",
            "status": "running", "runId": "stale-run-1234", "activePid": 99999999,
            "events": [],
        })
        live = self.service.save_task({
            "project": "history/live", "kind": "workspace-task", "question": "live run",
            "status": "running", "runId": "live-run-12345", "activePid": os.getpid(),
            "events": [],
        })
        removable = self.service.save_task({
            "project": "history/removable", "kind": "workspace-task", "question": "done",
            "status": "completed", "events": [],
        })
        old_time = "2020-01-01T00:00:00Z"
        connection = self.service.database()
        try:
            for task in (stale, live):
                task["updatedAt"] = old_time
                connection.execute(
                    "UPDATE tasks SET updated_at=?, payload_json=? WHERE id=?",
                    (old_time, json.dumps(task, ensure_ascii=False, sort_keys=True), task["taskId"]),
                )
            connection.commit()
        finally:
            connection.close()

        corrected = self.service.reconcile_stale_tasks()
        self.assertIn(stale["taskId"], corrected)
        self.assertNotIn(live["taskId"], corrected)
        self.assertEqual(self.service.load_task(stale["taskId"])["status"], "interrupted")
        self.assertEqual(self.service.load_task(live["taskId"])["status"], "running")
        stale_group = next(item for item in self.service.project_history()
                           if item.get("project") == "history/stale")
        self.assertEqual(stale_group["displayStatus"], "interrupted")

        report_dir = pathlib.Path(self.service.REPORT_ROOT) / removable["taskId"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "task-report.md").write_text("test report", encoding="utf-8")
        result = self.service.clear_history()
        self.assertGreaterEqual(result["deletedTasks"], 2)
        self.assertGreaterEqual(result["deletedReports"], 1)
        self.assertGreaterEqual(result["keptActive"], 1)
        with self.assertRaises(self.service.ResearchError):
            self.service.load_task(removable["taskId"])
        self.assertEqual(self.service.load_task(live["taskId"])["status"], "running")
        self.assertFalse(report_dir.exists())


if __name__ == "__main__":
    unittest.main()
