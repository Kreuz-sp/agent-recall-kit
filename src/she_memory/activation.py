"""淡忘与再激活：策略、时间依据、权重与排序层。

边界（冻结单 §1–§3）：
  · 默认只有 kind=entry 的经历单元参与衰减；事实/知识/经验/待办/承诺等默认受保护；
  · 衰减只影响**同一相关度层级内**的顺序，不改状态（不会把旧记忆变成 no_match）；
  · 明确历史查询、明确 unit/locator、完整引文命中 → 绕过衰减（仍核验来源）；
  · 所有时间用 UTC 保存；时钟可注入；不使用文件 mtime。
"""

import math
import re
from datetime import datetime, timedelta, timezone

DEFAULT_POLICY = {
    "enabled": True,
    "half_life_days": 90.0,
    "floor": 0.35,
    "decay_kinds": ["entry"],
    "protect_kinds": ["fact", "knowledge", "experience", "thread_row", "section",
                      "source_metadata", "document", "relation_group"],
    # 层级：相关度 >= best*strong 视为「明确匹配」，>= best*related 视为「相关」，其余「弱相关」
    "tier_strong": 0.9,
    "tier_related": 0.6,
    "future_tolerance_days": 1.0,
}

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class ActivationError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def utcnow(clock=None):
    if clock is None:
        return datetime.now(timezone.utc)
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def policy_from(profile, config=None):
    """合并 profile.activation / config.activation，并校验参数。"""
    merged = dict(DEFAULT_POLICY)
    for source in ((profile.get("activation") if profile else None),
                   (config or {}).get("activation")):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            merged[key] = value
    if not isinstance(merged.get("enabled"), bool):
        raise ActivationError("invalid_activation_policy", "enabled must be boolean")
    try:
        merged["half_life_days"] = float(merged["half_life_days"])
        merged["floor"] = float(merged["floor"])
        merged["tier_strong"] = float(merged["tier_strong"])
        merged["tier_related"] = float(merged["tier_related"])
        merged["future_tolerance_days"] = float(merged["future_tolerance_days"])
    except (TypeError, ValueError) as exc:
        raise ActivationError("invalid_activation_policy", "numeric policy fields") from exc
    if merged["half_life_days"] <= 0:
        raise ActivationError("invalid_activation_policy", "half_life_days must be > 0")
    if not 0 <= merged["floor"] < 1:
        raise ActivationError("invalid_activation_policy", "floor must be in [0, 1)")
    if not 0 < merged["tier_related"] <= merged["tier_strong"] <= 1:
        raise ActivationError("invalid_activation_policy", "tier ratios must satisfy 0 < related <= strong <= 1")
    merged["decay_kinds"] = list(merged.get("decay_kinds") or [])
    merged["protect_kinds"] = list(merged.get("protect_kinds") or [])
    return merged


def weight_for(age_days, policy):
    half_life = policy["half_life_days"]
    floor = policy["floor"]
    age = max(0.0, float(age_days))
    weight = floor + (1 - floor) * math.pow(2, -age / half_life)
    return max(floor, min(1.0, weight))


def _date_from_text(text):
    if not text:
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def unit_time(unit):
    """返回 (时间, 依据, 诊断)。依据优先级：显式元数据 > 解析器认可的条目日期 > None。"""
    metadata = unit.get("metadata") or {}
    for key in ("memory_time", "observed_at", "occurred_at", "date"):
        if metadata.get(key):
            parsed = parse_iso(str(metadata[key]))
            if parsed is not None:
                return parsed, "explicit_metadata", None
            return None, "explicit_metadata", "invalid_metadata_time"
    derived = _date_from_text(unit.get("path") or "")
    if derived is not None:
        return derived, "parsed_date", None
    derived = _date_from_text(unit.get("label") or "")
    if derived is not None:
        return derived, "parsed_date", None
    return None, "first_seen", None


def activation_entry(state, unit_id):
    """取某个单元的激活记录；state 可以是快照本身，也可以是 ensure_state 的返回值。"""
    activation = state.get("activation") if isinstance(state, dict) and "activation" in state else state
    if not isinstance(activation, dict):
        return None
    return ((activation.get("units") or {})).get(unit_id)


def ensure_state(state):
    """旧快照缺字段时补空结构（不写盘，由 build/refresh 发布）。"""
    activation = state.get("activation")
    if not isinstance(activation, dict):
        activation = {}
    activation.setdefault("units", {})
    activation.setdefault("feedback", {})
    state["activation"] = activation
    return activation


