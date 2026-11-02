# Agent Recall Kit

**简体中文** | [English](README.en.md)

### 不止于记录，更在于回忆。

*Beyond storing memories—recalling them when they matter.*

[快速开始](#快速开始) · [接入 Agent](#接入-agent) · [文档](#文档) · [MIT License](LICENSE)

---

## What is Memory?

什么是「记忆」？

在中文中，我认为「记忆」可以理解为两个部分：**记 + 忆**。

记，是记录和保存经历；忆，是在需要的时候重新找回经历。

我的上一个项目 [Agent Memory Kit](https://github.com/Kreuz-sp/agent-memory-kit) 分享了一套面向 Agent 的记忆库架构，主要解决了「记」的问题：让 Agent 能够记录、组织和保存自己的经历。

**但记下来，不代表能够想起来。**

随着记忆不断积累，如何在需要的时候重新找到它们？如何发现不同记忆之间的联系？又如何让过去的经历在新的交互中重新发挥作用？

这就是 Agent Recall Kit 想要探索的「忆」。

## What is Agent Recall Kit?

Agent Recall Kit 是一套面向个人 Agent、可独立运行的本地记忆工具包，提供记忆关联、检索与可视化管理能力。

它延续了 Agent Memory Kit 的记忆库设计思路，在 Markdown / YAML 原件之上建立关系图谱，帮助 Agent 找回相关经历、核对来源，并将过去的经验用于当前任务。

项目自带 MCP 接口，设计上不绑定特定的 LLM 或 Agent。支持 MCP 的宿主可以接入同一份本地记忆服务。

目前已在 **DSH、Codex 和 Cherry Studio** 中验证 MCP 接入与记忆读取。不同版本、宿主和模型的具体支持仍需按配置验证；接通工具也不等于每轮自动调用。

## Why Another Memory System?

在我设想的个人 Agent 中，认知与记忆应该是两个相对独立的部分：

- **Cognitive Brain（认知大脑）**：由 LLM 提供，负责理解、推理、规划与表达。
- **Memory Brain（记忆大脑）**：负责经历的保存、组织、关联与检索。

认知模型可以更换，会话可以结束，但积累下来的记忆应该能够继续承接。

我们可以更换一个 Agent 的思考方式，却不必因此重新开始认识它。

Agent Recall Kit 正是基于这样的想法而诞生的。

## A Personal Project, An Open Exploration

这个项目最初源于我对个人陪伴型 Agent 的探索。

我希望分享的不只是一个可以使用的工具，也是一种关于 Agent 长期记忆的设计思路。

目前的实现还有许多值得探索和改进的地方。至于它最终能够发展成什么样，我希望交给更多开发者共同探索。

欢迎试用、交流、提出问题，也欢迎分享你的见解。

**让 Agent 不止能够记下经历，也能够重新忆起经历。**

---

## 看记忆如何长出联系

[![点击观看记忆网络的生长演示](docs/media/recall-growth-poster.jpg)](docs/media/recall-growth.mp4)

[观看 20 秒视频](docs/media/recall-growth.mp4) · 无声 · 1080p

视频展示作者实际图谱的规模与形态，约 2800 个节点、4800 条关系，不展示节点标识、标题或记忆正文。生长顺序是示意编排，并非历史录像。下方界面截图均来自仓库内的虚构演示库。

## 它能帮 Agent 记住什么

项目里的决定、某次操作为什么失败、已经验证过的方法、仍未办完的事——这些资料不必散落在不同聊天窗口里。

例如，在自带的演示库中：

| 你问 | 程序提供 |
|---|---|
| 滴灌阀型号是什么？ | 对应的事实记录与来源位置 |
| 控制器固件版本是多少？ | 配置记录中的版本信息 |
| 低温卡涩的结论是什么？ | 相关经历与结论的原文 |

程序返回资料和证据，由接入的模型理解并回答。结果不足或存在多个候选时，也会保留缺项与歧义。

### 找回过去，并知道它从哪里来

每条来源单元保留文件、定位和版本。需要核对时，可以取回完整原文；文件发生变化后，旧版本读取会被拒绝，避免把缓存当成当前记录。

### 看见记忆之间的关系

全库图谱支持缩放、搜索、拖动、保存布局及关系编辑。程序会提出可能相关的记忆，由你对照两端原文后接受或拒绝；人工判断会持久保存。

![全库图谱与来源详情](docs/images/overview.png)

<details>
<summary>查看关系候选审核</summary>

![对照两条记忆的原文，审核关联建议](docs/images/candidate.png)

共同词只是关联线索，不代表两条记录说的是同一事件。

</details>

### 让普通经历慢慢沉下去，需要时仍能找回

普通经历会随时间降低召回优先级，原文仍然保留。重要事实、知识、经验、待办和承诺默认受保护，也可以手动保护某条经历。

明确查旧事会绕过衰减。真正用上某条记忆后，可以反馈“有帮助”使它重新活跃；仅仅查到、读过或点击，不会自动强化。

<details>
<summary>淡忘如何影响排序？</summary>

默认权重为：

```text
w(t) = 0.35 + 0.65 × 2^(−t/90)
```

`t` 是距离记忆时间或最近一次有效强化的天数。高于保底值的部分每 90 天减半，权重最低保留 0.35。演示 profile 使用 60 天，便于观察效果；这些参数可以配置。

先按基础相关度相对本次最高分分层：强相关 ≥90%，相关 ≥60% 且 <90%，弱相关 <60%。跨层保持顺序，层内按“基础相关度 × 活跃权重”排序；同层中较新的记录可能超过分数稍高的旧记录。年龄本身不消解歧义，也不把有资格返回的记录过滤掉。

时间依据依次是显式元数据、条目文件名/标题中的日期、首次入库时间，不使用文件修改时间。明确历史查询、单元定位或完整引文命中会绕过衰减，但不更新强化时间。

保护期间权重为 1，取消保护后回到原时间轨迹。只有有效的 `helpful` 反馈会重新激活，同一单元同一 UTC 日最多一次；`not_helpful` 只记录反馈、不降权。反馈事件绑定单元、来源版本和结果，重复事件不重复写入，不同内容复用事件 ID 会报冲突。

![查看记忆的活跃程度与时间依据](docs/images/activation-detail.png)

</details>

## 快速开始

当前版本为 **0.1.0 预览版**。已验证 Windows 11、Python 3.12/3.13 和 Edge；Linux/macOS 尚未验证。

在 PowerShell 中运行：

```powershell
git clone https://github.com/Kreuz-sp/agent-recall-kit.git
cd agent-recall-kit

python -m venv .venv
.venv\Scripts\python.exe -m pip install .

# 创建一份自带虚构资料的数据目录
.venv\Scripts\agent-recall-kit.exe init .\my-data --demo

# 从资料建立图谱
.venv\Scripts\agent-recall-kit.exe --config .\my-data\agent-recall-kit.json build

# 启动本地服务
.venv\Scripts\agent-recall-kit.exe --config .\my-data\agent-recall-kit.json serve --port 18791
```

打开 **http://127.0.0.1:18791**，将以下文件中的连接码填入页面：

```text
my-data/data/runtime/access-token.txt
```

服务运行期间保持终端开启，按 `Ctrl+C` 停止。需要后台启停或 Windows 守护任务时，见[运维说明](docs/OPS.md)。

数据与安装目录分开存放：

```text
my-data/
  agent-recall-kit.json   配置
  library/               Markdown / YAML 原件
  data/graph/            图谱、人工判断和激活状态
  data/runtime/          连接码、布局和运行状态
```

要使用自己的资料，去掉 `--demo` 创建空库，按[数据格式](docs/FORMAT.md)整理文件，并配置适合自己领域的 [profile](docs/PROFILE.md)。修改资料后重新运行 `build`，或在页面点击“重新读取来源”。

## 接入 Agent

记忆服务在本地运行，MCP stdio 桥把它提供给宿主。多个宿主可以共用同一服务。

```text
Agent / MCP 宿主 → stdio 桥 → 本地记忆服务 → 资料库与关系图
                                  ↑
                              浏览器管理界面
```

在宿主的 MCP 设置中填写命令与参数，使用你自己的绝对路径：

```text
命令：<仓库路径>/.venv/Scripts/agent-recall-kit-mcp.exe
参数：--config <数据目录>/agent-recall-kit.json --service http://127.0.0.1:18791
```

| 工具 | 用途 |
|---|---|
| `memory_query` | 查找相关记忆、来源和关系路径 |
| `memory_fetch` | 按单元 ID 与版本读取原文 |
| `memory_explain` | 查看检索依据 |
| `memory_health` | 检查连接和服务状态 |
| `memory_feedback` | 明确反馈某条记忆是否有帮助，写入激活元数据 |

完整 JSON 配置见 [MCP 接入说明](adapters/mcp/README.md)。接入与读取已在 DSH、Codex、Cherry Studio 中验证，具体宿主版本和模型需按配置确认。接通 MCP 后由模型或宿主决定何时调用；本版本不附带每轮自动供给。

## 数据由你保管

- 来源目录只读，图谱、布局、反馈等状态另存。普通经历的淡忘也不会删除原件。
- 服务默认只监听本机，连接码在本机生成。需要迁移或留副本时，使用[备份与恢复](docs/OPS.md)。
- 领域别名和证据规则需要自行配置；关系候选需要审核，检索仍可能遗漏或返回不相关资料。

预览版接口和数据格式仍可能变化。同一数据目录多进程写入、其它操作系统和浏览器尚未完整验证，详细边界见[已知限制](docs/LIMITS.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [数据格式](docs/FORMAT.md) | 资料如何组织、来源单元与版本 |
| [领域配置](docs/PROFILE.md) | 对象别名、目标字段、检索与证据规则 |
| [HTTP API](docs/API.md) | 查询、图谱、反馈与错误码 |
| [MCP 接入](adapters/mcp/README.md) | 命令、参数和工具说明 |
| [运维](docs/OPS.md) | 启停、体检、备份、恢复与守护 |
| [已知限制](docs/LIMITS.md) | 当前已验证范围与未实现部分 |

## 许可

[MIT](LICENSE) · Copyright © 2026 Kreuz-sp

Python 内部导入名仍为 `she_memory`；安装包名与命令前缀为 `agent-recall-kit`。
