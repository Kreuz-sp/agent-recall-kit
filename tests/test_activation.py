"""淡忘与再激活：策略、时间、排序、反馈、持久化。"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import brain, make_library, write
from she_memory.activation import (ActivationError, ensure_state, order_with_activation,
                                   policy_from, resolve, unit_time, weight_for)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
CLOCK = lambda: NOW  # noqa: E731


def add_entry(root, name, title, body):
    write(root / "entries" / name, f"# {title}\n\n{body}\n")


@pytest.fixture
def decay_ws(workspace):
    """同一主题的三条经历：旧而有答案、新而空、以及一条无关的新记录。"""
    lib = workspace["library"]
    add_entry(lib, "2025-09-01-valve-answer.md", "2025-09-01 低温卡涩的结论",
              "低温卡涩先调限流螺丝，不要换件。回读日志确认。")
    add_entry(lib, "2026-05-20-valve-meeting.md", "2026-05-20 例会随手记",
              "会上顺口提到滴灌阀，没有结论。")
    add_entry(lib, "2026-05-28-other-topic.md", "2026-05-28 别的记录",
              "与阀门无关，只写了一次例会。")
    b = brain(workspace["config"], clock=CLOCK)
    b.refresh()
    return b, lib


def unit_of(b, path_fragment):
    view = b.graph_view(mode="overview")
    return next(n for n in view["nodes"]
                if n["id"].startswith("unit:") and path_fragment in (n.get("label") or ""))


# ── 1. 权重公式与参数校验 ─────────────────────────────────────
def test_weight_curve_and_validation():
    policy = policy_from(None, {"activation": {"half_life_days": 90, "floor": 0.35}})
    assert round(weight_for(0, policy), 4) == 1.0
    assert round(weight_for(90, policy), 4) == 0.675
    assert round(weight_for(180, policy), 4) == 0.5125
    weights = [weight_for(day, policy) for day in range(0, 400, 10)]
    assert all(a >= b for a, b in zip(weights, weights[1:]))       # 单调下降
    assert all(w >= policy["floor"] for w in weights)              # 不低于 floor
    assert weight_for(-30, policy) == weights[0]                   # 未来/负数取 0 年龄

    with pytest.raises(ActivationError):
        policy_from(None, {"activation": {"half_life_days": 0}})
    with pytest.raises(ActivationError):
        policy_from(None, {"activation": {"floor": 1.2}})
    with pytest.raises(ActivationError):
        policy_from(None, {"activation": {"enabled": "yes"}})


def test_future_and_missing_time_are_diagnosed():
    policy = policy_from(None, {"activation": {}})
    future = {"id": "unit:x", "kind": "entry", "path": "entries/2027-01-01-future.md",
              "label": "future", "body": "x", "metadata": {}}
    entry = {"first_seen_at": "2026-05-01T00:00:00Z"}
    summary = resolve(future, entry, NOW, policy)
    assert summary["time_note"] == "future_time_ignored"
    assert summary["time_basis"] == "first_seen"

    invalid = dict(future, path="entries/broken.md", metadata={"memory_time": "not-a-date"})
    assert resolve(invalid, entry, NOW, policy)["time_note"] == "invalid_metadata_time"

    missing = dict(future, path="entries/no-date.md")
    summary = resolve(missing, {}, NOW, policy)
    assert summary["time_basis"] == "missing" and summary["weight"] == 1.0


# ── 2. 排序：层级内衰减，状态不变 ────────────────────────────
def test_old_relevant_still_wins_against_irrelevant_new(decay_ws):
    b, _lib = decay_ws
    result = b.query("低温卡涩 结论")
    assert result["status"] == "ok"
    paths = [item["citation"]["path"] for item in result["items"]]
    assert "entries/2025-09-01-valve-answer.md" in paths
    # 无关的新记录不能因为新而排到明确答案前面
    assert paths.index("entries/2025-09-01-valve-answer.md") < (
        paths.index("entries/2026-05-28-other-topic.md")
        if "entries/2026-05-28-other-topic.md" in paths else 99)
    explain = result["activation"]
    assert explain["enabled"] is True
    assert explain["items"], "explain 要给出层级与权重"
    assert all("tier" in item and "weight" in item for item in explain["items"])


def test_age_does_not_create_no_match_or_resolve_ambiguity():
    from conftest import make_config
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        lib = make_library(tmp_path / "library")
        add_entry(lib, "2025-08-01-a.md", "2025-08-01 阀门说明 A", "说明 A：先调螺丝")
        add_entry(lib, "2025-08-02-b.md", "2025-08-02 阀门说明 B", "说明 B：先调螺丝")
        config = make_config(tmp_path, lib)
        b = brain(config, clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
        b.refresh()
        result = b.query("阀门说明 调螺丝")
        assert result["status"] in ("ok", "ambiguous"), result["status"]
        assert result["items"], "衰减不能把旧记忆变成 no_match"
        if result["status"] == "ambiguous":
            assert result["conflicts"]


# ── 3. 保护与 pin ────────────────────────────────────────────
def test_protected_kinds_and_pin(decay_ws):
    b, _lib = decay_ws
    view = b.graph_view(mode="overview")
    fact = next(n for n in view["nodes"] if n["kind"] == "fact")
    policy = b.activation_policy
    summary = resolve(fact, ensure_state(b.store.read())["units"].get(fact["id"]), NOW, policy)
    assert summary["protected"] is True and summary["weight"] == 1.0

    old = unit_of(b, "低温卡涩的结论")
    before = resolve(old, ensure_state(b.store.read())["units"].get(old["id"]), NOW, policy)
    assert before["weight"] < 1.0

    revision = b.store.read()["revision"]
    b.edit("pin", {"unit_id": old["id"]}, revision, "pin-1")
    after = resolve(old, ensure_state(b.store.read())["units"].get(old["id"]), NOW, policy)
    assert after["pinned"] and after["weight"] == 1.0
    with pytest.raises(Exception) as exc:
        b.edit("unpin", {"unit_id": old["id"]}, revision, "pin-stale")   # 旧 revision
    assert getattr(exc.value, "code", "") == "revision_conflict"

    current = b.store.read()["revision"]
    b.edit("unpin", {"unit_id": old["id"]}, current, "unpin-1")
    after = resolve(old, ensure_state(b.store.read())["units"].get(old["id"]), NOW, policy)
    assert not after["pinned"] and after["weight"] < 1.0


# ── 4. 历史/精确定位绕过，且仍核验版本 ────────────────────────
def test_history_and_locator_bypass_but_still_verify(decay_ws):
    b, lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    policy = b.activation_policy
    entry = ensure_state(b.store.read())["units"].get(old["id"])
    assert resolve(old, entry, NOW, policy)["weight"] < 1.0

    history = b.query("上次低温卡涩是怎么处理的")
    assert history["activation"]["bypass"] == "history_query"
    items = {item["memory_id"]: item for item in history["items"]}
    if old["id"] in items:
        assert items[old["id"]]["activation"]["weight"] == 1.0

    exact = b.query(old["id"])
    assert exact["activation"]["bypass"] and "explicit_unit_id" in exact["activation"]["bypass"]

    path = lib / "entries/2025-09-01-valve-answer.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n改过了。\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        b.fetch(old["id"], old["source_version"])
    assert getattr(exc.value, "code", "") == "source_changed"


# ── 5. 读写边界：query/fetch 不写，helpful 才强化 ──────────────
def test_queries_do_not_write_and_helpful_reactivates(decay_ws):
    b, _lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    revision = b.store.read()["revision"]
    weights_before = [b.query("低温卡涩 结论")["items"][0]["activation"]["weight"] for _ in range(5)]
    for _ in range(15):
        b.query("低温卡涩 结论")
        b.fetch(old["id"], old["source_version"])
    assert b.store.read()["revision"] == revision
    assert b.query("低温卡涩 结论")["items"][0]["activation"]["weight"] == weights_before[0]

    policy = b.activation_policy
    stale_entry = ensure_state(b.store.read())["units"].get(old["id"])
    assert resolve(old, stale_entry, NOW, policy)["weight"] < 1.0

    answer = b.feedback(old["id"], old["source_version"], "evt-helpful-1", "helpful", revision)
    assert answer["effective"] is True
    fresh = resolve(old, ensure_state(b.store.read())["units"].get(old["id"]), NOW, policy)
    assert fresh["weight"] == 1.0

    again = brain(b.config['config_path'], clock=CLOCK)          # 重启（新 Brain 实例，同一数据目录）
    kept = resolve(old, ensure_state(again.store.read())["units"].get(old["id"]), NOW, policy)
    assert kept["weight"] == 1.0

    revision2 = again.store.read()["revision"]
    no = again.feedback(old["id"], old["source_version"], "evt-not-helpful-1", "not_helpful", revision2)
    assert no["effective"] is False
    after_no = resolve(old, ensure_state(again.store.read())["units"].get(old["id"]), NOW, policy)
    assert after_no["weight"] == 1.0                 # 不惩罚，也不跌回衰减


# ── 6. 反馈校验：幂等、同日、旧版本、缺参数 ───────────────────
def test_feedback_validation(decay_ws):
    b, lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    revision = b.store.read()["revision"]
    first = b.feedback(old["id"], old["source_version"], "evt-dup", "helpful", revision)
    assert first["effective"] is True
    revision_after = b.store.read()["revision"]

    replay = b.feedback(old["id"], old["source_version"], "evt-dup", "helpful", revision_after)
    assert replay["deduplicated"] is True
    assert b.store.read()["revision"] == revision_after        # 重放不推进

    same_day = b.feedback(old["id"], old["source_version"], "evt-same-day", "helpful", revision_after)
    assert same_day["effective"] is False                       # 同一 UTC 日只强化一次

    with pytest.raises(Exception) as exc:
        b.feedback(old["id"], "0" * 64, "evt-bad-version", "helpful", b.store.read()["revision"])
    assert getattr(exc.value, "code", "") == "source_changed"

    with pytest.raises(Exception) as exc:
        b.feedback(old["id"], old["source_version"], "", "helpful", b.store.read()["revision"])
    assert getattr(exc.value, "code", "") == "invalid_event_id"

    with pytest.raises(Exception) as exc:
        b.feedback(old["id"], old["source_version"], "evt-bad-result", "maybe", b.store.read()["revision"])
    assert getattr(exc.value, "code", "") == "invalid_feedback_result"

    before = b.store.read()["revision"]
    path = lib / "entries/2025-09-01-valve-answer.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n改过。\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        b.feedback(old["id"], old["source_version"], "evt-stale-source", "helpful", before)
    assert getattr(exc.value, "code", "") == "source_changed"
    assert b.store.read()["revision"] == before                 # 失败反馈不改元数据


# ── 7. refresh / 版本变化 / 旧快照 ───────────────────────────
def test_refresh_preserves_first_seen_and_clears_old_feedback(decay_ws):
    b, lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    revision = b.store.read()["revision"]
    b.edit("pin", {"unit_id": old["id"]}, revision, "pin-before-refresh")
    b.feedback(old["id"], old["source_version"], "evt-before-refresh", "helpful",
               b.store.read()["revision"])
    entry_before = dict(ensure_state(b.store.read())["units"][old["id"]])
    assert entry_before["last_helpful_at"] and entry_before["pin"] is True

    b.refresh()
    entry_after = ensure_state(b.store.read())["units"][old["id"]]
    assert entry_after["first_seen_at"] == entry_before["first_seen_at"]   # 不重置
    assert entry_after["pin"] is True

    path = lib / "entries/2025-09-01-valve-answer.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n版本二。\n", encoding="utf-8")
    b.refresh()
    entry_v2 = ensure_state(b.store.read())["units"][old["id"]]
    assert entry_v2["first_seen_at"] == entry_before["first_seen_at"]      # 换版本不重置首见
    assert entry_v2["last_helpful_at"] is None                             # 旧反馈不再作用于新版本
    assert entry_v2["pin"] is True
    assert entry_v2["source_version"] != entry_before["source_version"]

    # 旧快照缺 activation 字段也能读
    data = b.store.read()
    data.pop("activation", None)
    ensure_state(data)
    assert data["activation"]["units"] == {}


# ── 8. 备份 → 隔离恢复：激活与去重一致、权重一致 ──────────────
def test_backup_restore_keeps_activation(decay_ws, tmp_path):
    from she_memory.common import load_config
    from she_memory.tools import backup

    b, _lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    b.feedback(old["id"], old["source_version"], "evt-backup", "helpful",
               b.store.read()["revision"])
    policy = b.activation_policy
    weight_before = resolve(old, ensure_state(b.store.read())["units"].get(old["id"]),
                            NOW, policy)["weight"]

    out = tmp_path / "backups"
    config_path = b.config["config_path"]
    assert backup.main(["create", "--config", config_path, "--out", str(out)]) == 0
    archive = next(out.glob("*.zip"))
    assert backup.main(["verify", "--archive", str(archive)]) == 0
    target = tmp_path / "restored"
    assert backup.main(["restore", "--archive", str(archive), "--to", str(target)]) == 0

    restored_state = json.loads((target / "state" / "CURRENT.json").read_text(encoding="utf-8"))
    snapshot = json.loads((target / "state" / "snapshots" / f"{restored_state['revision']}.json")
                          .read_text(encoding="utf-8"))
    activation = ensure_state(snapshot)
    assert "evt-backup" in activation["feedback"]                     # 去重记录也在
    assert activation["units"][old["id"]]["last_helpful_at"]
    cfg = load_config(config_path)
    restored_weight = resolve(old, activation["units"].get(old["id"]), NOW, policy)["weight"]
    assert restored_weight == weight_before                           # 固定时钟下不漂移
    assert cfg["state_root"]


# ── 10. 关闭机制时行为回到原样 ───────────────────────────────
def test_disabled_matches_legacy_ordering(decay_ws):
    b, _lib = decay_ws
    enabled = b.query("低温卡涩 结论")
    b.activation_policy = dict(b.activation_policy, enabled=False)
    disabled = b.query("低温卡涩 结论")
    assert disabled["activation"]["enabled"] is False
    assert [i["memory_id"] for i in disabled["items"]] == [i["memory_id"] for i in enabled["items"]]
    assert disabled["status"] == enabled["status"]

# ── 补充：pin / feedback 不写原件 ─────────────────────────────
def test_writes_never_touch_source_files(decay_ws):
    import hashlib
    b, lib = decay_ws
    old = unit_of(b, "低温卡涩的结论")
    path = next(lib.rglob("*2025-09-01-valve-answer.md"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    revision = b.store.read()["revision"]
    b.edit("pin", {"unit_id": old["id"]}, revision, "pin-no-write")
    b.feedback(old["id"], old["source_version"], "evt-no-write", "helpful",
               b.store.read()["revision"])
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after, "pin/feedback 不许改原件"
    assert path.read_text(encoding="utf-8").startswith("# 2025-09-01")