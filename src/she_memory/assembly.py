"""导入断言的**统一装配**：所有发布入口（refresh / edit / 项目脚本）都走这里。

顺序固定，不能调换：
    1) 核验当前来源与登记（引文必须真的在那一行；登记必须还在 relations.yaml 里）
    2) 合并有效断言（证据用**当前** unit 的 source_version）
    3) 最后应用人工停用（disabled 里的边不发布）

这样三种情形都会自然成立：
    改成员正文   → 证据版本跟着变（不是旧版本）
    关系表删成员 → 断言不存在，边消失
    停用导入边   → 刷新后仍是停用状态
"""

import hashlib
import json
from pathlib import Path

import yaml

from .common import read_json  # noqa: F401  （保留给调用方复用）
from .graph import compile_graph, make_edge


def _relations_path(config):
    explicit = config.get("relations_file")
    if explicit:
        return Path(explicit)
    default = Path(config["memory_root"]) / "links" / "relations.yaml"
    return default if default.is_file() else None


def _line(path, lineno):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[lineno - 1] if 0 < lineno <= len(lines) else ""


def _unit_for(units, rel_path, lineno):
    for unit in units:
        if unit["path"] != rel_path:
            continue
        if unit["line"] <= lineno <= max(unit["end_line"], unit["line"]):
            return unit
    return None


def stable_import_edge_id(from_id, predicate, group_key):
    """身份与来源版本无关：来源改字不会换身份，停用/恢复才留得住。"""
    raw = f"{from_id}|{predicate}|{group_key}".encode("utf-8")
    return "edge:imported:" + hashlib.sha256(raw).hexdigest()[:16]


def verify_assertions(config, units):
    """按**当前**来源重新核对登记，返回节点、边与核对报告。"""
    path = _relations_path(config)
    if path is None or not path.is_file():
        return [], [], {"groups": 0, "sources": 0, "verified": 0, "dangling": []}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups = doc.get("relations") or []
    memory_root = Path(config["memory_root"])
    report = {"groups": 0, "sources": 0, "verified": 0, "dangling": []}
    nodes, edges = [], []
    for group in groups:
        key = str(group.get("key") or group.get("id"))
        report["groups"] += 1
        nodes.append({
            "id": "group:" + key,
            "label": group.get("label") or key,
            "kind": "relation_group",
            "origin": "imported_relations",
            "group_id": group.get("id"),
            "group_kind": group.get("kind"),
            "registration": {"path": str(path).replace("\\", "/"), "group": key},
        })
        for src in group.get("sources") or []:
            report["sources"] += 1
            rel_path = str(src.get("path", ""))
            lineno = int(src.get("line", 0) or 0)
            quote = str(src.get("quote", "") or "")
            text = _line(memory_root / rel_path, lineno)
            ok = bool(text) and quote != "" and quote in text
            unit = _unit_for(units, rel_path, lineno) if ok else None
            if not ok or unit is None:
                report["dangling"].append({
                    "group": key, "path": rel_path, "line": lineno,
                    "reason": "quote_not_on_line" if not ok else "no_unit_covers_line",
                })
                continue
            report["verified"] += 1
            edge = make_edge(unit["id"], "part_of", "group:" + key,
                             [{"unit_id": unit["id"], "source_version": unit["source_version"]}])
            edge["id"] = stable_import_edge_id(unit["id"], "part_of", key)
            edge["assertion_kind"] = "structural"
            edge["evidence_basis"] = "imported_relations_group"
            edge["registration"] = {"path": str(path).replace("\\", "/"), "group": key,
                                    "line": lineno, "quote": quote, "evidence": "explicit"}
            edges.append(edge)
    report_path = Path(config.get("reports_dir") or (Path(config["state_root"]).parent / "reports"))
    if report_path.is_dir():
        (report_path / "relations-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return nodes, edges, report


def assemble(config, units, entities, state, profile=None):
    """统一装配：编译 → 核验并合并导入 → 应用停用。返回 (nodes, edges, candidates, report)。"""
    nodes, edges, candidates = compile_graph(units, entities, state, profile)
    imported_nodes, imported_edges, report = verify_assertions(config, units)

    have_nodes = {n["id"] for n in nodes}
    for node in imported_nodes:
        if node["id"] not in have_nodes:
            nodes.append(node)
            have_nodes.add(node["id"])

    disabled = set(state.get("disabled") or [])
    have_edges = {e["id"] for e in edges}
    # 导入断言：停用的也保留（可恢复），只是标成 disabled
    for edge in imported_edges:
        if edge["from"] in have_nodes and edge["to"] in have_nodes and edge["id"] not in have_edges:
            edges.append(edge)
            have_edges.add(edge["id"])

    # lifecycle 是**派生**的，不写死在边对象里：
    # 停用只改状态、不删除边——界面才找得到恢复入口，检索仍然只看 active。
    for edge in edges:
        edge["lifecycle"] = "disabled" if edge["id"] in disabled else "active"

    return nodes, edges, candidates, report