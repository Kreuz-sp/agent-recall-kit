# MCP 接入（通用）

本包提供的桥：`agent-recall-kit-mcp`（stdio，一行一条 JSON-RPC 2.0，不用 Content-Length 分帧）。

它只做请求转换：宿主问什么，就转发给本机服务，再把 `status` / `citation` 原样返回；
**不维护图、不改排序、不额外补正文**。`ambiguous` / `insufficient` / `source_changed` 会原样透出。

## 起服务

```powershell
agent-recall-kit --config .\my-data\agent-recall-kit.json serve --port 18791
```

## 配置宿主

各宿主的配置格式不同，核心只有三项：

```
command: agent-recall-kit-mcp
args:    ["--config", "D:\\my-data\\agent-recall-kit.json", "--service", "http://127.0.0.1:18791"]
```

示例（Claude Desktop 风格）：

```json
{
  "mcpServers": {
    "agent-recall-kit": {
      "command": "agent-recall-kit-mcp",
      "args": ["--config", "D:/my-data/agent-recall-kit.json", "--service", "http://127.0.0.1:18791"]
    }
  }
}
```

- 连接码由桥自己从配置的 `view_root/access-token.txt` 读，不需要写进宿主配置；
- 一个宿主可以起多个桥进程，但都连**同一个**服务；
- 宿主崩溃/重启不影响服务本身。

## 工具

| 工具 | 用途 |
|---|---|
| `memory_query` | 查询；返回 `status`、条目、`citation`、`missing` |
| `memory_fetch` | 按 `unit_id` + `source_version` 取原文（版本不符会失败，不返回旧内容） |
| `memory_explain` | 看某次查询为什么选中这些资料（入口、候选数、分数、走过的边） |
| `memory_health` | 服务版本、`revision`、覆盖统计 |

## 边界

- 桥不推断用户意图，也不做"自动供给"（每轮自动把记忆塞进提示词）——那是宿主侧的事，默认不做；
- 不同宿主对 MCP 的支持程度不同，本项目未逐家验证；只保证"按上面的三项配置能连上本机服务"。
