"""命令行入口：init / scan / build / query / fetch / serve / doctor。

所有命令只认 --config 指向的配置文件；不在源码树里写任何运行状态。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from .common import MemoryErrorCode, load_config
from .readers import scan


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def cmd_init(args):
    """在用户指定的数据目录里放一份最小可用的配置与空库骨架（--demo 时填虚构演示库）。"""
    from .profile import STARTER
    data_dir = Path(args.dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    library = data_dir / "library"
    for sub in ("entries", "knowledge", "experience", "links"):
        (library / sub).mkdir(parents=True, exist_ok=True)
    for name, text in (("open-threads.md", "# 待办\n\n| 事项 | 状态 |\n| --- | --- |\n"),
                       ("promises.md", "# 承诺\n\n| 承诺 | 对象 |\n| --- | --- |\n"),
                       ("profile.md", "# 关于我\n\n（在这里写你自己的背景；这是资料，不是程序配置。）\n")):
        path = library / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
    entities = data_dir / "entities.json"
    if not entities.exists():
        _write_json(entities, [])
    profile = data_dir / "profile.json"
    if getattr(args, "demo", False):
        source = Path(__file__).resolve().parent / "data"
        demo = source / "demo-library"
        for file in sorted(demo.rglob("*")):
            if file.is_file():
                target = library / file.relative_to(demo)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.read_bytes())
        profile.write_bytes((source / "profile-demo.json").read_bytes())
        _write_json(entities, json.loads((library / "entities.json").read_text(encoding="utf-8")))
    elif not profile.exists():
        profile.write_text(json.dumps(STARTER, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    config = data_dir / "she-memory.json"
    if not config.exists():
        _write_json(config, {
            "mode": "source-readonly",
            "memory_root": "./library",
            "state_root": "./data/graph",
            "view_root": "./data/runtime",
            "entities_file": "./entities.json",
            "profile_file": "./profile.json",
            "roles_file": "",
        })
    print(json.dumps({"ok": True, "data_dir": str(data_dir), "config": str(config)}, ensure_ascii=False))
    return 0


def cmd_scan(args):
    cfg = load_config(args.config)
    from .profile import load_profile
    result = scan(cfg["memory_root"], load_profile(cfg.get("profile_file")))
    print(json.dumps(result["coverage"], ensure_ascii=False, indent=1))
    return 0


def _brain(config_path):
    from .core import Brain
    return Brain(load_config(config_path))


def cmd_build(args):
    result = _brain(args.config).refresh()
    print(json.dumps({k: result[k] for k in ("revision", "units", "nodes", "edges")}, ensure_ascii=False))
    return 0


def cmd_query(args):
    context = json.loads(args.context) if args.context else None
    result = _brain(args.config).query(args.text, args.max_items, context)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["status"] in ("ok", "insufficient", "ambiguous", "navigation") else 1


def cmd_fetch(args):
    result = _brain(args.config).fetch(args.unit, args.version or None)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def cmd_doctor(args):
    brain = _brain(args.config)
    health = brain.health()
    print(json.dumps(health, ensure_ascii=False, indent=1))
    return 0


def cmd_serve(args):
    from .server import serve
    brain = _brain(args.config)
    serve(brain, args.port)
    return 0


def main(argv=None):
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（init 生成的 she-memory.json）；放在子命令前后都行")
    parser = argparse.ArgumentParser(prog="she-memory", parents=[base], description="本地记忆服务：图谱、检索、只读来源")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="在指定目录生成最小配置与空库骨架")
    p_init.add_argument("dir", help="数据目录（会被创建）")
    p_init.add_argument("--demo", action="store_true", help="填入虚构演示库（连同演示 profile）")
    p_init.set_defaults(func=cmd_init)

    for name, func, help_text in (
        ("scan", cmd_scan, "只扫描来源，打印解析覆盖"),
        ("build", cmd_build, "建图/刷新（唯一写入图的入口）"),
        ("doctor", cmd_doctor, "打印服务健康与图统计"),
    ):
        p = sub.add_parser(name, help=help_text, parents=[base])
        p.set_defaults(func=func)

    p_query = sub.add_parser("query", help="查询", parents=[base])
    p_query.add_argument("text")
    p_query.add_argument("--max-items", type=int, default=3)
    p_query.add_argument("--context", help="JSON，例如 {\"focus_terms\": [\"阀门\"]}")
    p_query.set_defaults(func=cmd_query)

    p_fetch = sub.add_parser("fetch", help="按 unit_id + source_version 取原文", parents=[base])
    p_fetch.add_argument("--unit", required=True)
    p_fetch.add_argument("--version", help="期望的 source_version（省略表示不校验）")
    p_fetch.set_defaults(func=cmd_fetch)

    p_serve = sub.add_parser("serve", help="启动只读 HTTP 服务（默认 127.0.0.1）", parents=[base])
    p_serve.add_argument("--port", type=int, default=18791)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    config = getattr(args, "config", None)
    args.config = config
    if args.command != "init" and not config:
        parser.error("需要 --config <she-memory.json>（或用 init 生成一份）")
    try:
        return args.func(args)
    except MemoryErrorCode as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
