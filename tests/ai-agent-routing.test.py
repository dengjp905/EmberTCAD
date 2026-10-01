#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import pathlib
import sys
import json
import tempfile
import unittest
from unittest import mock


COMPANION = pathlib.Path(os.environ.get("AITCAD_TEST_COMPANION") or (
    pathlib.Path(__file__).resolve().parents[1] / "linux" / "swb-companion"
))
sys.path.insert(0, str(COMPANION))

import ai_agent


class ModelResearchRoutingTests(unittest.TestCase):
    def test_routes_different_tools_and_models_to_one_research_pipeline(self):
        prompts = (
            "请根据 manual 和 tutorial 帮我在 sdevice 里加 trap 模型",
            "请查论文依据并修改 SProcess 氧化模型",
            "参考 User Guide 修改 SDE 网格 refinement",
            "根据 tutorial 修改 Inspect 阈值提取脚本",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(ai_agent.parse_model_research_intent(prompt))

    def test_does_not_steal_plain_chat_or_existing_vth_optimizer(self):
        self.assertFalse(ai_agent.parse_model_research_intent("帮我看看当前工程"))
        self.assertFalse(ai_agent.parse_model_research_intent("把 Vth 优化到 0.52 V"))

    def test_model_payload_removes_unpaired_surrogates(self):
        cleaned = ai_agent.sanitize_unicode_tree({"text": "正常中文\ud800尾部"})
        self.assertEqual(cleaned["text"], "正常中文?尾部")

    def test_legacy_config_infers_provider_and_new_config_persists_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "model.json"
            path.write_text(json.dumps({"baseUrl": "https://api.openai.com/v1", "model": "gpt-test",
                                        "apiKey": "secret"}), encoding="utf-8")
            with mock.patch.object(ai_agent, "CONFIG_PATH", str(path)):
                self.assertEqual(ai_agent.read_config()["provider"], "openai")
                saved = ai_agent.save_config({"provider": "gemini",
                                              "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                                              "model": "gemini-test", "apiKey": "new-secret",
                                              "apiStyle": "chat_completions", "reasoningEffort": "standard"})
                self.assertEqual(saved["provider"], "gemini")
                self.assertEqual(saved["apiStyle"], "chat_completions")
                self.assertEqual(saved["reasoningEffort"], "standard")
                self.assertNotIn("apiKey", saved)

    def test_deepseek_legacy_model_ids_migrate_to_current_flash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "model.json"
            path.write_text(json.dumps({"provider": "deepseek", "baseUrl": "https://api.deepseek.com",
                                        "model": "deepseek-reasoner", "apiKey": "secret"}), encoding="utf-8")
            with mock.patch.object(ai_agent, "CONFIG_PATH", str(path)):
                config = ai_agent.read_config()
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["reasoningEffort"], "high")

    def test_anthropic_adapter_normalizes_tool_calls(self):
        response = {
            "model": "claude-test", "usage": {"input_tokens": 12, "output_tokens": 8},
            "content": [{"type": "text", "text": "先读取工程"},
                        {"type": "tool_use", "id": "tool-1", "name": "get_project_state", "input": {}}],
        }
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")
        config = {"provider": "anthropic", "baseUrl": "https://api.anthropic.com/v1",
                  "model": "claude-test", "apiKey": "secret", "thinking": False,
                  "apiStyle": "anthropic_messages", "reasoningEffort": "auto"}
        with mock.patch.object(ai_agent.urllib.request, "urlopen", return_value=handle) as opened:
            payload, message = ai_agent.api_call(config, [{"role": "user", "content": "读取"}], ai_agent.TOOLS[:1])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(payload["model"], "claude-test")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "get_project_state")

    def test_openai_compatible_provider_uses_chat_completions(self):
        response = {"model": "gpt-test", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")
        config = {"provider": "custom", "baseUrl": "https://compatible.example/v1",
                  "model": "gpt-test", "apiKey": "secret", "thinking": False,
                  "apiStyle": "chat_completions", "reasoningEffort": "standard"}
        with mock.patch.object(ai_agent.urllib.request, "urlopen", return_value=handle) as opened:
            _payload, message = ai_agent.api_call(config, [{"role": "user", "content": "test"}])
        sent = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(opened.call_args.args[0].full_url, "https://compatible.example/v1/chat/completions")
        self.assertIn("简体中文", sent["messages"][0]["content"])
        self.assertEqual(message["content"], "OK")

    def test_openai_responses_adapter_selects_model_effort_and_normalizes_tools(self):
        response = {
            "id": "resp_1", "model": "gpt-6.1-sol",
            "usage": {"input_tokens": 20, "output_tokens": 11,
                      "output_tokens_details": {"reasoning_tokens": 5}},
            "output": [
                {"id": "rs_1", "type": "reasoning", "summary": []},
                {"id": "fc_1", "type": "function_call", "call_id": "call_1",
                 "name": "get_project_state", "arguments": "{}"},
            ],
        }
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")
        config = {"provider": "openai", "baseUrl": "https://api.openai.com/v1",
                  "model": "gpt-6.1-sol", "apiKey": "secret", "thinking": True,
                  "apiStyle": "responses", "reasoningEffort": "medium"}
        with mock.patch.object(ai_agent.urllib.request, "urlopen", return_value=handle) as opened:
            payload, message = ai_agent.api_call(config, [{"role": "user", "content": "读取工程"}],
                                                 ai_agent.TOOLS[:1])
        request = opened.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(sent["model"], "gpt-6.1-sol")
        self.assertEqual(sent["reasoning"]["effort"], "medium")
        self.assertEqual(sent["include"], ["reasoning.encrypted_content"])
        self.assertEqual(sent["tools"][0]["name"], "get_project_state")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(payload["usage"]["completion_tokens_details"]["reasoning_tokens"], 5)

    def test_openai_responses_replays_reasoning_and_function_output(self):
        history = [
            {"role": "assistant", "content": "", "provider_output": [
                {"id": "rs_1", "type": "reasoning", "summary": []},
                {"id": "fc_1", "type": "function_call", "call_id": "call_1",
                 "name": "get_project_state", "arguments": "{}"},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\": true}"},
        ]
        converted = ai_agent._responses_input(history)
        self.assertEqual(converted[0]["type"], "reasoning")
        self.assertEqual(converted[1]["type"], "function_call")
        self.assertEqual(converted[2]["type"], "function_call_output")
        self.assertEqual(converted[2]["call_id"], "call_1")

    def test_failed_node_diagnostics_are_collected_automatically(self):
        state = {"nodes": [
            {"node": 3, "tool": "sde", "toolType": "sde", "state": "failed"},
            {"node": 4, "tool": "sdevice", "toolType": "sdevice", "state": "done"},
        ]}
        diagnostic = {
            "summary": "syntax error", "location": {"file": "device_dvs.cmd", "line": 12},
            "offendingInput": ")", "files": [{
                "path": os.path.join(os.sep, "tmp", "project", "n3_sde.err"),
                "name": "n3_sde.err", "tail": "Error in device_dvs.cmd at line 12",
            }],
        }
        with mock.patch.object(ai_agent.swb_live, "collect_node_diagnostics", return_value=diagnostic) as collect:
            result = ai_agent.project_failure_diagnostics(os.path.join(os.sep, "tmp", "project"), state)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["node"], 3)
        self.assertEqual(result[0]["files"][0]["name"], "n3_sde.err")
        collect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
