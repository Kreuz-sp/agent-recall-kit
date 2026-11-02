"""Profile：把"领域知识"从代码里挪出来的数据层。

一个 profile 是一份 JSON，描述：
  · objects / targets —— 用户在问谁、想知道什么（正则 + 词面扩写）
  · hints            —— 选路用的提示词正则（目录/指代/事实/方法/历史/评测回显）
  · scoring          —— 打分与定型阈值、字段加权、类型加权
  · format           —— 记忆库的目录约定与 YAML 键别名
  · navigation       —— 待办/承诺文件叫什么
  · candidate        —— 关联候选的生成上限与噪声词
  · evidence         —— 证据充分性的标记正则与规则

代码只认这些字段，不认任何具体领域。找不到 profile 时用 STARTER（空对象表），
此时检索退化为"词面 + 图可达"，仍然可用，只是没有领域别名加权。
"""

import json
from pathlib import Path

from .common import MemoryErrorCode

STARTER = {
    "schema_version": 1,
    "name": "starter",
    "description": "空的最小白名单：没有对象别名与目标字段，检索只用词面 + 图可达性。",
    "objects": [],
    "targets": [],
    "device": {"main": r"主屏|主显示器", "secondary": r"副屏|副显示器"},
    "hints": {
        "navigation": r"待办|未完成|没做完|悬着的事|承诺|答应|约定|之后要办|之前那件事|之前在做什么",
        "threads": r"待办|悬着|没做完|进展",
        "promises": r"承诺|答应|约定",
        "focus_only": r"^(欸|诶)?\s*(之前的呢|那个呢|然后呢|接着呢)\s*[？?]?$",
        "fact": r"什么|哪|多少|到没到|现在|今天",
        "procedure": r"怎么|如何|怎么办|没生效|不生效|报错|失败|查",
        "history": r"当时|那次|上次|以前|之前怎么|为什么改|怎么测|评测记录",
        "echo": r"词面命中|无指针|Memory Cue｜库内线索",
    },
    "stop_words": ["中午", "早上", "下午", "昨天", "今天", "明天", "现在", "有点", "一点",
                   "吃", "什么", "怎么", "如何"],
    "scoring": {
        "min_score": 3.0,
        "graph_only_base": 3.5,
        "lead_ratio": 1.15,
        "tie_ratio": 0.98,
        "target_bonus": 2.2,
        "device_bonus": 1.8,
        "focus_bonus": 2.2,
        "kind_bonus": {"fact": 1.35, "experience": 1.15, "knowledge": 1.15},
        "locator_bonuses": [],
        "procedure_body_bonus": 0.18,
        "procedure_verifiable_pattern": r"回读|断言|验证|读回来|核对|回看",
        "procedure_verifiable_bonus": 1.4,
        "verbatim_min_chars": 6,
        "verbatim_penalty": 0.4,
        "echo_penalty": 0.6,
    },
    "format": {
        "root_files": [],
        "dirs": ["entries", "knowledge", "experience", "links"],
        "key_aliases": {},
        "max_files": 2500,
        "max_bytes": 1048576,
    },
    "navigation": {"threads_file": "open-threads.md", "promises_file": "promises.md"},
    "candidate": {
        "stop_words": ["交接", "上午", "总结", "凌晨", "演示"],
        "max_candidates": 200,
        "max_units_per_object": 60,
        "rule": "shared-object-and-title/v1",
    },
    "evidence": {
        "marks": {
            "has_method": r"怎么做|怎么验|方法|步骤|做法|回读|断言|验证",
            "has_state": r"到了|已到|收到|已发货|确认到货",
        },
        "rules": [
            {"code": "method_steps", "when_target": "method", "unless_history": True,
             "require": ["has_method"]},
        ],
    },
}


def _merge(base, override):
    """浅合并：override 里出现的键覆盖 base；字典逐键合并一层。"""
    out = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


class Profile:
    """只读访问器：属性风格 + 少量派生。"""

    def __init__(self, data, path=None):
        self.data = data
        self.path = str(path) if path else None

    def __getattr__(self, item):
        try:
            return self.data[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def get(self, key, default=None):
        return self.data.get(key, default)

    # 常用派生
    def objects(self):
        return [(o["id"], o["pattern"], o.get("words") or []) for o in self.data.get("objects") or []]

    def targets(self):
        return [(t["id"], t["pattern"], t.get("locator_pattern"), t.get("words") or [])
                for t in self.data.get("targets") or []]

    def target_locator(self, target_id):
        for tid, _pattern, locator, _words in self.targets():
            if tid == target_id:
                return locator
        return None

    def target_words(self, target_id):
        for tid, _pattern, _locator, words in self.targets():
            if tid == target_id:
                return words
        return []

    def scoring(self):
        return self.data["scoring"]

    def summary(self):
        return {
            "name": self.data.get("name"),
            "path": self.path,
            "objects": len(self.data.get("objects") or []),
            "targets": len(self.data.get("targets") or []),
            "evidence_rules": len((self.data.get("evidence") or {}).get("rules") or []),
        }


# 模块级默认：没有配置 profile 时用它（空对象表，不假装懂任何领域）
DEFAULT = Profile(_merge(STARTER, {}))


def load_profile(path=None):
    """读取 profile；path 为空返回默认 STARTER。文件坏了要报错，不静默降级。"""
    if not path:
        return DEFAULT
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise MemoryErrorCode("profile_unavailable", f"cannot read profile: {path}") from exc
    except ValueError as exc:
        raise MemoryErrorCode("profile_invalid", f"profile is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise MemoryErrorCode("profile_invalid", "profile must be a JSON object")
    merged = _merge(STARTER, data)
    return Profile(merged, path)
