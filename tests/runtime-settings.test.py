#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import importlib.util
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "linux" / "swb-companion" / "native_assistant.py"


class SentaurusRuntimeSettingsTests(unittest.TestCase):
    def load_core(self, helper, sentaurus):
        os.environ["AITCAD_HELPER_PYTHON"] = str(helper)
        os.environ["AITCAD_RUN_STROOT"] = str(sentaurus)
        spec = importlib.util.spec_from_file_location("native_assistant_runtime_test", str(MODULE_PATH))
        module = importlib.util.module_from_spec(spec)
        fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_args: None)
        with mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}):
            spec.loader.exec_module(module)
        return module

    def test_detect_select_and_persist_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            helper = pathlib.Path(sys.executable)
            first = root / "O_2018.06-SP2"
            second = root / "X-2025.06"
            for release in (first, second):
                (release / "bin").mkdir(parents=True)
                for command in ("gtclsh", "swb", "gsub"):
                    path = release / "bin" / command
                    path.write_text("runtime", encoding="utf-8")
                    path.chmod(path.stat().st_mode | stat.S_IEXEC)
            core = self.load_core(helper, first)
            core.RUNTIME_CONFIG_PATH = str(root / "runtime.json")
            core.SENTAURUS_2018_ROOT = str(second)
            discovered = core.discover_sentaurus_installations()
            self.assertTrue(any(item["release"] == "X-2025.06" for item in discovered))
            selected = core.select_sentaurus_root(str(second))
            self.assertEqual(selected["selectedRelease"], "X-2025.06")
            self.assertEqual(os.environ["AITCAD_RUN_STROOT"], str(second.resolve()))
            self.assertEqual(json.loads(pathlib.Path(core.RUNTIME_CONFIG_PATH).read_text())["sentaurusRoot"], str(second.resolve()))

    def test_probe_accepts_a_fresh_empty_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            sentaurus = root / "X-2025.06"
            (sentaurus / "bin").mkdir(parents=True)
            for command in ("gtclsh", "swb", "gsub"):
                path = sentaurus / "bin" / command
                path.write_text("runtime", encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            core = self.load_core(pathlib.Path(sys.executable), sentaurus)
            status = {
                "workspace": {"root": str(root / "STDB2025")},
                "projects": [],
            }
            output = io.StringIO()
            with mock.patch.object(core, "rpc", return_value=status), contextlib.redirect_stdout(output):
                core.probe()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["activeProject"], "")
            self.assertEqual(payload["projects"], [])
            self.assertEqual(payload["entries"], 0)


if __name__ == "__main__":
    unittest.main()
