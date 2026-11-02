"""建图、全量视图与查询状态。"""
import json

from conftest import brain, make_library, write


def test_build_then_overview_is_complete(workspace):
    b = brain(workspace["config"])
    built = b.refresh()
    assert built["revision"] == 1
    assert built["units"] > 5

    view = b.graph_view(mode="overview")
    assert view["partial"] is False
    assert view["returned_nodes"] == view["total_nodes"] == len(view["nodes"])
    assert view["returned_edges"] == view["total_edges"] == len(view["edges"])
    node_ids = [n["id"] for n in view["nodes"]]
    assert len(node_ids) == len(set(node_ids))
    edge_ids = [e["id"] for e in view["edges"]]
    assert len(edge_ids) == len(set(edge_ids))
    known = set(node_ids)
    assert all(e["from"] in known and e["to"] in known for e in view["edges"])


def test_legacy_call_keeps_bounded_behaviour(workspace):
    b = brain(workspace["config"])
    b.refresh()
    legacy = b.graph_view(limit=5)
    assert legacy["partial"] is True
    assert len(legacy["nodes"]) <= 5
    assert legacy["total_nodes"] > len(legacy["nodes"])


def test_query_states(workspace):
    b = brain(workspace["config"])
    b.refresh()

    ok = b.query("滴灌阀 型号")
    assert ok["status"] == "ok", ok
    assert ok["items"][0]["citation"]["path"] == "01_site.yaml"

    insufficient = b.query("滴灌阀 怎么校准")
    assert insufficient["status"] == "insufficient"
    assert "method_steps" in insufficient["missing"]

    missing = b.query("火星上的咖啡机")
    assert missing["status"] == "no_match"
    assert missing["items"] == []

    navigation = b.query("还有什么待办")
    assert navigation["status"] == "navigation"
    assert navigation["navigation"]["target"] == "open-threads.md"


def test_fetch_rejects_stale_version(workspace):
    b = brain(workspace["config"])
    b.refresh()
    view = b.graph_view(mode="overview")
    unit = next(n for n in view["nodes"] if n["id"].startswith("unit:"))
    got = b.fetch(unit["id"], unit["source_version"])
    assert got["text"]

    path = workspace["library"] / got["citation"]["path"]
    path.write_text(path.read_text(encoding="utf-8") + "\n改过了。\n", encoding="utf-8")
    try:
        b.fetch(unit["id"], unit["source_version"])
        raise AssertionError("stale source should be rejected")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "source_changed"
