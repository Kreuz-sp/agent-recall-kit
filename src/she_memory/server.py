import hmac
import json
import os
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .common import MemoryErrorCode, atomic_json, read_json

WEB = Path(__file__).resolve().parent / 'web'   # 随包分发的前端资源


def load_token(root):
    path = Path(root) / 'access-token.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as out:
            out.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text(encoding='utf-8').strip()


def make_server(brain, port=18767):
    token = load_token(brain.config['view_root'])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, mime='application/json; charset=utf-8'):
            # 连接可能已经断了：发送失败不该冒泡成 500/栈
            try:
                raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                return

        def dispatch(self):
            actual_port = self.server.server_address[1]
            allowed = {f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'}
            if self.headers.get('Host') not in allowed:
                raise MemoryErrorCode('forbidden_host')
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + host for host in allowed}:
                raise MemoryErrorCode('forbidden_origin')
            path = urlsplit(self.path).path
            if self.command == 'GET' and path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                self.send(200, (WEB / name).read_bytes(), mime + '; charset=utf-8')
                return
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer ' + token):
                raise MemoryErrorCode('unauthorized')
            body = {}
            if self.command == 'POST':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise MemoryErrorCode('invalid_content_type')
                size = int(self.headers.get('Content-Length', '0'))
                # 布局要能装下整张图（约 2800 个位置 ≈ 150KB），其余接口维持 64KB 上限
                limit = 4 * 1024 * 1024 if path == '/v1/views/default' else 65536
                if not 0 < size <= limit:
                    raise MemoryErrorCode('request_size_limit')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise MemoryErrorCode('invalid_request')
            if self.command == 'GET' and path == '/v1/health':
                result = brain.health()
            elif self.command == 'GET' and path == '/v1/capabilities':
                result = brain.health()['capabilities']
            elif self.command == 'GET' and path.startswith('/v1/queries/') and path.endswith('/explain'):
                result = brain.traces.get(path.split('/')[3])
                if result is None:
                    raise MemoryErrorCode('trace_expired')
            elif self.command == 'POST' and path == '/v1/query':
                # context 透传：焦点/上一轮话题由调用方给，服务端不猜
                result = brain.query(body.get('text'), body.get('max_items', 3), body.get('context'))
            elif self.command == 'POST' and path == '/v1/fetch':
                result = brain.fetch(body.get('unit_id'), body.get('expected_source_version'))
            elif self.command == 'POST' and path == '/v1/feedback':
                # 只收元数据：unit_id / 版本 / event_id / result；不接收问句或正文
                result = brain.feedback(body.get('unit_id'), body.get('expected_source_version'),
                                        body.get('event_id'), body.get('result'),
                                        body.get('expected_revision'),
                                        body.get('idempotency_key') or body.get('event_id'))
            elif self.command == 'POST' and path == '/v1/graph/view':
                result = brain.graph_view(body.get('focus'), body.get('limit', 120), body.get('mode'))
            elif self.command == 'POST' and path == '/v1/candidates/detail':
                result = brain.candidate_detail(body.get('candidate_ids') or body.get('candidate_id'))
            elif self.command == 'POST' and path == '/v1/graph/edit':
                result = brain.edit(body.get('action'), body.get('payload', {}),
                                    body.get('expected_revision'), body.get('idempotency_key'))
            elif self.command == 'POST' and path == '/v1/refresh':
                result = brain.refresh()
            elif self.command == 'GET' and path == '/v1/views/default':
                view = Path(brain.config['view_root']) / 'layout.json'
                result = read_json(view) if view.exists() else {'revision': 0, 'positions': {}}
            elif self.command == 'POST' and path == '/v1/views/default':
                positions = body.get('positions')
                if not isinstance(positions, dict) or len(positions) > 5000:
                    raise MemoryErrorCode('invalid_layout')
                for identity, point in positions.items():
                    if not isinstance(identity, str) or not isinstance(point, dict) or set(point) != {'x', 'y'}:
                        raise MemoryErrorCode('invalid_layout')
                    if any(type(point[k]) not in (float, int) or not -10000 <= point[k] <= 10000 for k in ('x', 'y')):
                        raise MemoryErrorCode('invalid_layout')
                with brain.store.writer():
                    view = Path(brain.config['view_root']) / 'layout.json'
                    old = read_json(view) if view.exists() else {'revision': 0, 'positions': {}}
                    if old['revision'] != body.get('expected_view_revision'):
                        raise MemoryErrorCode('revision_conflict')
                    result = {'revision': old['revision'] + 1, 'positions': positions}
                    atomic_json(view, result)
            elif self.command == 'POST' and path in ('/v1/proposals', '/v1/outcomes'):
                raise MemoryErrorCode('not_supported')
            else:
                raise MemoryErrorCode('not_found')
            self.send(200, result)

        def handle_call(self):
            try:
                self.dispatch()
            except MemoryErrorCode as exc:
                status = 401 if exc.code == 'unauthorized' else 403 if exc.code.startswith('forbidden') else 409 if exc.code in ('revision_conflict', 'idempotency_conflict', 'source_changed') else 404 if exc.code in ('not_found', 'trace_expired') else 501 if exc.code == 'not_supported' else 400
                self.send(status, {'error': {'code': exc.code}})
            except (ValueError, TypeError, KeyError):
                self.send(400, {'error': {'code': 'invalid_request'}})
            except (OSError, RuntimeError):
                try:
                    self.send(503, {'error': {'code': 'unavailable'}})
                except OSError:
                    pass          # 连接已经断了，回不回去都无所谓

        do_GET = handle_call
        do_POST = handle_call

    class QuietServer(HTTPServer):
        # 单实例：Windows 上 allow_reuse_address=True（SO_REUSEADDR）允许第二个进程绑同一端口，
        # 会让两个实例同时服务、并各自写同一张图。这里禁掉复用，并在 bind 前用
        # SO_EXCLUSIVEADDRUSE 独占端口——两者不能混用，所以不再设 SO_REUSEADDR。
        allow_reuse_address = False

        def server_bind(self):
            if os.name == 'nt':
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except (AttributeError, OSError):
                    pass
            super().server_bind()

        def handle_error(self, request, client_address):
            # 客户端提前断开（ConnectionReset / BrokenPipe）是常态，不值得刷栈
            pass

    return QuietServer(('127.0.0.1', port), Handler)


def serve(brain, port):
    try:
        server = make_server(brain, port)
    except OSError as exc:
        # 绑定失败 = 端口已被别的实例/程序独占。第二个实例不许进服务循环。
        print(f'bind failed on 127.0.0.1:{port}: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
    print(f'Memory service: http://127.0.0.1:{server.server_address[1]}', flush=True)
    print(f'Mode: {brain.config["mode"]}; access code file: {Path(brain.config["view_root"]) / "access-token.txt"}', flush=True)
    print('Ctrl+C to stop. No automatic startup; sources are read-only.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
