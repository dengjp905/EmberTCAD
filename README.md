# EmberTCAD

**Open-source AI Assistant for Sentaurus TCAD**

> 当前版本：**v0.1.0** · Linux 原生桌面应用 · Apache-2.0

![EmberTCAD 任务工作台](Ember_TCAD_picture/01.png)

## 项目简介

**Ember** 意为余火。EmberTCAD 希望做的，就是把 AI 在 TCAD 工作流里点燃的那一点火星，变成真正能够落到工程中的工具。

EmberTCAD 是一款面向 **Synopsys Sentaurus TCAD / Sentaurus Workbench（SWB）** 的免费开源 AI 助手。它读取 SWB 工程，理解 Tool 链、源文件、参数、节点状态和已有结果；结合当前 Sentaurus 版本的本地 Manual / Tutorial 生成方案；经用户确认后，再修改参数或代码、运行节点、检查结果并保存报告。

除了接手已有工程，它也支持从自然语言需求和参考文献出发，规划并创建新的 TCAD 工程。

EmberTCAD 不是新的 TCAD 求解器，也不把模型生成的代码直接当作正确答案。它提供的是一条面向真实工程的、**可观察、可审查、可停止、可追溯**的 AI 工作链。

## 文档导航

- [为什么需要 EmberTCAD](#为什么需要-embertcad)
- [系统架构](#系统架构)
- [主要功能](#主要功能)
- [安装与启动](#安装与启动)
- [平台验证状态](#平台验证状态)
- [操作演示](#操作演示)
- [已知限制](#已知限制)

## 为什么需要 EmberTCAD

一个真实 TCAD 任务通常不仅是“写一段代码”，还包括工程读取、版本核对、手册检索、节点运行、日志诊断和结果验收。通用大模型能写代码，通用编码 Agent 也能操作电脑，但它们并不天然理解 SWB 的工程结构和验证规则。

| 使用方式 | 优点 | 主要缺口 |
| --- | --- | --- |
| 复制代码并询问大模型 | 简单、直接，适合解释语法和局部修改 | 模型看不到完整工程、节点状态和真实运行结果 |
| 使用 Codex 等通用 Agent 操作 TCAD | 灵活，适合有经验的工程师完成复杂自动化 | 需要自行组织上下文、权限、步骤、验证和历史记录 |
| **EmberTCAD** | 将工程理解、版本证据、方案审批、SWB 联动和任务归档整合为固定流程 | 目前仍是 v0.1.0，需要用户审查模型方案和仿真结果 |

EmberTCAD 的重点不是替代 Codex，而是补上 **AI 模型与真实 Sentaurus 工程之间的专业执行层**。

> 模型负责分析与规划；本地程序负责安全写入、SWB 操作、状态检查和结果留档。

## 系统架构

```mermaid
flowchart LR
    U[用户] --> UI[GTK3 原生桌面界面]
    UI --> P[工程解析器]
    UI --> M[版本匹配的手册检索]
    UI --> A[AI 规划器]
    UI --> H[本地任务与报告库]
    P --> S[安全执行与审批层]
    M --> A
    A --> S
    S --> C[SWB Connector]
    C --> W[Sentaurus Workbench]
    W --> T[SDE / SDevice / SVisual / Inspect]
    T --> H
```

- **工程与证据层**：在本机读取项目、Tool 链、参数和结果，并检索当前版本的 Manual / Tutorial。
- **AI 规划层**：接入 DeepSeek、OpenAI、Claude、Gemini 及兼容接口，生成工程理解、方案和修订建议。
- **安全执行层**：审批前不写入工程；执行时校验路径、文件指纹、目标工程与运行状态。
- **SWB Connector**：创建参数、刷新工程、提交节点、读取 `.sta` 和结果产物，并支持停止运行。
- **本地任务库**：保存阅读、方案、审批、运行、错误和 Markdown 报告。

## 主要功能

### 1. 任务工作台

选择已有 SWB 工程后，EmberTCAD 会先完成只读分析并生成工程报告。用户提出目标后，系统给出可调整、可审查的执行方案；只有获得批准，才会修改参数或源文件并运行真实节点。

### 2. 从零创建工程

用户只需描述器件和研究目标，也可以附加文本型 PDF。EmberTCAD 会询问缺失的物理条件，检索依据，生成工程蓝图、Tool 链与全部源文件，并在用户选择的目录中创建新工程。

### 3. 手册中心

检索当前 Sentaurus 版本对应的本地 Manual 和 Tutorial，返回手册、页码、示例路径与命中片段，帮助用户核对模型语法和参数依据。

### 4. 任务历史

按工程保存完整档案，包括工程阅读、方案版本、代码差异、运行过程、失败诊断和最终报告。每个 Project 只保留一条入口，也可以清空不再需要的记录。

### 5. 模型与版本设置

模型配置明确区分服务地址、模型 ID、接口协议和推理强度。EmberTCAD 也会扫描本机的 Sentaurus 安装，并让工程读取、手册检索、节点运行和新开的 SWB 使用用户选择的版本。

| 从自然语言创建工程 | 检索本机手册与教程 |
| --- | --- |
| ![从零创建工程入口](Ember_TCAD_picture/02.png) | ![手册中心](Ember_TCAD_picture/03.png) |

![工程档案与任务历史总览](Ember_TCAD_picture/04.png)

## 安装与启动

### 环境要求

- RHEL 兼容 Linux 图形桌面；当前主要测试 CentOS 7 与 Rocky Linux 8.10。
- 用户已经合法安装 Sentaurus TCAD / SWB，并可在当前账户下运行。
- Python、GTK3 / PyGObject 和基础系统工具；推荐先运行 `install.sh --check`，由脚本报告缺失项。
- 使用 Git 克隆仓库，或使用系统自带的 `curl` 与 `tar` 下载发布源码包。
- 一个可用的大模型 API。Sentaurus 与大模型服务均不包含在本项目中。

先进行只读环境检查：

```bash
git clone --depth 1 https://github.com/dengjp905/EmberTCAD.git
cd EmberTCAD
bash ./linux/swb-companion/install.sh --check
```

Rocky 等最小化安装没有预装 Git 时，可以直接下载公开版本源码包：

```bash
curl -fL https://github.com/dengjp905/EmberTCAD/archive/refs/tags/v0.1.0.tar.gz | tar -xz
cd EmberTCAD-0.1.0
bash ./linux/swb-companion/install.sh --check
```

确认无误后安装并启动：

```bash
bash ./linux/swb-companion/install.sh
EmberTCAD
```

安装发生在当前 Linux 用户目录，不会停止或修改已经运行的 SWB。小写命令 `embertcad` 同样可用。

自动探测不满足需要时，可以显式指定工作区和 Sentaurus 根目录：

```bash
bash ./linux/swb-companion/install.sh \
  --project-root "$HOME/STDB" \
  --sentaurus-root /usr/synopsys/sentaurus/O_2018.06-SP2
```

## 平台验证状态

| 平台 | 当前状态 |
| --- | --- |
| CentOS 7 + Sentaurus O-2018.06-SP2 | 已完成真实工程读取、模型规划、SWB 节点运行、停止和报告闭环验证 |
| Rocky Linux 8.10 + Sentaurus X-2025.06 | 已完成安装、GTK 界面、运行时发现、版本切换和本地手册检索验证 |
| AlmaLinux 8 | 提供安装和运行兼容路径，建议先运行 `install.sh --check` |
| Rocky Linux 9 / CentOS Stream 9 | 兼容目标，仍需在真实 Sentaurus 主机上继续验证 |

## 工作流与验证边界

两条工作流遵循同一原则：**先只读理解，再审查方案，获得批准后执行，最后以真实 SWB 节点和结果产物验收。**

### 已有工程

`选择工程 → AI 理解 → 描述目标 → 审查方案 → 执行验证 → 完成`

### 新建工程

`描述需求 → 依据与澄清 → 审查方案及文件 → 创建与验证 → 交付`

创建工程时可以选择三种验证级别：

1. **快速预检**：检查路径、文件结构、宏、依赖关系和已知版本语法，不运行仿真。
2. **标准验收**：运行一条有代表性的最短依赖路径，作为默认选择。
3. **完整验收**：运行全部叶节点，适合小型工程或最终确认。

“源文件已生成”“静态预检通过”和“真实仿真验证通过”是三个不同状态。EmberTCAD 只有在读取到真实节点状态与结果产物后，才会报告相应的数值验证结果。

如果同一工程已经在 SWB 中打开，EmberTCAD 会刷新该窗口；如果 SWB 正在使用其他工程，则为新工程打开独立窗口，不替换或关闭用户当前的工作。

## 操作演示

### 配置模型与 Sentaurus 版本

EmberTCAD 支持 DeepSeek、OpenAI、Claude、Gemini 和 OpenAI-compatible 服务。API Key 只用于账户认证，实际调用的模型由模型 ID 决定。

![模型与 Sentaurus 版本设置](Ember_TCAD_picture/05.png)

### 使用任务工作台处理已有工程

下面的演示从一个真实 NMOS SWB 工程开始。该工程使用 SDE 构建二维器件结构，通过 SDevice 计算 Id–Vg 曲线并提取阈值电压；P-well 掺杂浓度已经通过 `con_pwell` 参数引出。

![原始 NMOS SWB 工程](Ember_TCAD_picture/06.png)

用户选择目标 Project 后启动只读分析。EmberTCAD 依次清点文件与节点、读取 Tool 源文件，并调用已配置的模型理解工程。

![选择并读取工程](Ember_TCAD_picture/07.png)

| 实时显示工程阅读步骤 | 工程意图、Tool 链、参数和风险摘要 |
| --- | --- |
| ![AI 理解工程](Ember_TCAD_picture/08.png) | ![工程阅读报告](Ember_TCAD_picture/09.png) |

本例要求调整 `con_pwell`，使 NMOS 阈值电压进入 0.45–0.50 V。AI 先给出扫描范围、提取方法、固定条件、容差和执行步骤；参数可以修改，也可以要求 AI 重新规划。

| 描述目标 | 审查推荐方案 |
| --- | --- |
| ![描述优化目标](Ember_TCAD_picture/10.png) | ![审查方案](Ember_TCAD_picture/11.png) |

批准后，EmberTCAD 运行相应 SDE / SDevice 节点、提取阈值并评估结果。执行状态与新参数会同步出现在 SWB 中，用户可以随时观察或停止任务。

| EmberTCAD 中的执行与测量 | SWB 中同步生成的参数和节点 |
| --- | --- |
| ![执行验证](Ember_TCAD_picture/12.png) | ![SWB 实时联动](Ember_TCAD_picture/13.png) |

达到目标后，任务结束并保存每次迭代的参数、节点和测量结果。若任务需要修改 SDE、SDevice 或其他 Tool 源文件，方案审查阶段也会展示逐文件差异。

![任务完成](Ember_TCAD_picture/14.png)

### 从自然语言和论文创建新工程

第二个演示根据自然语言需求和一篇硅像素探测器论文，规划包含 SDE、SDevice、SVisual 和 Inspect 的新工程。

参考文献：S. Zhang et al., *Nuclear Instruments and Methods in Physics Research Section A*, 1063, 169287 (2024), [doi:10.1016/j.nima.2024.169287](https://doi.org/10.1016/j.nima.2024.169287)。

| 输入需求并附加 PDF | AI 集中询问缺失条件 |
| --- | --- |
| ![输入新工程需求](Ember_TCAD_picture/15.png) | ![关键条件澄清](Ember_TCAD_picture/16.png) |

用户回答后，AI 整理器件目标、物理模型、边界条件和预期结果。这个阶段只进行分析，不会写入工程。

| 生成工程方案 | 审查结构假设、Tool 链和目标目录 |
| --- | --- |
| ![生成工程方案](Ember_TCAD_picture/17.png) | ![工程蓝图](Ember_TCAD_picture/18.png) |

全部源文件会在写入前展示。用户可以检查 SDE 几何、参数、SDevice 物理模型和结果提取脚本，并选择验证级别。

| 审查生成文件 | 选择验证级别 |
| --- | --- |
| ![文件审查](Ember_TCAD_picture/19.png) | ![三种验证方式](Ember_TCAD_picture/20.png) |

正式创建前，EmberTCAD 再次显示目录、工程名、源文件数量、Tool 数量和验证方式。同名工程不会被直接覆盖。

| 最终创建确认 | 新工程在独立 SWB 窗口中打开 |
| --- | --- |
| ![创建确认](Ember_TCAD_picture/21.png) | ![工程创建与交付](Ember_TCAD_picture/22.png) |

下面是生成工程的结构与掺杂分布示例。该结果用于检查几何区域、电极和掺杂设置，最终物理结论仍需结合网格、模型、边界条件和完整仿真判断。

![生成工程的结构与掺杂分布](Ember_TCAD_picture/23.png)

### 手册检索与任务归档

| 检索当前版本的 Manual / Tutorial | 按工程查看完整任务档案 |
| --- | --- |
| ![Trap 模型证据检索](Ember_TCAD_picture/24.png) | ![工程档案与任务历史](Ember_TCAD_picture/25.png) |

## 数据与模型边界

- 模型凭据保存在 `~/.config/aitcad/model.json`，文件权限仅限当前用户。
- 任务与报告保存在 `~/.local/share/aitcad/research/`；升级不会删除工程、历史、报告或设置。
- 执行 AI 阅读和规划时，用户需求、必要的工程摘要、相关源文件与检索证据会发送给用户配置的模型服务商；EmberTCAD 不会替用户选择或托管模型账户。
- 用户选择的 PDF 会复制到本地私有参考库，并记录 SHA-256 指纹；只向配置的模型发送相关页面片段。
- 图片型扫描 PDF 在没有 OCR 时会被明确拒绝，不会被当作已经理解。
- 模型输出不能绕过用户审批、路径安全、源文件指纹、版本检查和真实节点验收。

## 已知限制

- v0.1.0 仍是初始版本。AI 生成的 SDE / SDevice 代码可能存在语法错误、版本差异或不合理的物理假设。
- 对运行时间长、节点数量多的复杂项目，完整闭环可能效率较低；建议先选择标准验收或限定代表性路径。
- 手册证据能减少模型凭空编写，但不能替代工程师对网格、边界条件、物理模型和收敛性的判断。
- 模型能力会影响首轮方案质量。更换模型可能改善规划与代码生成，但不能替代真实仿真验证。

现阶段更适合把 EmberTCAD 看作一名能够阅读工程、检索资料并操作 SWB 的 AI 助手，而不是替代 TCAD 工程师的全自动专家。

## 代码与测试

当前原生应用位于 `linux/swb-companion/`。仓库中的旧网页原型仅保留作迁移记录，不由安装脚本部署。

主要本地测试：

```bash
python3 tests/generated-project.test.py
python3 tests/connector-safety.test.py
python3 tests/research-service.test.py
python3 tests/ai-agent-routing.test.py
python3 tests/runtime-settings.test.py
python3 tests/unbounded-execution.test.py
```

以 `centos-` 开头的测试会调用真实 GTK / SWB 环境，只应在隔离测试工作区中运行。

实现细节参见 [原生应用说明](linux/swb-companion/README.md)。欢迎提交 Issue、测试记录和 Pull Request，尤其欢迎补充不同器件、Tool 链与 Sentaurus 版本的验证结果。

## License

EmberTCAD 使用 [Apache License 2.0](LICENSE)。再分发本项目或衍生源码时，请保留许可证与 [NOTICE](NOTICE)。

Sentaurus、Sentaurus Workbench、官方 Manual / Tutorial 及其许可证属于 Synopsys，不包含在本仓库中，用户需自行合法安装和持有。
