# Profile：把"领域规则"写成数据

程序本身不认识任何领域。对象别名、目标字段、提示词、字段加权、证据规则都来自 **profile JSON**，
由配置里的 `profile_file` 指向。换领域 = 换 profile（或干脆用空的 starter），不需要改代码。

- 没有配置 `profile_file` → 使用 `STARTER`：对象表为空，检索只靠**词面 + 图可达性**，不假装懂你的领域。
- `agent-recall-kit init` 会把 `STARTER` 写到数据目录，作为起点。

## 字段

```json
{
  "name": "demo-greenhouse",
  "objects": [
    {"id": "valve", "pattern": "滴灌阀|阀门|阀组", "words": ["滴灌阀", "阀门", "阀组"]}
  ],
  "targets": [
    {"id": "model", "pattern": "型号|版本号", "locator_pattern": "(^|/)(model|firmware|version)"}
  ],
  "hints": {
    "navigation": "待办|未完成|承诺", "threads": "待办", "promises": "承诺",
    "fact": "型号|版本|多少", "procedure": "怎么|如何|步骤",
    "history": "当时|上次", "focus_only": "^呢[？?]?$", "echo": "检索命中"
  },
  "stop_words": ["今天", "现在"],
  "device": {"main": "主屏", "secondary": "副屏"},
  "scoring": {
    "min_score": 2.0, "graph_only_base": 2.5, "lead_ratio": 1.15, "tie_ratio": 0.98,
    "target_bonus": 2.2, "device_bonus": 1.5, "focus_bonus": 2.0,
    "kind_bonus": {"fact": 1.35, "knowledge": 1.15},
    "locator_bonuses": [{"pattern": "(^|/)note", "factor": 0.8}],
    "procedure_body_bonus": 0.2,
    "procedure_verifiable_pattern": "回读|确认|核对",
    "procedure_verifiable_bonus": 1.3,
    "verbatim_min_chars": 6, "verbatim_penalty": 0.4, "echo_penalty": 0.6
  },
  "format": {
    "root_files": ["profile.md", "open-threads.md"],
    "dirs": ["entries", "knowledge", "experience", "links"],
    "key_aliases": {"valve": "阀门 滴灌阀"},
    "max_files": 2500, "max_bytes": 1048576
  },
  "navigation": {"threads_file": "open-threads.md", "promises_file": "promises.md"},
  "candidate": {"stop_words": ["记录"], "max_candidates": 200, "max_units_per_object": 60,
                "rule": "shared-object-and-title/v1"},
  "evidence": {
    "marks": {"has_method": "步骤|方法|做法"},
    "rules": [{"code": "method_steps", "when_target": "method",
               "unless_history": true, "require": ["has_method"]}]
  }
}
```

### 各段在检索里的作用

| 段 | 作用 |
|---|---|
| `objects` | 用户提到这些词 → 判定"在说某个对象"，并以 `entity:<id>` 为种子走图；`words` 会被并入查询词 |
| `targets` | 判定"想知道什么"；`locator_pattern` 命中定位键时加权；没命中会返回 `insufficient` |
| `hints` | 选路提示：目录（待办/承诺）、事实、方法、历史、纯指代、评测回显 |
| `stop_words` | 太常见的词不构成线索（"今天""现在"） |
| `scoring` | 阈值与加权；`min_score` 太低会更容易判成命中，太高会更容易 `no_match` |
| `format` | 目录/根文件/YAML 键别名/大小上限 |
| `navigation` | 待办文件与承诺文件名 |
| `candidate` | 候选生成上限、每条对象的条目上限、标题噪声词、规则名 |
| `evidence.marks` | 用正则给单元正文打标（如"有步骤""有验收"） |
| `evidence.rules` | 声明式规则：满足 `when_*` / `if_marks` 时，缺少 `require` 里的标记就返回缺证据代码 |

规则求值顺序（`needs_more_evidence`）：

1. `when_target` 不匹配 → 跳过；`when_object` 不在本次对象里 → 跳过；`unless_history` 且问题是历史问法 → 跳过；
2. `if_marks` 里任一标记没命中 → 跳过（规则只在特定正文形态下生效）；
3. `require` 里有标记没命中 → 把 `code` 加进 `missing`（查询状态会变 `insufficient`）。

## 写一份自己的 profile

1. 从 `examples/config/profile-demo.json` 复制一份；
2. 只写你库里**真的存在**的对象与字段（宁少勿多，别写"可能有人这么问"的词）；
3. `pattern` 是 Python 正则、大小写不敏感；`words` 是被并入查询词的词面扩写；
4. 先跑 `agent-recall-kit --config ... query "..."` 看状态与命中，再决定阈值；
5. 不写代码：改 profile → 重启服务（或重新 `build` 后刷新页面）即可。

> 约定：profile 是**规则**，不是记忆；不要把它当作资料库来用。
