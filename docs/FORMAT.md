# 记忆库格式（最小约定）

一个"记忆库"就是一个目录。程序只读它；图状态写在**另一个**目录（配置里的 `state_root`/`view_root`）。

## 目录

```
<library>/
  profile.md            # 可选：你的背景（资料，不是程序配置）
  open-threads.md       # 待办表（表头 + 行）
  promises.md           # 承诺表
  01_site.yaml          # 事实文件：文件名以两位数字 + "_" 开头才会被当作事实库
  02_inventory.yaml
  entries/              # 事件/日志类 Markdown
  knowledge/            # 知识类 Markdown
  experience/           # 经验类 Markdown
  links/relations.yaml  # 显式关系登记（可选）
  entities.json         # 对象表（由配置里的 entities_file 指向）
```

目录名与根文件名都可以在 profile 的 `format` 里改；默认值见 `src/she_memory/profile.py` 里的 `STARTER`。

## 单元（unit）

一个文件会被切成若干**单元**，每个单元是最小证据单位：

| 字段 | 含义 |
|---|---|
| `id` | `unit:<uuid5>`，由「路径 + 定位键」稳定生成 |
| `path` | 相对记忆库根的路径 |
| `locator` | 定位键：Markdown 是 `md:<标题路径>`，YAML 是 `yaml_path:<键路径>` 或 `yaml_id:<id>` |
| `line` / `end_line` | 1 起始的行号范围 |
| `source_version` | 文件内容的 sha256（取证据时按它校验） |
| `kind` | `entry` / `knowledge` / `experience` / `fact` / `section` / `source_metadata` |
| `body` | 该单元的正文（界面里点开看到的就是它） |

**Markdown**：`#` 标题切分；标题路径组成 `locator`；代码块里的 `#` 不算标题。
**YAML**：必须是"标量键路径"或带 `id`+`content` 的块；**不接受 YAML 锚点/别名**（会直接报 `yaml_alias_not_supported`）。
**大小限制**：单文件默认 1 MiB，文件数默认 2500，可在 profile `format` 里改。

## 节点与关系

- 每个单元 → 一个节点；每个文件 → 一个 `document` 节点（标签是路径）；
- 每个 `entities.json` 里的对象 → 一个 `entity:` 节点；
- 自动边：`located_in`（单元→文档）、`mentions`（单元→对象，靠对象的 aliases 匹配）；
- 人工边：`related_to`（界面"关联到当前节点"，仅作导航）；
- 显式关系：`links/relations.yaml` 里登记的分组 → `relation_group` 节点 + `part_of` 边（要求 `quote` 真的出现在指定行）；
- 每条边有 `lifecycle: active|disabled`；停用的边仍显示（虚线），但不参与检索遍历。

## 候选

同一对象下、标题有共同词的两个条目会被提为"可能同主题"的候选（上限 200，见 `docs/LIMITS.md`）。
候选**不是**已发布的关系：接受后才写进图；拒绝也会记下判断。

## 版本与失效

- 取原文时同时给出 `unit_id` 与 `source_version`；文件改过就返回 `source_changed`，不会拿旧快照冒充新内容。
- 建图后你又改了文件：先 `build` 刷新，再查；界面上方的"重新读取来源"就是做这件事。