def resolve(unit, entry, now, policy):
    """算一个单元的活跃摘要。entry 为空 → 视为首次入库时间未知（weight=1，basis=missing）。"""
    first_seen = parse_iso((entry or {}).get("first_seen_at"))
    last_helpful = parse_iso((entry or {}).get("last_helpful_at"))
    pinned = bool((entry or {}).get("pin"))
    kind = unit.get("kind")
    protected_kind = kind not in policy["decay_kinds"]
    summary = {
        "unit_id": unit["id"], "kind": kind, "pinned": pinned,
        "protected": bool(pinned or protected_kind),
        "age_days": None, "time_basis": None, "time_note": None,
        "last_helpful_at": (entry or {}).get("last_helpful_at"),
        "weight": 1.0,
    }
    if protected_kind or pinned:
        summary["time_basis"] = "protected"
        return summary
    moment, basis, note = unit_time(unit)
    if moment is not None and moment > now + timedelta(days=policy["future_tolerance_days"]):
        moment, basis, note = None, "first_seen", "future_time_ignored"
    if moment is None:
        moment = first_seen
        basis = "first_seen"
        if moment is None:
            summary.update(time_basis="missing", time_note=note, weight=1.0)
            return summary
    if last_helpful is not None and last_helpful > moment:
        moment = last_helpful
        basis = "last_helpful_at"
    age_days = max(0.0, (now - moment).total_seconds() / 86400.0)
    summary.update(age_days=round(age_days, 3), time_basis=basis, time_note=note,
                   weight=round(weight_for(age_days, policy), 6))
    return summary


def bypass_reason(norm, text, unit):
    """明确历史 / 明确单元或 locator / 完整引文 → 绕过衰减（仍由上层核验来源）。"""
    if norm.get("history"):
        return "history_query"
    lowered = (text or "").lower()
    if unit["id"].lower() in lowered:
        return "explicit_unit_id"
    citation = unit.get("path") or ""
    locator = unit.get("locator") or ""
    if citation and citation.lower() in lowered:
        return "explicit_source_path"
    if locator and len(locator) >= 4 and locator.lower() in lowered:
        return "explicit_locator"
    body = re.sub(r"\s+", "", unit.get("body") or "").lower()
    query = re.sub(r"\s+", "", lowered)
    if body and len(query) >= 8 and query in body:
        return "verbatim_citation"
    return None


def order_with_activation(ranked, units_by_id, state, now, policy, norm, text):
    """在同一相关度层级内按「相关度 × 衰减权重」排序；跨层级顺序不变。

    返回 (ordered, explain)。ranked 元素是 (score, unit, matched)。
    """
    explain = {"enabled": bool(policy["enabled"]), "policy": {
        "half_life_days": policy["half_life_days"], "floor": policy["floor"],
        "decay_kinds": policy["decay_kinds"], "tier_strong": policy["tier_strong"],
        "tier_related": policy["tier_related"]}, "items": [], "bypass": None, "decayed": 0}
    if not policy["enabled"] or not ranked:
        return list(ranked), explain
    best = ranked[0][0] or 1.0
    annotated = []
    for score, unit, matched in ranked:
        summary = resolve(unit, activation_entry(state, unit["id"]), now, policy)
        reason = bypass_reason(norm, text, unit)
        weight = 1.0 if reason else summary["weight"]
        ratio = score / best if best else 0.0
        tier = 0 if ratio >= policy["tier_strong"] else (1 if ratio >= policy["tier_related"] else 2)
        annotated.append({"score": score, "unit": unit, "matched": matched, "tier": tier,
                          "weight": weight, "effective": score * weight,
                          "summary": summary, "bypass": reason})
        if reason or summary["weight"] < 0.999:
            explain["decayed"] += 1
    if any(item["bypass"] for item in annotated):
        reasons = sorted({item["bypass"] for item in annotated if item["bypass"]})
        explain["bypass"] = ",".join(reasons)
    annotated.sort(key=lambda item: (item["tier"], -item["effective"]))
    explain["items"] = [{
        "unit_id": item["unit"]["id"], "kind": item["unit"]["kind"],
        "base_score": round(item["score"], 4), "tier": item["tier"],
        "weight": round(item["weight"], 4), "effective_score": round(item["effective"], 4),
        "time_basis": item["summary"]["time_basis"], "age_days": item["summary"]["age_days"],
        "pinned": item["summary"]["pinned"], "protected": item["summary"]["protected"],
        "bypass": item["bypass"],
    } for item in annotated[:20]]
    return [(item["score"], item["unit"], item["matched"]) for item in annotated], explain


def activation_map(units, state, now, policy):
    """给界面用：unit_id → 摘要（只算参与展示的字段，不落盘）。"""
    out = {}
    for unit in units:
        summary = resolve(unit, activation_entry(state, unit["id"]), now, policy)
        out[unit["id"]] = {
            "weight": summary["weight"], "time_basis": summary["time_basis"],
            "age_days": summary["age_days"], "pinned": summary["pinned"],
            "protected": summary["protected"], "last_helpful_at": summary["last_helpful_at"],
            "time_note": summary["time_note"],
        }
    return out
