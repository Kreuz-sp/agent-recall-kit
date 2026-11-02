import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


class MemoryErrorCode(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def digest(data):
    if not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_id(namespace, value):
    return namespace + ":" + str(uuid.uuid5(uuid.NAMESPACE_URL, namespace + ":" + value))


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def safe_file(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or ":" in str(relative):
        raise MemoryErrorCode("path_outside_root")
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise MemoryErrorCode("link_rejected")
    target = cursor.resolve()
    if not target.is_relative_to(root):
        raise MemoryErrorCode("path_outside_root")
    return target


def load_config(path):
    path = Path(path).resolve()
    cfg = read_json(path)
    for key in ("memory_root", "state_root", "view_root", "entities_file", "profile_file"):
        if not cfg.get(key):
            continue
        value = Path(cfg[key])
        cfg[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    cfg["config_path"] = str(path)
    if cfg.get("mode") not in ("sandbox", "source-readonly"):
        raise MemoryErrorCode("unsupported_mode", "Framework supports sandbox and source-readonly only")
    for writable in ("state_root", "view_root"):
        if Path(cfg[writable]).is_relative_to(Path(cfg["memory_root"])):
            raise MemoryErrorCode("state_inside_source")
    return cfg
