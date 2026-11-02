"""查询流水线：归一 → 选路 → 打分 → 定型。

口径：
  · 状态按**本次查询**目标是否缺证据决定，不被全库覆盖错误左右；
  · 不按文件名判错——只看本次命中的单元正文是否含需要的证据；
  · 保留五种状态：ok / ambiguous / insufficient / no_match / navigation，explain 另附。

领域知识（对象别名、目标字段、提示词、字段加权、证据规则）全部来自 profile，
代码里不写死任何具体领域。profile 缺失时退化为"词面 + 图可达"。
"""

import json
import math
import re
from pathlib import Path

from .graph import tokens
from .profile import DEFAULT as DEFAULT_PROFILE


def _has(pattern, text):
    return bool(re.search(pattern, text, re.I))


def normalize(text, profile=DEFAULT_PROFILE):
    words_extra = []
    objects = []
    for oid, pattern, words in profile.objects():
        if _has(pattern, text):
            objects.append(oid)
            words_extra.extend(words)
    targets = [t[0] for t in profile.targets() if _has(t[1], text)]
    device_cfg = profile.get("device") or {}
    device = None
    if device_cfg.get("secondary") and _has(device_cfg["secondary"], text):
        device = "secondary"
    elif device_cfg.get("main") and _has(device_cfg["main"], text):
        device = "main"
    hints = profile.get("hints") or {}
    stop_words = set(profile.get("stop_words") or [])
    return {
        "objects": objects,
        "target": targets[0] if targets else None,
        "targets": targets,
        "device": device,
        "history": _has(hints.get("history", r"$^"), text),
        "procedure": _has(hints.get("procedure", r"$^"), text),
        "fact": _has(hints.get("fact", r"$^"), text),
        "navigation": _has(hints.get("navigation", r"$^"), text),
        "focus_only": bool(re.search(hints.get("focus_only", r"$^"), text.strip())),
        "words": set(w for w in tokens(text + " " + " ".join(words_extra))
                      if w not in stop_words and not any(s in w for s in stop_words)),
        "verbatim": re.sub(r"\s+", "", text).lower(),
    }


def route(norm, has_focus=False):
    """唯一的分支处：先目录 → 再对象 → 再焦点 → 再指代 → 否则词面搜索。"""
    if norm["navigation"]:
        return "navigation"
    if norm["objects"]:
        return "object"
    if has_focus:
        return "focus"
    if norm["focus_only"]:
        return "ambiguous"
    return "search"


def score_unit(unit, norm, df=None, total=1, profile=DEFAULT_PROFILE):
    match = norm["words"] & tokens(unit["search_text"])
    if not match:
        return 0.0, []
    scoring = profile.scoring()
    locator = unit.get("locator") or ""

    def rarity(w):
        d = (df or {}).get(w, 1)
        return min(1 + math.log(max(total, 2) / max(d, 1)), 1.6)

    base = sum(min(len(w), 4) * rarity(w) for w in match) / math.sqrt(1 + len(unit["search_text"]) / 600)
    bonus = 1.0
    # 目标字段属于同一对象才加权（locator_pattern 由 profile 给）
    if norm["target"]:
        pattern = profile.target_locator(norm["target"])
        if pattern and re.search(pattern, locator):
            bonus *= scoring.get("target_bonus", 2.2)
    # 领域字段加权（例如"实测"优先于"计划"），由 profile 声明
    for rule in scoring.get("locator_bonuses") or []:
        if rule.get("when_target") and norm.get("target") != rule["when_target"]:
            continue
        if re.search(rule["pattern"], locator):
            bonus *= rule.get("factor", 1.0)
    if norm.get("device") and locator and ("/" + norm["device"] + "/") in locator:
        bonus *= scoring.get("device_bonus", 1.8)
    if norm.get("focus") and any(w in (unit["search_text"] or "") for w in norm["focus"]):
        bonus *= scoring.get("focus_bonus", 2.2)
    kind_bonus = scoring.get("kind_bonus") or {}
    bonus *= float(kind_bonus.get(unit["kind"], 1.0))
    # 方法类：正文真的覆盖问句关键词、并且可验证，才更像"对症"的那一节
    if norm["procedure"]:
        body_hits = len(norm["words"] & tokens(unit["body"] or ""))
        bonus *= 1 + scoring.get("procedure_body_bonus", 0.18) * body_hits
        if scoring.get("procedure_verifiable_pattern") and \
                re.search(scoring["procedure_verifiable_pattern"], unit["body"] or ""):
            bonus *= scoring.get("procedure_verifiable_bonus", 1.4)
    # 评测回显：正文里逐字重复了问句/描述检索行为本身，降权（不排除）
    text_norm = re.sub(r"\s+", "", unit["body"] or "").lower()
    if norm["verbatim"] and len(norm["verbatim"]) >= scoring.get("verbatim_min_chars", 6) \
            and norm["verbatim"] in text_norm:
        bonus *= scoring.get("verbatim_penalty", 0.4)
    echo = (profile.get("hints") or {}).get("echo")
    if echo and re.search(echo, unit["body"] or ""):
        bonus *= scoring.get("echo_penalty", 0.6)
    return base * bonus, match


