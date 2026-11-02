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


## 写元数据的工具：memory_feedback（与四个读取工具分开）

memory_feedback 记录一次**明确的使用反馈**，写的是项目侧元数据，不改原文：

| 参数 | 说明 |
|---|---|
| unit_id | 哪个单元 |
| expected_source_version | 当前版本（对不上会被拒） |
| event_id | 事件 id（重放不重复写） |
| 
esult | helpful 或 
ot_helpful |
| expected_revision | 当前图版本（并发保护） |

约定：只有调用者**确实得到结果反馈且获准**时才调用；不要求每次 query 后自动报 helpful；
读到了不等于验证事实为真。参数不合法会在协议层以 JSON-RPC -32602 拒绝。
