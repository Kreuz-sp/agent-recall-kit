"""HTTP / MCP 层的活跃度接口：反馈、保护、校验与 MCP 工具入参。"""
import json
import subprocess
import sys

from conftest import Server, brain, free_port


def _old_unit(server):
    server.request("/v1/refresh", {})
    status, view = server.request("/v1/graph/view", {"mode": "overview"})
    assert status == 200
    unit = next(n for n in view["nodes"] if n["id"].startswith("unit:"))
    return view, unit


def test_feedback_http_roundtrip(workspace):
    srv = Server(workspace["config"], free_port())
    try:
        view, unit = _old_unit(srv)
        srv.request("/v1/refresh", {})
        status, health = srv.request("/v1/health")
        revision = health["revision"]
        payload = {"unit_id": unit["id"], "expected_source_version": unit["source_version"],
                   "event_id": "http-evt-1", "result": "helpful", "expected_revision": revision}
        status, answer = srv.request("/v1/feedback", payload)
        assert status == 200 and answer["effective"] is True
        status, again = srv.request("/v1/feedback", payload)
        assert status == 200 and again["deduplicated"] is True

        status, bad = srv.request("/v1/feedback", dict(payload, event_id="", expected_revision=revision + 1))
        assert status == 400 and bad["error"]["code"] == "invalid_event_id"
        status, stale = srv.request("/v1/feedback", dict(payload, event_id="http-evt-2",
                                                         expected_source_version="0" * 64))
        assert status == 409 and stale["error"]["code"] == "source_changed"
        status, conflict = srv.request("/v1/feedback", dict(payload, event_id="http-evt-3",
                                                            expected_revision=1))
        assert status == 409 and conflict["error"]["code"] == "revision_conflict"
    finally:
        srv.close()


def test_pin_http_and_detail(workspace):
    srv = Server(workspace["config"], free_port())
    try:
        view, unit = _old_unit(srv)
        status, pinned = srv.request("/v1/graph/edit", {
            "action": "pin", "payload": {"unit_id": unit["id"]},
            "expected_revision": view["revision"], "idempotency_key": "http-pin"})
        assert status == 200 and pinned["pinned"] is True
        status, detail = srv.request("/v1/fetch", {"unit_id": unit["id"],
                                                   "expected_source_version": unit["source_version"]})
        assert status == 200 and detail["activation"]["pinned"] is True
        status, view2 = srv.request("/v1/graph/view", {"mode": "overview"})
        assert view2["activation"]["units"][unit["id"]]["pinned"] is True
    finally:
        srv.close()


def test_mcp_rejects_bad_feedback_args(workspace):
    """MCP 工具声明了 schema 就要真的拒绝：缺参数、错类型、枚举外值。"""
    srv = Server(workspace["config"], free_port())
    try:
        view, unit = _old_unit(srv)
        token = srv.token()
        config = workspace["config"]
        script = str((workspace["tmp"] / "mcp_once.py"))
        (workspace["tmp"] / "mcp_once.py").write_text(
            "import json,sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from she_memory.tools import mcp_stdio\n"
            "messages = json.loads(sys.argv[1])\n"
            "for m in messages:\n"
            "    mcp_stdio.handle_message(None, m) if hasattr(mcp_stdio,'handle_message') else None\n"
            % (str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")),
            encoding="utf-8")
        # 直接用协议跑：一行一条 JSON-RPC
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "memory_feedback", "arguments": {
                 "unit_id": unit["id"], "expected_source_version": unit["source_version"],
                 "event_id": "mcp-evt-1", "result": "maybe", "expected_revision": 1}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_feedback", "arguments": {"unit_id": unit["id"]}}},
        ]
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "she_memory.tools.mcp_stdio",
             "--config", str(config), "--service", f"http://127.0.0.1:{srv.port}"],
            input="\n".join(json.dumps(r) for r in requests) + "\n",
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            env={**__import__("os").environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")})
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert lines, proc.stderr
        first, second = lines[0], lines[1]
        # 参数不合法走 JSON-RPC -32602（协议层拒绝），不是工具结果的 isError
        assert first["error"]["code"] == -32602
        assert "result must be one of" in first["error"]["message"]
        assert second["error"]["code"] == -32602
        assert "missing required" in second["error"]["message"]
    finally:
        srv.close()

def test_feedback_event_id_conflict_over_http(workspace):
    """真实 HTTP：同 event_id 不同载荷必须 409，不能返回“成功”或拼两条记录。"""
    srv = Server(workspace["config"], free_port())
    try:
        srv.request("/v1/refresh", {})
        status, view = srv.request("/v1/graph/view", {"mode": "overview"})
        units = [n for n in view["nodes"] if n["id"].startswith("unit:")][:2]
        assert len(units) == 2
        a, b = units

        def payload(unit, result="helpful", event="http-shared-event"):
            return {"unit_id": unit["id"], "expected_source_version": unit["source_version"],
                    "event_id": event, "result": result,
                    "expected_revision": srv.request("/v1/health")[1]["revision"]}

        status, first = srv.request("/v1/feedback", payload(a))
        assert status == 200 and first["effective"] is True
        revision_after = srv.request("/v1/health")[1]["revision"]

        status, replay = srv.request("/v1/feedback", payload(a))
        assert status == 200 and replay["deduplicated"] is True
        assert srv.request("/v1/health")[1]["revision"] == revision_after   # 重放不推进

        status, cross = srv.request("/v1/feedback", payload(b))
        assert status == 409 and cross["error"]["code"] == "idempotency_conflict", cross

        status, flipped = srv.request("/v1/feedback", payload(a, result="not_helpful"))
        assert status == 409 and flipped["error"]["code"] == "idempotency_conflict", flipped

        # 失败不改 metadata/revision，也不把 B 的状态混进来
        assert srv.request("/v1/health")[1]["revision"] == revision_after
        status, detail_b = srv.request("/v1/fetch", {"unit_id": b["id"],
                                                     "expected_source_version": b["source_version"]})
        assert status == 200 and detail_b["activation"]["last_helpful_at"] is None
    finally:
        srv.close()