def rank(units, norm, profile=DEFAULT_PROFILE):
    usable = [u for u in units if u["kind"] != "source_metadata"]
    df = {}
    for u in usable:
        for w in (norm["words"] & tokens(u["search_text"])):
            df[w] = df.get(w, 0) + 1
    total = len(usable)
    ranked = []
    for unit in usable:
        value, match = score_unit(unit, norm, df, total, profile)
        if value > 0:
            ranked.append((value, unit, match))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def classify(ranked, norm, df=None, total=1, graph_ok=None, allow_common=False,
             profile=DEFAULT_PROFILE):
    """定型只看本次证据：够就 ok，接近就 ambiguous，缺目标字段就 insufficient，没有就 no_match。"""
    scoring = profile.scoring()
    if not ranked:
        return "no_match", [], []
    best, unit, match = ranked[0]
    # 证据要"具体"：至少有一个足够稀有的词命中，否则视为范围外
    specific = (allow_common or unit["id"] in (graph_ok or set())
                or any((df or {}).get(w, total) <= max(5, int(total * 0.02)) for w in match))
    if not specific:
        return "no_match", [], []
    if best < scoring.get("min_score", 3.0):
        return "no_match", [], []
    rivals = [r for r in ranked[1:] if r[0] >= best * scoring.get("tie_ratio", 0.98)][:4]
    if rivals and not (best >= rivals[0][0] * scoring.get("lead_ratio", 1.15)):
        return "ambiguous", [unit], [r[1] for r in rivals]
    if norm["target"]:
        pattern = profile.target_locator(norm["target"])
        if pattern and not re.search(pattern, unit.get("locator") or ""):
            return "insufficient", [unit], []
    return "ok", [unit], []


# ── 图上的有限路径 ──────────────────────────────────────────────
def build_adjacency(edges):
    adj = {}
    for edge in edges:
        if edge.get("lifecycle") != "active":
            continue
        adj.setdefault(edge["from"], []).append((edge["to"], edge["id"]))
        adj.setdefault(edge["to"], []).append((edge["from"], edge["id"]))
    return adj


def graph_paths(adjacency, seeds, max_hops=2, fanout=400):
    """从种子节点做有界 BFS，返回 {node_id: (node_ids, edge_ids)} 的最短路径。"""
    seen = {}
    frontier = []
    for seed in seeds:
        if seed in adjacency or any(seed == s for s in seeds):
            seen[seed] = ([seed], [])
            frontier.append(seed)
    hops = 0
    expanded = 0
    while frontier and hops < max_hops and expanded < fanout:
        nxt = []
        for node in frontier:
            for other, edge_id in adjacency.get(node, []):
                if other in seen:
                    continue
                nodes, edges = seen[node]
                seen[other] = (nodes + [other], edges + [edge_id])
                nxt.append(other)
                expanded += 1
                if expanded >= fanout:
                    break
            if expanded >= fanout:
                break
        frontier = nxt
        hops += 1
    return seen, expanded


def cover_evidence(unit, profile=DEFAULT_PROFILE):
    """按 profile 里的标记正则，算这个单元正文"命中"了哪些证据标记。"""
    body = unit.get("body") or ""
    marks = {}
    for name, pattern in ((profile.get("evidence") or {}).get("marks") or {}).items():
        marks[name] = bool(re.search(pattern, body))
    return marks


def needs_more_evidence(norm, unit, profile=DEFAULT_PROFILE):
    """按 profile 的声明式规则判断缺什么证据；空列表表示够。"""
    rules = (profile.get("evidence") or {}).get("rules") or []
    if not rules:
        return []
    marks = cover_evidence(unit, profile)
    missing = []
    for rule in rules:
        if rule.get("when_target") and norm.get("target") != rule["when_target"]:
            continue
        if rule.get("when_object") and rule["when_object"] not in (norm.get("objects") or []):
            continue
        if rule.get("unless_history") and norm.get("history"):
            continue
        if rule.get("if_marks") and not all(marks.get(m) for m in rule["if_marks"]):
            continue
        if not all(marks.get(m) for m in rule.get("require") or []):
            missing.append(rule.get("code") or "evidence_missing")
    return missing


# ── 小节级登记（用户登记"这类问题该看哪些小节"）────────────────
def load_roles(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], {}
    entities = {e["id"]: e for e in doc.get("entities") or []}
    return doc.get("rules") or [], entities


def registered_units(units, rules, wanted_ids):
    """返回命中登记小节、且该小节声明属于 wanted_ids 的单元。"""
    if not rules or not wanted_ids:
        return set()
    hits = set()
    for unit in units:
        heading = " / ".join(unit.get("heading_path") or [])
        for rule in rules:
            if rule.get("path") != unit["path"]:
                continue
            want = rule.get("heading_contains")
            if want and want not in heading and want not in (unit.get("body") or "")[:80]:
                continue
            if set(rule.get("anchors") or []) & wanted_ids:
                hits.add(unit["id"])
    return hits
