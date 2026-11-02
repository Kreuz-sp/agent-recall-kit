"""Local JSON-lines read-only transport. This is not an MCP implementation."""
import json
import sys

from .common import MemoryErrorCode


def run(brain):
    while True:
        line = sys.stdin.readline(65537)
        if not line:
            return
        request_id = None
        try:
            if len(line) > 65536:
                while line and not line.endswith('\n'):
                    line = sys.stdin.readline(65537)
                raise MemoryErrorCode('request_size_limit')
            request = json.loads(line)
            request_id = request.get('id')
            if not isinstance(request_id, (str, int)) or len(str(request_id)) > 128:
                raise MemoryErrorCode('invalid_request_id')
            params = request.get('params', {})
            method = request.get('method')
            if method == 'memory.query':
                result = brain.query(params.get('text'))
            elif method == 'memory.fetch':
                result = brain.fetch(params.get('unit_id'), params.get('expected_source_version'))
            elif method == 'memory.capabilities':
                result = brain.health()
            else:
                raise MemoryErrorCode('not_supported')
            response = {'id': request_id, 'result': result}
        except (MemoryErrorCode, ValueError, TypeError, AttributeError, OSError) as exc:
            response = {'id': request_id, 'error': {'code': getattr(exc, 'code', 'invalid_request')}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
