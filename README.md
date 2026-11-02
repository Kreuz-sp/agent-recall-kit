# She 记忆大脑（工作名 · 发布候选 0.1.0）

本地、可追溯的记忆服务预览版：把你的 Markdown/YAML 资料读成一张**带来源**的图，
在浏览器里全库浏览、按关系检索、逐条审核自动生成的关系候选；
程序只读你的原件，人工决定单独存放，可导出备份。

> **状态**：发布候选（RC），尚未发布。项目名、署名、许可证、仓库可见性由项目所有者决定，
> 本文与 `LICENSE` 里的占位**不是最终决定**。预览版本号 `0.1.0` 与包元数据一致。

**它不做什么**：不自带任何人格或聊天助手；不声称"任意问法都能准确召回"；
不把可编辑关系图说成神经网络或意识；不改写你的记忆原件。

---

## 它解决什么

- **来源可追溯**：每个节点要么是来源单元（带 `path:line` 与 `source_version`），要么是文档/对象/关系组；检索结果能点回原文并按版本校验。
- **全库总览**：一次取回整张图（当前演示库约 50 节点，作者自用库约 2800 节点），可缩放、平移、搜索定位、按类型筛选。
- **候选审核**：机器只提"可能同主题"的候选，必须人看两端原文再接受/拒绝；接受与否决都记在快照里，重启不丢。
- **只读来源**：`source-readonly` 模式下程序不写你的资料目录；人工决定写在数据目录的图快照里。
- **可选的 MCP 桥**：给支持 MCP 的宿主（如 Claude Desktop、Codex、DSH）提供查询/取原文工具；默认不启用"每轮自动供给"。

## 截图（来自本仓库的虚构演示库）

| 文件 | 内容 |
|---|---|
| `docs/images/overview.png` | 全库总览（适应画布） |
| `docs/images/local.png` | 主题局部（标注"显示 X/Y"） |
| `docs/images/candidate.png` | 候选审核：A/B 两端原文对照 |
| `docs/images/missing-source.png` | 一端来源缺失：接受被禁用，B 端仍可读 |

演示库是**纯虚构**的（人物、项目、设备、日期都是编的），见 `examples/demo-library/`。

## 五分钟快速开始（Windows，实测）

```powershell
# 1) 装
python -m venv .venv
.venv\Scripts\activate
pip install she_memory-0.1.0-py3-none-any.whl      # 或 pip install .

# 2) 建数据目录（配置、图状态、连接码都放这里，不写源码树）
she-memory init .\my-data

# 3) 想先看演示：把虚构演示库和示例 profile 拷进去
copy examples\demo-library\* .\my-data\library\ -Recurse
copy examples\config\profile-demo.json .\my-data\profile.json

# 4) 建图（唯一写图的入口）
she-memory --config .\my-data\she-memory.json build
# {"revision": 1, "units": 39, "nodes": 54, "edges": 75}

# 5) 启动（只监听 127.0.0.1；首次启动生成连接码）
she-memory --config .\my-data\she-memory.json serve --port 18791

# 6) 浏览器打开 http://127.0.0.1:18791
#    连接码在 .\my-data\data\runtime\access-token.txt
```

命令行也能用：

```powershell
she-memory --config .\my-data\she-memory.json query "滴灌阀 型号"
she-memory --config .\my-data\she-memory.json fetch --unit unit:... --version <sha256>
she-memory-ctl --config .\my-data\she-memory.json status
she-memory-doctor --config .\my-data\she-memory.json
```

## 已验证环境

| 项 | 实测 |
|---|---|
| 平台 | Windows 11（本机），Python 3.13.15 |
| 浏览器 | Microsoft Edge 156（headless 实跑：加载、缩放、拖拽、搜索、候选对照） |
| 安装 | 全新 venv + 安装 wheel，数据目录在 `%TEMP%` 下，与作者环境无引用 |
| 规模 | 演示库 54 节点；合成大图 3000 节点（`examples/generate_synthetic.py`） |
| 未验 | Linux/macOS 安装、非 Edge 浏览器、多进程同时写同一数据目录 |

## 目录结构

```
src/she_memory/       核心（阅读器、图、查询、HTTP 服务）与随包前端 web/
examples/demo-library/ 纯虚构演示资料
examples/config/       可直接使用的示例配置与演示 profile
adapters/              MCP 接入配置、可选 Windows 守护任务
tests/                 隔离测试（临时目录、临时端口）
docs/                  格式、profile、接口、运维、限制
```

## 文档

- 数据格式与单位身份：`docs/FORMAT.md`
- 领域规则（对象/目标/证据规则）怎么写：`docs/PROFILE.md`
- HTTP 接口与状态：`docs/API.md`
- 启停、体检、备份与恢复、可选守护：`docs/OPS.md`
- 已知限制（必读）：`docs/LIMITS.md`

## 安全与边界

- 默认只监听 `127.0.0.1`，每次启动时若没有连接码就在数据目录生成一个（`access-token.txt`），不随包分发、不写入仓库。
- 来源为**只读**：所有写操作只落在数据目录的图快照/布局文件里。
- 端口独占只保证"同一端口只有一个服务"；**不代表**同一数据目录可以被多个进程同时写——这一点未验证，见 `docs/LIMITS.md`。

## 待项目所有者决定

项目名、署名（作者/组织）、版本号策略、许可证（建议 MIT）、仓库可见性（先私有→何时公开）。
本仓库的 `LICENSE`、`pyproject.toml` 的 `name/authors/license` 都是占位。
