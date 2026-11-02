"""测试夹具：全部用 tmp_path 里的假库与临时端口，不读作者本机任何配置。"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

PROFILE = {
    "name": "test-profile",
    "objects": [
        {"id": "valve", "pattern": "滴灌阀|阀门|阀组", "words": ["滴灌阀", "阀门", "阀组"]},
        {"id": "controller", "pattern": "控制器|固件", "words": ["控制器", "固件"]},
    ],
    "targets": [
        {"id": "verify", "pattern": "验收|检查", "locator_pattern": "(^|/)verify"},
        {"id": "model", "pattern": "型号|版本号", "locator_pattern": "(^|/)(model|firmware)"},
        {"id": "method", "pattern": "怎么|如何|步骤", "locator_pattern": None},
    ],
    "hints": {"navigation": "待办|承诺|答应", "threads": "待办", "promises": "承诺",
              "procedure": "怎么|如何|步骤"},
    "stop_words": ["今天", "明天", "现在"],
    "scoring": {"min_score": 2.0, "graph_only_base": 2.5,
                "procedure_verifiable_pattern": "回读|确认|核对"},
    "format": {"root_files": ["open-threads.md", "promises.md", "profile.md"],
               "dirs": ["entries", "knowledge", "experience", "links"],
               "key_aliases": {"valve": "阀门 滴灌阀", "verify": "验收"}},
    "navigation": {"threads_file": "open-threads.md", "promises_file": "promises.md"},
    "candidate": {"stop_words": ["记录"]},
    "evidence": {"marks": {"has_method": "步骤|方法|做法"},
                 "rules": [{"code": "method_steps", "when_target": "method",
                            "unless_history": True, "require": ["has_method"]}]},
}

ENTRY_A = """# 2026-05-01 滴灌阀更换

今天把 3 号滴灌阀换掉了。更换后做了外观检查。
回读控制器日志留到明天。
"""

ENTRY_B = """# 2026-05-12 滴灌阀校准

按手册校准了滴灌阀流量。没有拆阀，只调了限流螺丝。
"""

FACT = """valve:
  model: DV-12
  installed: 2026-05-01
  verify:
    - 关闭主阀，确认压力表归零
    - 手动开关三次
controller:
  firmware: 2.4.1
"""

THREADS = """# 待办

| 事项 | 状态 |
| --- | --- |
| 滴灌阀验收 | 进行中 |
| 控制器回滚 | 未开始 |
"""

PROMISES = """# 承诺

| 承诺 | 对象 |
| --- | --- |
| 带备用阀 | 滴灌阀 |
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_library(root):
    write(root / "profile.md", "# 关于我\n\n测试库。\n")
    write(root / "open-threads.md", THREADS)
    write(root / "promises.md", PROMISES)
    write(root / "01_site.yaml", FACT)
    write(root / "entries" / "2026-05-01-valve.md", ENTRY_A)
    write(root / "entries" / "2026-05-12-valve.md", ENTRY_B)
    write(root / "knowledge" / "verification.md", "# 验收清单\n\n1. 关主阀\n2. 手动开关\n")
    write(root / "entities.json", json.dumps([
        {"id": "valve", "name": "滴灌阀", "kind": "object", "aliases": ["滴灌阀", "阀门"]},
        {"id": "controller", "name": "控制器", "kind": "object", "aliases": ["控制器", "固件"]},
    ], ensure_ascii=False))
    return root


def make_config(tmp_path, library):
    data = tmp_path / "data"
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "agent-recall-kit.json"
    config.write_text(json.dumps({
        "mode": "source-readonly",
        "memory_root": str(library),
        "state_root": str(data / "graph"),
        "view_root": str(data / "runtime"),
        "entities_file": str(library / "entities.json"),
        "profile_file": str(profile),
        "roles_file": "",
    }, ensure_ascii=False), encoding="utf-8")
    return config


@pytest.fixture
def workspace(tmp_path):
    library = make_library(tmp_path / "library")
    config = make_config(tmp_path, library)
    return {"tmp": tmp_path, "library": library, "config": config}


def brain(config):
    from she_memory.common import load_config
    from she_memory.core import Brain
    return Brain(load_config(str(config)))


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, config, port):
        self.port = port
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-m", "she_memory",
             "--config", str(config), "serve", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONPATH": str(SRC)})
        token_file = Path(json.loads(Path(config).read_text(encoding="utf-8"))["view_root"])
        if not token_file.is_absolute():
            token_file = (Path(config).parent / token_file)
        self.token_file = token_file / "access-token.txt"
        for _ in range(80):
            if self.token_file.is_file() and self.health() is not None:
                return
            time.sleep(0.25)
        raise RuntimeError("server did not start")

    def token(self):
        return self.token_file.read_text(encoding="utf-8").strip()

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method="GET" if data is None else "POST")
        req.add_header("Authorization", "Bearer " + self.token())
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def health(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1):
                return True
        except Exception:
            return None

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def server(workspace):
    srv = Server(workspace["config"], free_port())
    try:
        yield srv
    finally:
        srv.close()
