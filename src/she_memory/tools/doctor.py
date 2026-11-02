"""体检：配置能不能读、来源在不在、profile 能不能用、图建没建、连接码在不在。

只读，不修改任何文件。退出码：0 正常；1 有需要处理的问题。
"""

import argparse
import json
import sys
from pathlib import Path

from she_memory.common import load_config
from she_memory.core import Brain
from she_memory.profile import load_profile


def check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": detail}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-recall-kit-doctor")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    results = []
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=1))
        return 1

    results.append(check("配置可读", True, cfg["config_path"]))
    results.append(check("来源目录存在", Path(cfg["memory_root"]).is_dir(), cfg["memory_root"]))
    try:
        profile = load_profile(cfg.get("profile_file"))
        results.append(check("profile 可读", True, json.dumps(profile.summary(), ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001
        results.append(check("profile 可读", False, str(exc)))

    try:
        health = Brain(cfg).health()
        results.append(check("图已建立", health["revision"] >= 1, f"revision={health['revision']}"))
        results.append(check("解析没有错误", health["coverage"].get("errors", 0) == 0,
                             json.dumps(health["coverage"], ensure_ascii=False)))
        token = Path(cfg["view_root"]) / "access-token.txt"
        results.append(check("连接码已生成（首次启动时本机生成）", token.is_file(), str(token)))
    except Exception as exc:  # noqa: BLE001
        results.append(check("图可读", False, f"{type(exc).__name__}: {exc}"))

    ok = all(r["ok"] for r in results)
    print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
