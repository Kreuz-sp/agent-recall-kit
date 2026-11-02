"""备份 / 校验 / 隔离恢复。

一份备份里装什么：
  · state_root 下的图快照（CURRENT.json + snapshots/*.json）——人工决定、停用、布局版本都在里面
  · view_root 下的 layout.json（界面布局）
  · config 的快照副本（**不含** access-token.txt）
  · manifest.json：每个条目的 sha256，用于 restore 前逐项校验

命令：
  create  --config X [--out DIR]      生成 <out>/agent-recall-kit-backup-<时间戳>.zip
  verify  --archive Z                 逐项校验 sha256
  restore --archive Z --to DIR        解到隔离目录（不覆盖正在运行的实例）

退出码：0 成功；1 失败/校验不过。
"""

import argparse
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

from she_memory.common import load_config

EXCLUDE_NAMES = {"access-token.txt", "memctl.json", "start.lock", "serve.log", "ensure.log",
                 "writer.lock"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(cfg):
    items = []
    state = Path(cfg["state_root"])
    view = Path(cfg["view_root"])
    if state.is_dir():
        items.append((state / "CURRENT.json", "state/CURRENT.json"))
        for file in sorted((state / "snapshots").glob("*.json")) if (state / "snapshots").is_dir() else []:
            items.append((file, "state/snapshots/" + file.name))
    layout = view / "layout.json"
    if layout.is_file():
        items.append((layout, "view/layout.json"))
    config = Path(cfg["config_path"])
    if config.is_file():
        items.append((config, "config/" + config.name))
    return [(src, arc) for src, arc in items if src.is_file()]


def cmd_create(cfg, out_dir):
    out = Path(out_dir).resolve() if out_dir else Path(cfg["state_root"]).parent / "backups"
    # 默认目录由配置决定、与 cwd 无关；也绝不允许落在来源目录里（否则备份会被当成来源递归收录）
    source = Path(cfg["memory_root"]).resolve()
    try:
        out.resolve().relative_to(source)
    except ValueError:
        pass
    else:
        print(json.dumps({"ok": False, "error": "backup_inside_source", "target": str(out)},
                         ensure_ascii=False))
        return 1
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = out / f"agent-recall-kit-backup-{stamp}.zip"
    entries = collect(cfg)
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "config": cfg["config_path"],
                "state_root": cfg["state_root"], "items": []}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arc in entries:
            zf.write(src, arc)
            manifest["items"].append({"arcname": arc, "sha256": sha256(src), "size": src.stat().st_size})
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    print(json.dumps({"ok": True, "archive": str(archive), "items": len(entries)}, ensure_ascii=False))
    return 0


def cmd_verify(archive):
    archive = Path(archive).resolve()
    problems = []
    with zipfile.ZipFile(archive) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            print(json.dumps({"ok": False, "error": "manifest.json missing"}, ensure_ascii=False))
            return 1
        for item in manifest["items"]:
            try:
                data = zf.read(item["arcname"])
            except KeyError:
                problems.append({"arcname": item["arcname"], "problem": "missing"})
                continue
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                problems.append({"arcname": item["arcname"], "problem": "hash_mismatch"})
    ok = not problems
    print(json.dumps({"ok": ok, "archive": str(archive), "items": len(manifest["items"]),
                      "problems": problems}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def cmd_restore(archive, target):
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        print(json.dumps({"ok": False, "error": "target_not_empty", "target": str(target)},
                         ensure_ascii=False))
        return 1
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(Path(archive).resolve()) as zf:
        zf.extractall(target)
    print(json.dumps({"ok": True, "restored_to": str(target),
                      "next": "把 config/ 下的配置里的 state_root/view_root 指到这个目录，再 start；"
                              "不要覆盖正在运行实例的数据目录"}, ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-recall-kit-backup")
    sub = parser.add_subparsers(dest="command", required=True)
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--config", default=argparse.SUPPRESS)

    p_create = sub.add_parser("create", parents=[base], help="创建备份")
    p_create.add_argument("--out", help="备份输出目录（默认 <state_root>/../backups，与 cwd 无关）")
    p_verify = sub.add_parser("verify", parents=[base], help="校验备份")
    p_verify.add_argument("--archive", required=True)
    p_restore = sub.add_parser("restore", parents=[base], help="隔离恢复（解到空目录）")
    p_restore.add_argument("--archive", required=True)
    p_restore.add_argument("--to", required=True)

    args = parser.parse_args(argv)
    if args.command == "create":
        cfg = load_config(getattr(args, "config", None))
        return cmd_create(cfg, args.out)
    if args.command == "verify":
        return cmd_verify(args.archive)
    return cmd_restore(args.archive, args.to)


if __name__ == "__main__":
    raise SystemExit(main())
