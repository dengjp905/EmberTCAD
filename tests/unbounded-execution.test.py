#!/usr/bin/env python3
import importlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion"
sys.path.insert(0, str(APP_DIR))
agent = importlib.import_module("ai_agent")


class UnboundedExecutionTests(unittest.TestCase):
    def test_vth_controller_can_run_more_than_eight_iterations(self):
        values = [float(index) * 1e17 for index in range(1, 10)]
        state = {
            "parameters": [
                {"name": "con_pwell", "default": "1e17", "values": values},
                {"name": "Vd", "default": "0.1", "values": [0.1]},
            ]
        }
        extraction_count = {"value": 0}

        def extract(*_args, **_kwargs):
            extraction_count["value"] += 1
            return {"vth": float(extraction_count["value"])}

        with tempfile.TemporaryDirectory(prefix="spark-unbounded-") as project, \
                mock.patch.object(agent, "ensure_sde_parameter_binding", return_value={"file": "nmos_sde.cmd", "changed": False, "binding": "@con_pwell@"}), \
                mock.patch.object(agent, "source_dependency_mtime", return_value=0), \
                mock.patch.object(agent, "project_state", return_value=state), \
                mock.patch.object(agent, "wait_for_experiment_nodes", side_effect=lambda *_args: (state, extraction_count["value"] + 1, extraction_count["value"] + 101)), \
                mock.patch.object(agent.os.path, "isfile", return_value=True), \
                mock.patch.object(agent.os.path, "getmtime", return_value=100), \
                mock.patch.object(agent, "extract_threshold_voltage", side_effect=extract), \
                mock.patch.object(agent, "emit_event"):
            result = agent.optimize_threshold_voltage(project, {
                "targetV": 9.0, "toleranceV": 0.01, "parameterName": "con_pwell",
                "method": "max_gm", "drainBiasV": 0.1,
            })

        self.assertTrue(result["converged"])
        self.assertEqual(len(result["trials"]), 9)

    def test_general_agent_can_use_more_than_twelve_tool_rounds(self):
        calls = {"value": 0}

        def api_call(_config, _messages, _tools=None):
            calls["value"] += 1
            if calls["value"] <= 13:
                index = calls["value"]
                return ({"usage": {}, "model": "test"}, {
                    "content": "",
                    "tool_calls": [{
                        "id": "call-%d" % index,
                        "function": {"name": "get_project_status", "arguments": '{"round": %d}' % index},
                    }],
                })
            return ({"usage": {}, "model": "test"}, {"content": "completed after 13 tool rounds"})

        with tempfile.TemporaryDirectory(prefix="spark-agent-") as project, \
                mock.patch.object(agent, "read_config", return_value={"apiKey": "test", "model": "test", "thinking": True}), \
                mock.patch.object(agent.swb_live, "project_path", return_value=project), \
                mock.patch.object(agent, "project_state", return_value={}), \
                mock.patch.object(agent, "parse_model_research_intent", return_value=False), \
                mock.patch.object(agent, "parse_vth_optimization_intent", return_value=None), \
                mock.patch.object(agent, "api_call", side_effect=api_call), \
                mock.patch.object(agent, "execute_tool", side_effect=lambda _p, _n, args, _c: {"ok": True, "round": args["round"]}), \
                mock.patch.object(agent, "emit_event"):
            result = agent.chat({"project": project, "message": "perform a long general workflow"})

        self.assertEqual(len(result["tools"]), 13)
        self.assertIn("completed after 13", result["text"])


if __name__ == "__main__":
    unittest.main()
