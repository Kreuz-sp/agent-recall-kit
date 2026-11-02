"""记忆服务的运行入口：start / stop / status / restart / ensure。

设计：
  · 进程脱离当前终端（DETACHED），关掉窗口服务还在；
  · 重复启动不多开第二份：已在跑就复用；端口被别的程序占着只报错，不杀别人；
  · 停止只针对**能确认身份**的本程序实例（pid 记录 + /v1/health 指纹 + 端口归属三重核验）；
  · 启动与巡检共用一把文件锁；真正的单实例由操作系统的 bind 失败兜底（见 server.py）。

所有路径都来自 --config：pid/日志写在配置的 view_root 下，不写源码树。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class Ctx:
    def __init__(self, config_path, port=None):
        self.config_path = Path(config_path).resolve()
        self.cfg = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self.port = int(port or self.cfg.get("port") or 18791)
        view_root = Path(self.cfg.get("view_root", "./data/runtime"))
        self.view_root = (view_root if view_root.is_absolute()
                          else (self.config_path.parent / view_root).resolve())
        self.pid_file = self.view_root / "memctl.json"
        self.lock_file = self.view_root / "start.lock"
        self.log_file = self.view_root / "serve.log"
        self.ensure_log = self.view_root / "ensure.log"
        self.token_file = self.view_root / "access-token.txt"

    def token(self):
        return self.token_file.read_text(encoding="utf-8").strip() if self.token_file.is_file() else ""


class LockBusy(Exception):
    pass


class StartLock:
    """跨进程启动锁（进程退出自动释放）。"""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"x")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockBusy(str(exc)) from exc
        self.handle = handle
        return self

    def __exit__(self, *exc_info):
        if self.handle is None:
            return False
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None
        return False


def _run(cmd):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return out.stdout or ""


def health(port, token, timeout=3):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health")
    request.add_header("Authorization", "Bearer " + (token or ""))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, None, "HTTP %d" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, None, type(exc).__name__
    mine = isinstance(data, dict) and data.get("schema_version") == "1.0" and "mode" in data
    return mine, data, None


def port_listening(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def port_owner(port):
    try:
        text = _run(["netstat", "-ano", "-p", "TCP"])
    except Exception:  # noqa: BLE001
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}") \
                and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def pid_alive(pid):
    try:
        return str(pid) in _run(["tasklist", "/FI", "PID eq %d" % int(pid)])
    except Exception:  # noqa: BLE001
        return False


def read_pid(ctx):
    try:
        return json.loads(ctx.pid_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_pid(ctx, pid, source="start"):
    ctx.view_root.mkdir(parents=True, exist_ok=True)
    ctx.pid_file.write_text(json.dumps({
        "pid": int(pid), "port": ctx.port,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": str(ctx.config_path), "source": source,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def sync_pid_owner(ctx):
    owner = port_owner(ctx.port)
    if not owner:
        return None
    info = read_pid(ctx) or {}
    if info.get("pid") != owner:
        write_pid(ctx, owner, source="discovered")
    return owner


def log_ensure(ctx, result, detail=""):
    try:
        ctx.view_root.mkdir(parents=True, exist_ok=True)
        if ctx.ensure_log.is_file() and ctx.ensure_log.stat().st_size > 128 * 1024:
            keep = ctx.ensure_log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            ctx.ensure_log.write_text("\n".join(keep) + "\n", encoding="utf-8")
        with ctx.ensure_log.open("a", encoding="utf-8") as handle:
            handle.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), result, detail))
    except OSError:
        pass


def spawn(ctx):
    ctx.view_root.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-B", "-X", "utf8", "-m", "she_memory",
           "--config", str(ctx.config_path), "serve", "--port", str(ctx.port)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with ctx.log_file.open("ab") as handle:
        proc = subprocess.Popen(cmd, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True)
    for _ in range(40):
        time.sleep(0.25)
        mine, data, _err = health(ctx.port, ctx.token(), timeout=1)
        if mine:
            owner = port_owner(ctx.port)
            if owner is None:   # 无控制台环境（例如计划任务）里 netstat 可能拿不到输出
                owner = proc.pid if proc.poll() is None else None
            if owner == proc.pid:
                write_pid(ctx, proc.pid, "start")
                return 0, proc.pid, data.get("revision"), True
            return 0, proc.pid, data.get("revision"), False
    return 1, proc.pid, None, False


def cmd_start(ctx):
    if port_listening(ctx.port):
        mine, data, err = health(ctx.port, ctx.token())
        if mine:
            owner = sync_pid_owner(ctx)
            print(f"已在运行：图 r{data.get('revision')}，端口 {ctx.port}（监听 pid={owner}）")
            return 0
        print(f"端口 {ctx.port} 被其它程序占用（{err or '指纹不符'}）。不启动，也不杀别人。")
        return 2
    try:
        with StartLock(ctx.lock_file):
            mine, data, _err = health(ctx.port, ctx.token(), timeout=2)
            if mine:
                sync_pid_owner(ctx)
                print(f"已在运行（另一个入口刚拉起）：图 r{data.get('revision')}，端口 {ctx.port}")
                return 0
            if port_listening(ctx.port):
                print(f"端口 {ctx.port} 刚被别的程序占用。不启动，也不杀别人。")
                return 2
            code, pid, revision, owned = spawn(ctx)
    except LockBusy:
        print("另一个 start/ensure 正在拉起进程，本次不动。")
        return 3
    if code == 0 and owned:
        print(f"已启动：pid={pid} 端口={ctx.port} 图 r{revision}（日志 {ctx.log_file}）")
        return 0
    if code == 0:
        print(f"另一个实例先绑上了端口（pid={port_owner(ctx.port)}），本次没抢到、已退出；pid 记录未覆盖。")
        return 0
    print(f"启动后 10 秒内没拿到健康响应。看日志：{ctx.log_file}")
    return 1


def cmd_stop(ctx):
    info = read_pid(ctx)
    mine, _data, err = health(ctx.port, ctx.token())
    if not mine:
        print(f"端口 {ctx.port} 上没有本程序在跑（{err or '无响应'}）。什么也没做。")
        return 1
    if not info:
        print("服务在跑，但没有 pid 记录——无法确认身份，不杀。")
        return 2
    pid = int(info.get("pid", 0))
    if info.get("port") != ctx.port:
        print(f"pid 记录里的端口是 {info.get('port')}，不是 {ctx.port}——不杀。")
        return 2
    if Path(str(info.get("config", ""))).resolve() != ctx.config_path:
        print("pid 记录里的配置不是当前配置——不杀。")
        return 2
    if port_owner(ctx.port) != pid:
        print(f"端口 {ctx.port} 现在属于 pid {port_owner(ctx.port)}，不是 pid {pid}——不杀。")
        return 2
    if not pid_alive(pid):
        print(f"pid {pid} 已不在，只清理 pid 记录。")
        ctx.pid_file.unlink(missing_ok=True)
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    for _ in range(20):
        time.sleep(0.25)
        if not port_listening(ctx.port):
            break
    if port_listening(ctx.port):
        print(f"停止失败：pid={pid} 仍在，端口 {ctx.port} 仍在监听")
        return 1
    ctx.pid_file.unlink(missing_ok=True)
    print(f"已停止 pid={pid}（端口 {ctx.port} 已释放）")
    return 0


def cmd_status(ctx):
    info = read_pid(ctx)
    listening = port_listening(ctx.port)
    mine, data, err = health(ctx.port, ctx.token())
    print(f"配置      : {ctx.config_path}")
    print(f"运行目录  : {ctx.view_root}")
    print(f"端口 {ctx.port}   : {'在监听' if listening else '未监听'}")
    if info:
        print("pid 记录  : pid=%s port=%s started=%s alive=%s"
              % (info.get("pid"), info.get("port"), info.get("started"),
                 pid_alive(info.get("pid", 0))))
    else:
        print("pid 记录  : 无")
    if mine:
        print("服务      : 正常 · 图 r%s · %s 节点 / %s 边 · 单元 %s"
              % (data.get("revision"), data.get("nodes"), data.get("edges"),
                 (data.get("coverage") or {}).get("units")))
        return 0
    if listening:
        print(f"服务      : 端口有东西在听，但不是本程序（{err or '指纹不符'}）")
        return 2
    print("服务      : 未在运行")
    return 1


def cmd_ensure(ctx):
    mine, data, err = health(ctx.port, ctx.token(), timeout=2)
    if mine:
        owner = sync_pid_owner(ctx)
        print(f"在跑，不动：图 r{data.get('revision')}，端口 {ctx.port}（监听 pid={owner}）")
        log_ensure(ctx, "alive", "pid=%s revision=%s" % (owner, data.get("revision")))
        return 0
    if port_listening(ctx.port):
        print(f"端口 {ctx.port} 有东西在听，但不是本程序（{err or '指纹不符'}）——不杀，也不启动。")
        log_ensure(ctx, "port-conflict", "port=%d reason=%s" % (ctx.port, err or "fingerprint-mismatch"))
        return 2
    try:
        with StartLock(ctx.lock_file):
            mine, data, _err = health(ctx.port, ctx.token(), timeout=2)
            if mine:
                print(f"在跑，不动（另一入口刚拉起）：图 r{data.get('revision')}")
                log_ensure(ctx, "alive-race", "revision=%s" % data.get("revision"))
                return 0
            code, pid, revision, owned = spawn(ctx)
    except LockBusy:
        print("另一次 start/ensure 正在拉起进程，本次不动。")
        log_ensure(ctx, "lock-busy", "port=%d" % ctx.port)
        return 3
    if code == 0 and owned:
        print(f"ensure：已拉起 pid={pid} 图 r{revision}")
        log_ensure(ctx, "started", "pid=%d revision=%s" % (pid, revision))
        return 0
    if code == 0:
        print(f"ensure：端口已被另一个实例占用（pid={port_owner(ctx.port)}），本次没抢到。")
        log_ensure(ctx, "lost-race", "pid=%s" % port_owner(ctx.port))
        return 0
    log_ensure(ctx, "start-failed", "pid=%s reason=no-health-in-10s" % pid)
    print(f"ensure：启动失败，10 秒内没拿到健康响应。看 {ctx.log_file}")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-recall-kit-ctl", description="记忆服务运行入口")
    parser.add_argument("action", choices=["start", "stop", "status", "restart", "ensure"])
    parser.add_argument("--config", required=True, help="agent-recall-kit.json 路径")
    parser.add_argument("--port", type=int, help="覆盖配置里的端口")
    args = parser.parse_args(argv)
    ctx = Ctx(args.config, args.port)
    dispatch = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "ensure": cmd_ensure}
    try:
        if args.action == "restart":
            code = cmd_stop(ctx)
            if code not in (0, 1):
                return code
            return cmd_start(ctx)
        return dispatch[args.action](ctx)
    except Exception as exc:  # noqa: BLE001 —— 计划任务里没有控制台，异常要落日志
        log_ensure(ctx, "exception", "%s: %s" % (type(exc).__name__, exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
