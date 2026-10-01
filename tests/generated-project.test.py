#!/usr/bin/env python3
import pathlib
import hashlib
import sys
import tempfile
import unittest
from unittest import mock


COMPANION = pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion"
sys.path.insert(0, str(COMPANION))
import ai_agent
import research_service


class GeneratedProjectWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spark-generation-test-")
        self.root = pathlib.Path(self.temporary.name)
        self.patches = [
            mock.patch.object(research_service, "APP_DATA_ROOT", str(self.root / "app")),
            mock.patch.object(research_service, "DATABASE_PATH", str(self.root / "app" / "tasks.sqlite3")),
            mock.patch.object(research_service, "REPORT_ROOT", str(self.root / "app" / "reports")),
            mock.patch.object(research_service, "WORKSPACE_ROOT", str(self.root / "STDB")),
        ]
        for patch in self.patches:
            patch.start()
        (self.root / "STDB").mkdir()
        self.task = research_service.create_generation_task({
            "goal": "创建二维 PN 结，计算反向偏压下的电场分布，并核对物理依据"
        })["task"]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def test_missing_model_fails_without_fake_blueprint(self):
        with mock.patch.object(ai_agent, "read_config", return_value={"apiKey": ""}):
            with self.assertRaisesRegex(RuntimeError, "配置"):
                ai_agent.plan_generated_project({"taskId": self.task["taskId"]})
        self.assertEqual(research_service.load_task(self.task["taskId"])["status"], "planning")

    def test_structured_model_call_repairs_malformed_json_once(self):
        invalid = ({"model": "mock"}, {"content": '{"title":"plan" "steps":[]}'})
        repaired = ({"model": "mock"}, {"content": '{"title":"plan","steps":[]}'})
        with mock.patch.object(ai_agent, "api_call", side_effect=[invalid, repaired]) as call, mock.patch.object(
            ai_agent, "emit_event"
        ) as event:
            _payload, _message, result = ai_agent.api_json_call(
                {"apiKey": "configured"}, [{"role": "user", "content": "return json"}], "test plan"
            )
        self.assertEqual(result["title"], "plan")
        self.assertEqual(call.call_count, 2)
        self.assertTrue(event.called)

    def test_clarification_then_two_distinct_tool_chains(self):
        with mock.patch.object(ai_agent, "read_config", return_value={"apiKey": "configured", "model": "mock"}):
            with mock.patch.object(ai_agent, "api_call", return_value=({}, {"content": '{"questions":["请确认器件极性和端子"]}'})):
                result = ai_agent.plan_generated_project({"taskId": self.task["taskId"]})
            self.assertIn("端子", result["clarification"][0])
            research_service.record_generation_plan({"taskId": self.task["taskId"], "clarification": result["clarification"]})
            research_service.revise_generation_goal({"taskId": self.task["taskId"], "answer": "p 区在左，n 区在右"})
            for chain in ([{"step": "geometry", "tool": "sde"}],
                          [{"step": "process", "tool": "sprocess"}, {"step": "device", "tool": "sdevice"}]):
                proposal = {"questions": [], "name": "diode_research", "toolChain": chain}
                files = ([{"path": "geometry_dvs.cmd", "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")"}]
                         if len(chain) == 1 else [
                             {"path": "process_fps.cmd", "content": "line x location=0 spacing=0.1"},
                             {"path": "device_des.cmd", "content": 'File { Grid="@tdr|process@" }\nPhysics { Mobility }'},
                         ])
                blueprint = {"name": "diode_research", "toolChain": chain,
                             "files": files,
                             "validationPlan": ["运行节点，检查日志和 .tdr"], "summary": "独立生成"}
                responses = [({}, {"content": __import__("json").dumps(proposal)}),
                             ({}, {"content": __import__("json").dumps(blueprint)})]
                with mock.patch.object(ai_agent, "api_call", side_effect=responses), mock.patch.object(
                    research_service, "research_sources", return_value={
                        "manualResults": [{"kind": "manual", "title": "official", "location": "p.7"}],
                        "tutorialResults": [], "articleResults": [],
                    }):
                    result = ai_agent.plan_generated_project({"taskId": self.task["taskId"]})
                self.assertEqual(result["blueprint"]["toolChain"], chain)
                self.assertEqual(len(result["blueprint"]["evidence"]), len(chain))
                self.assertNotIn("templateId", result["blueprint"])

    def test_generation_repairs_unapproved_tool_chain_change(self):
        chain = [{"step": "geometry", "tool": "sde"}]
        proposal = {"questions": [], "name": "diode_research", "toolChain": chain}
        invalid = {
            "name": "diode_research",
            "toolChain": chain + [{"step": "viewer", "tool": "svisual"}],
            "files": [
                {"path": "geometry_dvs.cmd", "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")"},
                {"path": "viewer_vis.tcl", "content": "puts viewer"},
            ],
            "summary": "模型擅自增加了没有检索依据的 Tool",
            "validationPlan": ["运行 SDE 并检查网格 TDR"],
        }
        repaired = {
            "name": "diode_research", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd", "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")"}],
            "summary": "只保留已有证据的 SDE Tool",
            "validationPlan": ["运行 SDE 并检查网格 TDR"],
        }
        responses = [
            ({}, {"content": __import__("json").dumps(proposal)}),
            ({}, {"content": __import__("json").dumps(invalid)}),
            ({}, {"content": __import__("json").dumps(repaired)}),
        ]
        with mock.patch.object(ai_agent, "read_config", return_value={"apiKey": "configured", "model": "mock"}), mock.patch.object(
            ai_agent, "api_call", side_effect=responses
        ) as api_mock, mock.patch.object(research_service, "research_sources", return_value={
            "manualResults": [{"kind": "manual", "title": "official", "location": "p.7"}],
            "tutorialResults": [], "articleResults": [],
        }):
            result = ai_agent.plan_generated_project({"taskId": self.task["taskId"]})
        self.assertEqual(result["blueprint"]["toolChain"], chain)
        self.assertEqual(api_mock.call_count, 3)
        repair_request = __import__("json").loads(api_mock.call_args_list[2][0][1][1]["content"])
        self.assertEqual(repair_request["requiredToolChain"], chain)

    def test_generation_repairs_wrong_sde_build_mesh_signature(self):
        chain = [{"step": "geometry", "tool": "sde"}]
        proposal = {"questions": [], "name": "mesh_research", "toolChain": chain}
        invalid = {
            "name": "mesh_research", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd",
                       "content": "(sde:clear)\n(sde:build-mesh \"sde_mesh\" \"custom_output\")"}],
            "summary": "错误使用额外 build-mesh 参数",
            "validationPlan": ["运行 SDE 并检查网格 TDR"],
        }
        repaired = {
            "name": "mesh_research", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd",
                       "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")"}],
            "summary": "使用当前版本 SWB 网格输出形式",
            "validationPlan": ["运行 SDE 并检查 n1_msh.tdr"],
        }
        responses = [
            ({}, {"content": __import__("json").dumps(proposal)}),
            ({}, {"content": __import__("json").dumps(invalid)}),
            ({}, {"content": __import__("json").dumps(repaired)}),
        ]
        with mock.patch.object(ai_agent, "read_config", return_value={"apiKey": "configured", "model": "mock"}), mock.patch.object(
            ai_agent, "api_call", side_effect=responses
        ) as api_mock, mock.patch.object(research_service, "research_sources", return_value={
            "manualResults": [],
            "tutorialResults": [{"kind": "tutorial", "title": "official SDE", "location": "SimpleDiode:61"}],
            "articleResults": [],
        }):
            result = ai_agent.plan_generated_project({"taskId": self.task["taskId"]})
        self.assertIn('(sde:build-mesh "n@node@_msh")', result["blueprint"]["files"][0]["content"])
        self.assertEqual(api_mock.call_count, 3)

    def test_sdevice_semicolon_and_missing_parameter_file_are_rejected(self):
        chain = [{"step": "device", "tool": "sdevice"}]
        blueprint = {
            "name": "broken_device", "toolChain": chain,
            "files": [{"path": "device_des.cmd", "content": "; invalid comment\nFile { Parameter=\"@parameter@\" }\n"}],
            "validationPlan": ["parse and run"],
        }
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("分号注释" in value for value in errors))
        self.assertTrue(any(".par" in value for value in errors))

    def test_sdevice_requires_named_upstream_tdr_dependency(self):
        chain = [{"step": "geometry", "tool": "sde"}, {"step": "device", "tool": "sdevice"}]
        base = {
            "name": "dependency_check", "toolChain": chain,
            "files": [
                {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                {"path": "device_des.cmd", "content": 'File { Grid="@tdr@" }\nPhysics { Mobility }\n'},
            ],
            "validationPlan": ["run representative path"],
        }
        errors = ai_agent.generated_blueprint_errors(base, chain)
        self.assertTrue(any("@tdr|geometry@" in value for value in errors))
        base["files"][1]["content"] = 'File { Grid="@tdr|geometry@" }\nPhysics { Mobility }\n'
        self.assertEqual(ai_agent.generated_blueprint_errors(base, chain), [])

    def test_sdevice_unknown_current_density_plot_is_rejected(self):
        chain = [{"step": "geometry", "tool": "sde"}, {"step": "device", "tool": "sdevice"}]
        blueprint = {
            "name": "plot_check", "toolChain": chain,
            "files": [
                {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                {"path": "device_des.cmd", "content": (
                    'File { Grid="@tdr|geometry@" }\nPlot { Potential CurrentDensity }\n'
                )},
            ],
            "validationPlan": ["parse SDevice"],
        }
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("CurrentDensity" in value for value in errors))

    def test_sde_profile_placement_arity_is_rejected(self):
        chain = [{"step": "geometry", "tool": "sde"}]
        blueprint = {
            "name": "broken_profile", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd", "content": (
                '(sdedr:define-constant-profile "Doping" "BoronActiveConcentration" 1e17)\n'
                '(sdedr:define-constant-profile-region "Doping" "PRegion")\n'
                '(sde:build-mesh "n@node@_msh")\n')}],
            "validationPlan": ["run SDE"],
        }
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("placement" in value for value in errors))

    def test_sde_doping_profile_wrong_namespace_is_rejected(self):
        chain = [{"step": "geometry", "tool": "sde"}]
        blueprint = {
            "name": "wrong_namespace", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd", "content": (
                '(sdegeo:define-constant-profile "Doping" "BoronActiveConcentration" 1e17)\n'
                '(sde:build-mesh "n@node@_msh")\n')}],
            "validationPlan": ["run SDE"],
        }
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("sdegeo" in value for value in errors))

    def test_sde_nonexistent_refine_mesh_command_is_rejected(self):
        chain = [{"step": "geometry", "tool": "sde"}]
        blueprint = {
            "name": "bad_refinement", "toolChain": chain,
            "files": [{"path": "geometry_dvs.cmd", "content": (
                '(sdedr:refine-mesh "window" "size")\n'
                '(sde:build-mesh "n@node@_msh")\n')}],
            "validationPlan": ["run SDE"],
        }
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("define-refinement-placement" in value for value in errors))
        blueprint["files"][0]["content"] = (
            '(sdedr:define-refinement-size "size" 0.1 0.1 0.05 0.05)\n'
            '(sdedr:define-refinement-placement "place" "size" (box 0 0 0 1 1 0))\n'
            '(sde:build-mesh "n@node@_msh")\n'
        )
        errors = ai_agent.generated_blueprint_errors(blueprint, chain)
        self.assertTrue(any("(box ...)" in value for value in errors))

    def test_validation_keeps_upstream_nodes_before_deep_parameter_leaf(self):
        state = {
            "parameters": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "nodes": [
                {"node": 1, "toolIndex": 0, "toolType": "sde", "parameterValues": [], "state": "done"},
                {"node": 2, "toolIndex": 1, "toolType": "snmesh", "parameterValues": [], "state": "done"},
                {"node": 7, "toolIndex": 2, "toolType": "sdevice", "parameterValues": [], "state": "none"},
                {"node": 6, "toolIndex": 2, "toolType": "sdevice", "parameterValues": ["1"], "state": "none"},
                {"node": 5, "toolIndex": 2, "toolType": "sdevice", "parameterValues": ["1", "2"], "state": "none"},
                {"node": 3, "toolIndex": 2, "toolType": "sdevice", "parameterValues": ["1", "2", "3"], "state": "done"},
            ],
        }
        launched = []
        def run_node(_project, node, **_kwargs):
            launched.append(node)
            return {"returncode": 0, "log": "run.log", "logTail": "0 failed"}
        with mock.patch.object(ai_agent.swb_live, "project_path", return_value=str(self.root)), mock.patch.object(
            ai_agent, "project_state", return_value=state
        ), mock.patch.object(ai_agent.swb_live, "run_node_wait", side_effect=run_node), mock.patch.object(
            ai_agent.swb_live, "queue_swb_refresh"
        ), mock.patch.object(
            ai_agent.os, "listdir", side_effect=[[], ["n3_des.plt"]]
        ):
            result = ai_agent.validate_generated_project({"project": str(self.root), "validationMode": "baseline"})
        self.assertTrue(result["validationPassed"])
        self.assertEqual(launched, [1, 2, 3])
        self.assertEqual([item["node"] for item in result["nodes"]], [1, 2, 3])

    def test_cancelled_draft_ignores_late_outcome(self):
        blueprint = {"name": "fresh_diode", "toolChain": [{"step": "geometry", "tool": "sde"}],
                     "files": [{"path": "geometry_dvs.cmd", "content": "(sde:clear)"}]}
        research_service.record_generation_plan({"taskId": self.task["taskId"], "blueprint": blueprint})
        research_service.approve_workspace_task({"taskId": self.task["taskId"]})
        running = research_service.begin_workspace_execution({"taskId": self.task["taskId"]})["task"]
        run_id = running["runId"]
        research_service.cancel_workspace_task({"taskId": self.task["taskId"], "runId": run_id})
        created = self.root / "STDB" / "aitcad_workspaces" / "fresh_diode"
        created.mkdir(parents=True)
        (created / "gtree.dat").write_text("# isolated generated project\n", encoding="utf-8")
        associated = research_service.associate_generated_project({
            "taskId": self.task["taskId"], "runId": run_id, "path": str(created)})["task"]
        self.assertEqual(associated["status"], "cancelled")
        self.assertEqual(associated["projectPath"], str(created))
        validation = research_service.record_generation_validation({
            "taskId": self.task["taskId"], "runId": run_id,
            "validation": {"nodes": [{"node": 1, "state": "done"}]}})
        self.assertTrue(validation["ignored"])
        outcome = research_service.record_workspace_outcome({"taskId": self.task["taskId"], "runId": run_id,
                                                              "summary": "late success", "failed": False})
        self.assertTrue(outcome["ignored"])
        self.assertEqual(outcome["task"]["status"], "cancelled")

    def test_multi_tool_diff_checks_every_original_and_never_writes_during_planning(self):
        project = self.root / "STDB" / "two_tool_project"
        project.mkdir()
        (project / "gtree.dat").write_text("# project\n", encoding="utf-8")
        files = {"geometry_dvs.cmd": "(sde:clear)\n", "device_des.cmd": "Physics { Mobility }\n"}
        targets = []
        for tool, filename in (("sde", "geometry_dvs.cmd"), ("sdevice", "device_des.cmd")):
            (project / filename).write_text(files[filename], encoding="utf-8")
            targets.append({"tool": tool, "relativePath": "two_tool_project/" + filename,
                            "sourceSha256": hashlib.sha256((project / filename).read_bytes()).hexdigest(), "step": 0})
        task = research_service.save_task({"kind": "tool-model", "status": "evidence-ready",
                                           "project": "two_tool_project", "projectPath": str(project),
                                           "question": "修改 SDE 和 SDevice", "target": targets[0], "targets": targets})
        proposal = {"summary": "两处代码变更", "modifications": [
            {"relativePath": targets[0]["relativePath"], "modifiedContent": "(sde:clear)\n(sde:set-process-up-direction \"+z\")\n"},
            {"relativePath": targets[1]["relativePath"], "modifiedContent": "Physics { Mobility( DopingDep ) Recombination(SRH) }\n"},
        ]}
        recorded = research_service.record_model_proposal({"taskId": task["taskId"], "proposal": proposal})["task"]
        self.assertEqual(len(recorded["modifications"]), 2)
        self.assertEqual((project / "geometry_dvs.cmd").read_text(), files["geometry_dvs.cmd"])
        self.assertEqual((project / "device_des.cmd").read_text(), files["device_des.cmd"])
        (project / "device_des.cmd").write_text("Physics { changed by user }\n", encoding="utf-8")
        with self.assertRaisesRegex(research_service.ResearchError, "发生变化"):
            research_service.record_model_proposal({"taskId": task["taskId"], "proposal": proposal})


if __name__ == "__main__":
    unittest.main()
