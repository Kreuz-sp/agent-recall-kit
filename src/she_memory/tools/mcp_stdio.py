"""记忆大脑的 MCP Server（stdio 桥接版）。

职责边界（2026-11-01 用户定）：
  · 只做**请求转换与证据返回**，不维护图、不排序、不碰人格；
  · 每个 Agent 可以起自己的桥接进程，但都连**同一个**记忆服务（默认 127.0.0.1:18791）；
  · ambiguous / insufficient / source_changed 等状态原样透出，不在包装层变成「查到了」。

四个工具：memory_query / memory_fetch / memory_explain / memory_health

协议（MCP stdio，2025-06-18）：一行一条 JSON-RPC 2.0，**不用 Content-Length 分帧**。
错误分两层，别混：
  · JSON-RPC 错误（-32700 解析失败 / -32600 非法请求 / -32602 参数或调用结构不合法 / -32601 方法不存在）
  · 工具结果里的 isError（业务失败：not_found / source_changed / service_unavailable）

启动：
    agent-recall-kit-mcp --config <数据目录>/agent-recall-kit.json --service http://127.0.0.1:18791
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL = "2025-06-18"
from she_memory import __version__  # noqa: E402
SERVER_INFO = {"name": "agent-recall-kit", "version": __version__}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class Service:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data,
                                         method="GET" if payload is None else "POST")
        request.add_header("Authorization", "Bearer " + self.token)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return {"__error__": json.loads(body).get("error", {}).get("code", "http_%d" % exc.code)}
            except ValueError:
                return {"__error__": "http_%d" % exc.code}
        except Exception as exc:  # 连接不上 ≠ 库里没有
            return {"__error__": "service_unavailable", "detail": type(exc).__name__}


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required}}


TOOLS = [
    _tool("memory_query",
          "查询记忆库：返回状态（ok/ambiguous/insufficient/no_match/navigation）、证据正文、来源路径与关系路径。缺证据时如实返回缺项，不猜。",
          {"text": {"type": "string", "description": "要查的问题，用自然语言"},
           "max_items": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
           "focus_terms": {"type": "array", "items": {"type": "string"},
                           "description": "可选：上一轮的话题焦点，用于「那个呢」这类指代"}},
          ["text"]),
    _tool("memory_fetch",
          "按单元 ID 与预期版本读取原文。原件改过会返回 source_changed，不会拿旧版冒充。",
          {"unit_id": {"type": "string"}, "expected_source_version": {"type": "string"}},
          ["unit_id", "expected_source_version"]),
    _tool("memory_explain",
          "查看最近一次查询为什么选中这些资料：入口、候选数、最高分、走了哪些边。",
          {"query_id": {"type": "string"}}, ["query_id"]),
    _tool("memory_health", "服务状态：图版本、节点/边数量、来源覆盖、可用能力。", {}, []),
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


def validate(name, args):
    """按 inputSchema 真校验——声明不等于执行过，所以这里逐项检查。"""
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    schema = TOOL_MAP[name]["inputSchema"]
    props = schema["properties"]
    problems = []
    for key in schema.get("required", []):
        if key not in args:
            problems.append("missing required: %s" % key)
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            problems.append("unknown argument: %s" % key)
            continue
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                problems.append("%s must be string" % key)
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append("%s must be integer" % key)
            else:
                low, high = spec.get("minimum"), spec.get("maximum")
                if low is not None and value < low:
                    problems.append("%s must be >= %s" % (key, low))
                if high is not None and value > high:
                    problems.append("%s must be <= %s" % (key, high))
        elif kind == "array":
            if not isinstance(value, list):
                problems.append("%s must be array" % key)
            else:
                item_type = (spec.get("items") or {}).get("type")
                if item_type == "string" and any(not isinstance(x, str) for x in value):
                    problems.append("%s items must be strings" % key)
    return problems


def tool_result(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
            "isError": is_error}


def dispatch(service, name, args):
    if name == "memory_query":
        payload = {"text": args["text"], "max_items": args.get("max_items", 3)}
        if args.get("focus_terms"):
            payload["context"] = {"focus_terms": args["focus_terms"]}
        result = service.call("/v1/query", payload)
    elif name == "memory_fetch":
        result = service.call("/v1/fetch", {"unit_id": args["unit_id"],
                                            "expected_source_version": args["expected_source_version"]})
    elif name == "memory_explain":
        result = service.call("/v1/queries/%s/explain" % args["query_id"])
    elif name == "memory_health":
        result = service.call("/v1/health")
    else:
        raise KeyError(name)
    return tool_result(result, bool(result.get("__error__")))


def write_message(message):
    # 一行一条 JSON（MCP stdio）；必须走 binary，否则 Windows 会把 \n 翻成 \r\n
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    out.write(body + b"\n")
    out.flush()


def error(mid, code, message, data=None):
    payload = {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
    if data:
        payload["error"]["data"] = data
    return payload


def handle_tools_call(service, mid, params):
    """调用结构先校验：params 非对象、name 非字符串都直接 -32602，绝不让异常把进程带崩。"""
    if not isinstance(params, dict):
        return error(mid, INVALID_PARAMS, "params must be an object")
    name = params.get("name")
    if not isinstance(name, str):
        return error(mid, INVALID_PARAMS, "name must be a string")
    if name not in TOOL_MAP:
        return error(mid, INVALID_PARAMS, "unknown tool: %s" % name)
    # arguments 只在**缺省**时补 {}；传了别的类型就原样交给校验去拒
    arguments = params["arguments"] if "arguments" in params else {}
    problems = validate(name, arguments)
    if problems:
        return error(mid, INVALID_PARAMS, "invalid arguments: " + "; ".join(problems),
                     {"tool": name, "problems": problems})
    try:
        return {"jsonrpc": "2.0", "id": mid, "result": dispatch(service, name, arguments)}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": mid,
                "result": tool_result({"__error__": "internal", "detail": type(exc).__name__}, True)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="agent-recall-kit.json（用来读连接码与端口）")
    ap.add_argument("--service", default="http://127.0.0.1:18791")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    view_root = Path(cfg.get("view_root", "./data/runtime"))
    if not view_root.is_absolute():
        view_root = (Path(args.config).parent / view_root).resolve()
    token_file = view_root / "access-token.txt"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.is_file() else ""
    service = Service(args.service, token)

    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip().lstrip("\ufeff")
        if not text:
            continue

        try:
            message = json.loads(text)
        except ValueError as exc:
            write_message(error(None, PARSE_ERROR, "parse error", str(exc)[:120]))
            continue

        if not isinstance(message, dict):
            write_message(error(None, INVALID_REQUEST, "request must be a JSON object"))
            continue
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            write_message(error(message.get("id"), INVALID_REQUEST, "invalid JSON-RPC request"))
            continue

        method = message["method"]
        mid = message.get("id")
        if "id" not in message:      # 通知：不产生响应
            continue

        if method == "initialize":
            write_message({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO}})
        elif method == "tools/list":
            write_message({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            write_message(handle_tools_call(service, mid, message.get("params")))
        elif method == "ping":
            write_message({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            write_message(error(mid, METHOD_NOT_FOUND, "method not found: %s" % method))


if __name__ == "__main__":
    main()