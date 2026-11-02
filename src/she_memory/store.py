"""Single-writer, immutable snapshots. CURRENT is the publication boundary."""
import contextlib
import json
import os
import threading
from pathlib import Path

from .common import MemoryErrorCode, atomic_json, digest, now, read_json


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()

    @contextlib.contextmanager
    def writer(self):
        with self.mutex:
            handle = (self.root / 'writer.lock').open('a+b')
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            locked = False
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                yield
            except OSError as exc:
                raise MemoryErrorCode('writer_or_io_unavailable') from exc
            finally:
                if locked:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def read(self):
        pointer = self.root / 'CURRENT.json'
        if not pointer.exists():
            return {'schema_version': '1.0', 'revision': 0, 'units': [], 'nodes': [], 'edges': [],
                    'candidates': [], 'decisions': {}, 'manual_edges': {}, 'disabled': [],
                    'coverage': {}, 'events': [], 'requests': {}}
        current = read_json(pointer)
        revision = current['revision']
        if type(revision) is not int or revision < 1:
            raise MemoryErrorCode('invalid_snapshot_pointer')
        snapshot = read_json(self.root / 'snapshots' / f'{revision}.json')
        if digest(snapshot) != current['hash']:
            raise MemoryErrorCode('snapshot_integrity_failure')
        return snapshot

    def publish(self, data, operation):
        old = self.read()
        data['revision'] = old['revision'] + 1
        data['updated_at'] = now()
        event = {'revision': data['revision'], 'operation': operation, 'at': now()}
        data['events'] = old['events'] + [event]
        atomic_json(self.root / 'snapshots' / f"{data['revision']}.json", data)
        atomic_json(self.root / 'CURRENT.json', {'revision': data['revision'], 'hash': digest(data)})
        # Events live inside the published snapshot. JSONL is a convenience mirror.
        try:
            with (self.root / 'journal.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps(event, ensure_ascii=False) + '\n')
                out.flush()
                os.fsync(out.fileno())
        except OSError:
            pass  # CURRENT + embedded events remain the authoritative committed transaction.
        return data

    def mutate(self, expected, key, operation, change):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise MemoryErrorCode('idempotency_key_required')
        with self.writer():
            data = self.read()
            prior = data['requests'].get(key)
            if prior:
                if prior['operation'] != operation:
                    raise MemoryErrorCode('idempotency_conflict')
                return prior['result']
            if type(expected) is not int or expected != data['revision']:
                raise MemoryErrorCode('revision_conflict')
            payload = change(data)
            result = {'revision': data['revision'] + 1, **(payload or {})}
            data['requests'][key] = {'operation': operation, 'result': result}
            self.publish(data, operation)
            return result
