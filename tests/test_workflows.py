"""候选裁定、停用/恢复、布局隔离、单实例、备份。"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from conftest import Server, brain, free_port


def _overview(b):
    return b.graph_view(mode="overview")


def test_candidate_accept_persists(workspace):
    b = brain(workspace["config"])
    b.refresh()
    view = _overview(b)
    assert view["candidates"], "demo fixture should generate at least one candidate"
    cand = view["candidates"][0]
    detail = b.candidate_detail(cand["id"])
    assert detail["can_accept"] is True

    result = b.edit("accept", {"candidate_id": cand["id"]}, view["revision"], "test-accept")
    assert result["candidate_id"] == cand["id"]

    again = brain(workspace["config"])       # 重新开一个实例，模拟重启
    after = _overview(again)
    state = next(c for c in after["candidates"] if c["id"] == cand["id"])["state"]
    assert state == "accepted"


def test_disable_removes_edge_from_query_path(workspace):
    b = brain(workspace["config"])
    b.refresh()
    baseline = b.query("滴灌阀 更换")
    baseline_edges = {e for item in baseline["items"] for e in (item["path"]["edges"] or [])}
    assert baseline_edges, "fixture query must traverse at least one edge"

    view = _overview(b)
    edge_id = next(iter(baseline_edges))
    edge = next(e for e in view["edges"] if e["id"] == edge_id)
    b.edit("disable", {"edge_id": edge_id}, view["revision"], "test-disable")
    disabled_view = _overview(b)
    assert next(e for e in disabled_view["edges"] if e["id"] == edge_id)["lifecycle"] == "disabled"
    after = b.query("滴灌阀 更换")
    after_edges = {e for item in after["items"] for e in (item["path"]["edges"] or [])}
    assert edge_id not in after_edges

    b.edit("restore", {"edge_id": edge_id}, disabled_view["revision"], "test-restore")
    restored = b.query("滴灌阀 更换")
    restored_edges = {e for item in restored["items"] for e in (item["path"]["edges"] or [])}
    assert edge_id in restored_edges
    assert edge["predicate"]


def test_layout_is_scoped(server):
    status, layout = server.request("/v1/views/default")
    assert status == 200 and layout["revision"] == 0

    status, first = server.request("/v1/views/default", {
        "expected_view_revision": 0,
        "positions": {"overview|unit:a": {"x": 1, "y": 2}},
    })
    assert status == 200
    status, second = server.request("/v1/views/default", {
        "expected_view_revision": first["revision"],
        "positions": {"overview|unit:a": {"x": 1, "y": 2}, "local:*|unit:a": {"x": 9, "y": 9}},
    })
    assert status == 200
    status, fetched = server.request("/v1/views/default")
    assert fetched["positions"]["overview|unit:a"] == {"x": 1, "y": 2}
    assert fetched["positions"]["local:*|unit:a"] == {"x": 9, "y": 9}
    # 旧 revision 写入被拒
    status, conflict = server.request("/v1/views/default", {
        "expected_view_revision": 0,
        "positions": {"overview|unit:a": {"x": 5, "y": 5}},
    })
    assert status == 409 and conflict["error"]["code"] == "revision_conflict"


def test_second_instance_is_refused(workspace):
    port = free_port()
    first = Server(workspace["config"], port)
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-m", "she_memory",
             "--config", str(workspace["config"]), "serve", "--port", str(port)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        assert proc.returncode != 0
        assert "bind failed" in (proc.stdout or "") + (proc.stderr or "")
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    finally:
        first.close()


def test_backup_verify_and_isolated_restore(workspace, tmp_path):
    from she_memory.common import load_config
    from she_memory.tools import backup

    b = brain(workspace["config"])
    view = b.refresh()
    assert view["revision"] == 1

    cfg = load_config(str(workspace["config"]))
    out = tmp_path / "backups"
    assert backup.main(["create", "--config", str(workspace["config"]), "--out", str(out)]) == 0
    archive = next(out.glob("*.zip"))
    assert backup.main(["verify", "--archive", str(archive)]) == 0

    restored = tmp_path / "restored"
    assert backup.main(["restore", "--archive", str(archive), "--to", str(restored)]) == 0
    assert (restored / "state" / "CURRENT.json").is_file()
    assert (restored / "manifest.json").is_file()
    # 恢复目录非空时拒绝覆盖
    assert backup.main(["restore", "--archive", str(archive), "--to", str(restored)]) == 1
