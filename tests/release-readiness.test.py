#!/usr/bin/env python3
"""Fast, offline checks for a publishable EmberTCAD source tree."""

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parent.parent


def text(path):
    return (ROOT / path).read_text(encoding="utf-8")


gtk = text("linux/swb-companion/native_assistant_gtk3.py")
core = text("linux/swb-companion/native_assistant.py")
service = text("linux/swb-companion/research_service.py")
connector = text("connector/aitcad_connector.py")
versions = {
    re.search(r'APP_VERSION\s*=\s*"([^"]+)"', gtk).group(1),
    re.search(r'APP_VERSION\s*=\s*"([^"]+)"', core).group(1),
    re.search(r'SERVICE_VERSION\s*=\s*"([^"]+)"', service).group(1),
}
assert len(versions) == 1, "native app/service versions differ: %r" % versions
assert versions == {"0.1.0"}, "public release version must be 0.1.0: %r" % versions
assert 'label(u"从零创建工程", "title-large")' in gtk
assert '从零创建 TCAD 工程' not in gtk
assert "实时连接" not in gtk and "正在连接" not in gtk
assert "live_label" not in gtk and "live_label" not in core
assert re.search(r'CONNECTOR_VERSION\s*=\s*"([^"]+)"', connector)

readme = text("README.md")
assert "git clone --depth 1" in readme and "bash ./linux/swb-companion/install.sh" in readme
assert "# EmberTCAD" in readme and "EmberTCAD" in readme
assert "Apache License 2.0" in readme
assert (ROOT / "LICENSE").is_file() and (ROOT / "NOTICE").is_file()
assert (ROOT / "linux/swb-companion/assets/embertcad-logo.png").is_file()
assert (ROOT / "linux/swb-companion/embertcad").is_file()
assert (ROOT / "linux/swb-companion/embertcad.desktop.in").is_file()
for legacy_path in (
    "linux/swb-companion/server.py",
    "linux/swb-companion/aitcad-swb",
    "linux/swb-companion/aitcad-swb.desktop.in",
    "linux/swb-companion/static",
    "linux/swb-companion/assets/spark-ai-logo.png",
):
    assert not (ROOT / legacy_path).exists(), "legacy runtime source remains: %s" % legacy_path
assert 'title="EmberTCAD"' in gtk
assert 'Open-source AI Assistant for Sentaurus TCAD' in gtk
assert not re.search(r"/home/[A-Za-z0-9_.-]+/", connector), "connector contains a developer home path"

secret_patterns = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r'(?i)(?:api[_-]?key|secret|password)\s*[=:]\s*["\'](?!configured|test)[^"\']{12,}["\']'),
)
scan_extensions = {".py", ".sh", ".md", ".mjs", ".ts", ".tsx", ".json", ".yml", ".yaml", ".tcl"}
for path in ROOT.rglob("*"):
    if not path.is_file() or path.suffix.lower() not in scan_extensions:
        continue
    if any(part in {".git", "node_modules", ".artifacts"} for part in path.parts):
        continue
    if path.name in {".aitcad-model.local.json", ".aitcad-model.local.json.tmp"} or path.name.startswith(".env"):
        continue
    content = path.read_text(encoding="utf-8", errors="ignore")
    for pattern in secret_patterns:
        assert not pattern.search(content), "possible committed secret in %s" % path.relative_to(ROOT)

print("RELEASE_READINESS_OK version=%s" % next(iter(versions)))
