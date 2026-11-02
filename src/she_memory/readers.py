"""来源读取：把记忆库目录读成"单元"。

格式约定（目录、根文件、YAML 键别名、大小上限）来自 profile.format，
代码里不写死任何具体领域或文件名。
"""

import re
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken

from .common import MemoryErrorCode, digest, safe_file, stable_id
from .profile import DEFAULT as DEFAULT_PROFILE


class StrictLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    output = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in output:
                raise MemoryErrorCode("duplicate_yaml_key")
            output[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise MemoryErrorCode("unsupported_yaml_key") from exc
    return output


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_yaml(text):
    # 初始实现直接拒绝锚点/别名，避免展开成无界图
    if any(isinstance(token, AliasToken) for token in yaml.scan(text)):
        raise MemoryErrorCode("yaml_alias_not_supported")
    data = yaml.load(text, Loader=StrictLoader)
    return data, yaml.compose(text, Loader=StrictLoader)


def selected(relative, profile=DEFAULT_PROFILE):
    fmt = profile.get("format") or {}
    root_files = set(fmt.get("root_files") or [])
    dirs = set(fmt.get("dirs") or [])
    parts = Path(relative).parts
    name = parts[-1].lower()
    if any(p.startswith(".") for p in parts):
        return False
    if len(parts) == 1:
        return name in root_files or bool(re.match(r"\d{2}_.+\.ya?ml$", name))
    return parts[0] in dirs and name not in {"readme.md", "index.md", "catalog.md"}


def units_for(relative, raw, profile=DEFAULT_PROFILE):
    fmt = profile.get("format") or {}
    key_aliases = fmt.get("key_aliases") or {}
    text = raw.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    version = digest(raw)
    document_id = stable_id("doc", relative)
    units = []
    seen = set()

    def add(key, headings, start, end, kind, metadata=None):
        if kind == 'fact' and not re.fullmatch(r'\d{2}_[^/]+\.ya?ml', relative, re.I):
            kind = 'source_metadata'
        if key in seen:
            raise MemoryErrorCode("ambiguous_source_locator")
        seen.add(key)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            return
        parent = " / ".join(headings)
        alias = " ".join(key_aliases.get(str(h), "") for h in headings)
        units.append({
            "id": stable_id("unit", relative + "#" + key), "document_id": document_id,
            "path": relative, "kind": kind, "heading_path": headings,
            "label": parent or Path(relative).stem, "locator": key,
            "line": start + 1, "end_line": end, "body": body,
            "search_text": parent + "\n" + alias + "\n" + body,
            "source_version": version, "unit_version": digest([parent, body]),
            "authority_kind": "fact_record" if kind == "fact" else kind,
            "access_scope": "private", "metadata": metadata or {},
        })

    suffix = Path(relative).suffix.lower()
    if suffix in (".yaml", ".yml"):
        _, root = parse_yaml(text)

        def end_of(node):
            mark = node.end_mark
            # 容器结束位置有时落在下一个键的缩进前，这里补回去
            included = mark.line < len(lines) and bool(lines[mark.line][:mark.column].strip())
            return mark.line + int(included)

        def walk(node, keys, depth=0):
            if depth > 32:
                raise MemoryErrorCode("yaml_depth_limit")
            if isinstance(node, MappingNode):
                pairs = {k.value: (k, v) for k, v in node.value}
                if "id" in pairs and "content" in pairs:
                    identity = pairs["id"][1].value
                    add("yaml_id:" + identity, keys + [identity], node.start_mark.line,
                        end_of(node), "fact", {"legacy_id": identity})
                    return
                for key, (key_node, child) in pairs.items():
                    if isinstance(child, ScalarNode):
                        add("yaml_path:" + "/".join(keys + [key]), keys + [key],
                            key_node.start_mark.line,
                            max(key_node.start_mark.line + 1, end_of(child)), "fact")
                    else:
                        walk(child, keys + [key], depth + 1)
            elif isinstance(node, SequenceNode):
                if all(isinstance(n, ScalarNode) for n in node.value):
                    add("yaml_path:" + "/".join(keys), keys, node.start_mark.line,
                        end_of(node), "fact")
                else:
                    for idx, child in enumerate(node.value):
                        walk(child, keys + [str(idx)], depth + 1)
        if root:
            walk(root, [])
    else:
        kind = Path(relative).parts[0] if "/" in relative else "section"
        if kind == "entries":
            kind = "entry"
        headings = []
        start = 0
        fenced = None
        key = "md:preamble"
        for idx, line in enumerate(lines):
            fence = re.match(r"^\s*(`{3,}|~{3,})", line)
            if fence:
                marker = fence[1][0]
                if fenced is None:
                    fenced = (marker, len(fence[1]))
                elif marker == fenced[0] and len(fence[1]) >= fenced[1]:
                    fenced = None
                continue
            heading = None if fenced else re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                add(key, [h[1] for h in headings], start, idx, kind)
                level, title = len(heading[1]), heading[2].strip()
                headings = [h for h in headings if h[0] < level] + [(level, title)]
                key = "md:" + "/".join(h[1] for h in headings)
                start = idx
        add(key, [h[1] for h in headings], start, len(lines), kind)
    return units


def scan(root, profile=DEFAULT_PROFILE):
    fmt = profile.get("format") or {}
    max_files = int(fmt.get("max_files", 2500))
    max_bytes = int(fmt.get("max_bytes", 1048576))
    root = Path(root).resolve()
    if not root.is_dir():
        raise MemoryErrorCode("source_unavailable")
    report = {"files": [], "parsed": 0, "excluded": 0, "errors": 0, "units": 0}
    units = []
    files = []
    import os
    for parent, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            p = Path(parent) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                report["files"].append({"path": rel, "status": "link_rejected"})
                report["errors"] += 1
                dirs.remove(name)
            elif p.parent == root and name not in set(fmt.get("dirs") or []):
                dirs.remove(name)
        for name in sorted(names):
            rel = (Path(parent) / name).relative_to(root).as_posix()
            if not selected(rel, profile):
                report["excluded"] += 1
                continue
            files.append(rel)
            if len(files) > max_files:
                raise MemoryErrorCode("file_count_limit")
    for relative in sorted(files):
        info = {"path": relative}
        try:
            path = safe_file(root, relative)
            if path.suffix.lower() not in (".md", ".yaml", ".yml"):
                info["status"] = "content_not_parsed"
            else:
                with path.open("rb") as handle:
                    raw = handle.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise MemoryErrorCode("size_limit")
                parsed = units_for(relative, raw, profile)
                units.extend(parsed)
                info.update(status="ok", source_version=digest(raw), units=len(parsed))
                report["parsed"] += 1
        except Exception as exc:  # noqa: BLE001 —— 单个文件坏掉不该拖垮整库
            info["status"] = exc.code if isinstance(exc, MemoryErrorCode) else type(exc).__name__
            report["errors"] += 1
        report["files"].append(info)
    report["units"] = len(units)
    return {"units": units, "coverage": report}


def fetch(root, unit, profile=DEFAULT_PROFILE):
    path = safe_file(root, unit["path"])
    max_bytes = int((profile.get("format") or {}).get("max_bytes", 1048576))
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError as exc:
        raise MemoryErrorCode("source_missing") from exc
    if len(raw) > max_bytes or digest(raw) != unit["source_version"]:
        raise MemoryErrorCode("source_changed")
    for current in units_for(unit["path"], raw, profile):
        if current["id"] == unit["id"]:
            return current
    raise MemoryErrorCode("source_locator_missing")
