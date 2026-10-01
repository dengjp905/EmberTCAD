# Contributing to EmberTCAD

感谢你帮助改进 EmberTCAD。Issue 和 Pull Request 都欢迎，尤其欢迎不同器件、Tool 链、Linux 发行版与 Sentaurus 版本的真实测试记录。

## 提交问题

请尽量提供：

- Linux 发行版与版本；
- Sentaurus 版本；
- EmberTCAD 版本；
- 使用的模型服务、模型 ID 与接口协议（不要提交 API Key）；
- 可复现步骤、错误信息和脱敏后的日志；
- 任务处于工程读取、方案生成、文件写入还是节点执行阶段。

请勿上传 Synopsys Manual、Tutorial、许可证文件、未获授权的工程数据或任何凭据。

## 本地检查

```bash
python3 tests/release-readiness.test.py
python3 tests/generated-project.test.py
python3 tests/connector-safety.test.py
python3 tests/research-service.test.py
python3 tests/ai-agent-routing.test.py
bash tests/install-compatibility.test.sh
bash tests/install-fresh-home.test.sh
```

涉及真实 SWB 的测试必须使用隔离工程，不能覆盖用户现有项目。

## Pull Request

- 一个 PR 尽量只解决一类问题。
- 说明改变了什么、为什么改变、如何验证。
- 新增行为应包含相应测试。
- 不要提交本地模型配置、任务历史、运行产物、日志或专有文档。
