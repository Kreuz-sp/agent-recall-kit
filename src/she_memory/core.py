import collections
import math
import re
import uuid

from .common import MemoryErrorCode, digest, now, read_json
from pathlib import Path
from .graph import alias_matches, compile_graph, make_edge, tokens
from .assembly import assemble
from .activation import (ActivationError, activation_entry, activation_map, bypass_reason, ensure_state,
                          order_with_activation, parse_iso, policy_from, resolve, utcnow)
from .profile import load_profile
from .query_pipeline import (build_adjacency, classify, graph_paths, load_roles,
                             needs_more_evidence, normalize, rank, registered_units, route)
from .readers import fetch, scan
from .store import Store


def file_digest(path):
    """原件当前版本的 sha256，用来在返回前核对快照是否已过期。"""
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _ReplayEvent(Exception):
    """同 event_id、同语义载荷的重放：在写锁内识别，直接返回原结果，不发布新 revision。"""

    def __init__(self, payload):
        self.payload = payload
        super().__init__('replayed feedback event')


class Brain:
    def __init__(self, config, clock=None):
        self.config = config
        self.store = Store(config['state_root'])
        self.entities = read_json(config['entities_file'])
        self.profile = load_profile(config.get('profile_file'))
        self.activation_policy = policy_from(self.profile, config)
        self.clock = clock
        self.traces = collections.OrderedDict()

    def now(self):
        return utcnow(self.clock)

    def health(self):
        data = self.store.read()
        activation = ensure_state(data)
        return {'schema_version': '1.0', 'version': '0.1.0', 'mode': self.config['mode'],
                'revision': data['revision'], 'nodes': len(data['nodes']), 'edges': len(data['edges']),
                'coverage': {k: v for k, v in data['coverage'].items() if k != 'files'},
                'profile': self.profile.summary(),
                'activation': {'enabled': self.activation_policy['enabled'],
                               'half_life_days': self.activation_policy['half_life_days'],
                               'floor': self.activation_policy['floor'],
                               'decay_kinds': self.activation_policy['decay_kinds'],
                               'tracked_units': len(activation['units']),
                               'feedback_events': len(activation['feedback'])},
                'capabilities': {'query_fetch': True, 'graph_edit': True,
                                 'feedback': True, 'source_write': False, 'automatic_supply': False,
                                 'experience_commit': False, 'object_merge': False}}

    def refresh(self):
        with self.store.writer():
            old = self.store.read()
            read = scan(self.config['memory_root'], self.profile)
            old['units'] = read['units']
            old['coverage'] = read['coverage']
            # 首次入库时间在这里初始化；已有状态一律保留，不替旧经历捏造历史。
            activation = ensure_state(old)
            stamp = self.now().isoformat().replace('+00:00', 'Z')
            for unit in read['units']:
                entry = activation['units'].get(unit['id'])
                if entry is None:
                    activation['units'][unit['id']] = {
                        'first_seen_at': stamp, 'source_version': unit['source_version'],
                        'pin': False, 'last_helpful_at': None, 'last_helpful_day': None,
                        'feedback_count': 0}
                else:
                    entry.setdefault('pin', False)
                    if entry.get('source_version') != unit['source_version']:
                        # 同 unit_id 换了版本：首见时间不重置，旧版本的有效反馈不再作用于新内容
                        entry['source_version'] = unit['source_version']
                        entry['last_helpful_at'] = None
                        entry['last_helpful_day'] = None
            # 统一装配：编译 → 核验并合并导入断言 → 应用人工停用
            old['nodes'], old['edges'], old['candidates'], report = assemble(
                self.config, read['units'], self.entities, old, self.profile)
            data = self.store.publish(old, 'refresh')
            return {'revision': data['revision'], 'units': len(data['units']),
                    'nodes': len(data['nodes']), 'edges': len(data['edges']),
                    'imported': report, 'coverage': data['coverage']}
    def fetch(self, unit_id, expected_version):
        """取回原文单元。普通单元按快照里的单元读；thread: 合成单元按行读。
        两条路径都限制在 memory_root 内，越界一律 not_found。"""
        root = Path(self.config['memory_root']).resolve()

        def inside(rel_path):
            target = (root / rel_path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise MemoryErrorCode('forbidden_path')
            return target

        if isinstance(unit_id, str) and unit_id.startswith('thread:'):
            try:
                _, rel, line_no = unit_id.split(':')
            except ValueError:
                raise MemoryErrorCode('not_found')
            path = inside(rel)
            try:
                current = file_digest(path)
                lines = path.read_text(encoding='utf-8').splitlines()
                text = lines[int(line_no) - 1]
            except (OSError, IndexError):
                raise MemoryErrorCode('not_found')
            if expected_version and expected_version != current:
                raise MemoryErrorCode('source_changed')
            return {'memory_id': unit_id, 'source_version': current, 'text': text,
                    'citation': {'path': rel, 'locator': 'thread_row',
                                 'lines': [int(line_no), int(line_no)]}}

        unit = next((u for u in self.store.read()['units'] if u['id'] == unit_id), None)
        if not unit:
            raise MemoryErrorCode('not_found')
        # 快照是否已经过期：用原件当前 hash 和快照版本比，不看调用方传什么
        try:
            current = file_digest(inside(unit['path']))
        except OSError:
            raise MemoryErrorCode('not_found')
        if current != unit['source_version']:
            raise MemoryErrorCode('source_changed', 'source changed on disk; refresh required')
        if expected_version != unit['source_version']:
            raise MemoryErrorCode('source_changed')
        return {'memory_id': unit['id'], 'source_version': unit['source_version'],
                'text': unit['body'],
                'citation': {'path': unit['path'], 'locator': unit['locator'],
                             'lines': [unit['line'], unit['end_line']]},
                'activation': resolve(unit, activation_entry(ensure_state(self.store.read()), unit['id']),
                                      self.now(), self.activation_policy)}

    def _coverage(self, data):
        """解析覆盖：不返回 files 明细（太长），其余原样。"""
        return {k: v for k, v in data['coverage'].items() if k != 'files'}

    def graph_view(self, focus=None, limit=120, mode=None):
        """图视图。

        mode='overview'：同一份已发布快照的全集（含 disabled 边，带 lifecycle）。
        mode='local' 或未传：沿用原有有界展开（保持旧调用行为）。
        新增字段只加不改；数量、图、候选都取自同一次 store.read()。
        另附 activation 映射（界面用来调透明度；不删任何节点）。
        """
        data = self.store.read()
        nodes = {n['id']: n for n in data['nodes']}
        total_nodes, total_edges = len(nodes), len(data['edges'])
        activation_state = ensure_state(data)
        now = self.now()
        if mode == 'overview':
            # 按 id 排序保证稳定；去重以防快照里出现重复（正常不会）
            picked_nodes = [nodes[i] for i in sorted(nodes)]
            unique_nodes = list({n['id']: n for n in picked_nodes}.values())
            picked_edges = [e for e in data['edges'] if e['from'] in nodes and e['to'] in nodes]
            unique_edges = list({e['id']: e for e in picked_edges}.values())
            return {'revision': data['revision'], 'mode': 'overview',
                    'total_nodes': total_nodes, 'total_edges': total_edges,
                    'returned_nodes': len(unique_nodes), 'returned_edges': len(unique_edges),
                    'partial': False, 'coverage': self._coverage(data),
                    'nodes': unique_nodes, 'edges': unique_edges,
                    'candidates': data['candidates'],
                    'activation': {'enabled': self.activation_policy['enabled'],
                                   'units': activation_map(data['units'], activation_state, now,
                                                           self.activation_policy)},
                    'candidate_meta': {'total': len(data['candidates']), 'generation_cap': 200,
                                       'note': '当前生成的候选；生成上限 200，不是全部潜在关系'}}

        limit = max(1, min(300, int(limit)))
        chosen = set([focus]) if focus in nodes else set(n['id'] for n in data['nodes'] if n['id'].startswith('entity:'))
        for _ in range(2):
            for edge in data['edges']:
                if len(chosen) >= limit:
                    break
                if edge['lifecycle'] == 'active' and (edge['from'] in chosen or edge['to'] in chosen):
                    chosen.update([edge['from'], edge['to']])
        ids = sorted(chosen)[:limit]
        picked_edges = [e for e in data['edges'] if e['from'] in ids and e['to'] in ids]
        return {'revision': data['revision'], 'mode': 'local',
                'total_nodes': total_nodes, 'total_edges': total_edges,
                'returned_nodes': len(ids), 'returned_edges': len(picked_edges),
                'nodes': [nodes[i] for i in ids], 'edges': picked_edges,
                'candidates': data['candidates'], 'partial': len(ids) < total_nodes,
                'activation': {'enabled': self.activation_policy['enabled'],
                               'units': activation_map([nodes[i] for i in ids if i.startswith('unit:')],
                                                       activation_state, now, self.activation_policy)},
                'coverage': self._coverage(data)}

    def candidate_detail(self, candidate_id):
        """候选的两端定位：标题/路径/小节/行号/版本状态。正文仍走 /v1/fetch 按版本读。

        传字符串 → 返回单个；传列表 → 返回 {'revision', 'items': [...]}（一次读快照，供分页用）。
        """
        batch = isinstance(candidate_id, (list, tuple))
        wanted = list(candidate_id) if batch else [candidate_id]
        data = self.store.read()
        by_id = {c['id']: c for c in data['candidates']}
        units = {u['id']: u for u in data['units']}
        nodes = {n['id']: n for n in data['nodes']}
        root = Path(self.config['memory_root'])

        def one(cid):
            candidate = by_id.get(cid)
            if not candidate:
                raise MemoryErrorCode('not_found')
            endpoints = []
            for ref in candidate['edge']['evidence_refs']:
                uid, pinned = ref['unit_id'], ref['source_version']
                unit = units.get(uid)
                # 四种状态分开：ok / stale（文件在、版本变了）/ missing（文件确实不在）/ unreadable（权限或其它读错）
                entry = {'unit_id': uid, 'source_version': pinned, 'status': 'missing',
                         'reason': 'unit_not_in_snapshot'}
                if unit:
                    entry.update({'label': unit['label'], 'kind': unit['kind'], 'path': unit['path'],
                                  'locator': unit['locator'], 'heading_path': unit.get('heading_path'),
                                  'lines': [unit['line'], unit['end_line']],
                                  'label_in_graph': (nodes.get(uid) or {}).get('label')})
                    try:
                        current = file_digest(root / unit['path'])
                    except FileNotFoundError:
                        entry.update(status='missing', reason='file_not_found')
                    except PermissionError:
                        entry.update(status='unreadable', reason='permission_denied')
                    except OSError as exc:
                        errno = getattr(exc, 'errno', None)
                        entry.update(status='unreadable',
                                     reason='oserror:%s' % (errno if errno is not None
                                                            else type(exc).__name__))
                    else:
                        if current == unit['source_version'] == pinned:
                            entry.update(status='ok', reason=None)
                        else:
                            entry.update(status='stale', reason='version_changed',
                                         current_version=current)
                endpoints.append(entry)
            ok = all(e['status'] == 'ok' for e in endpoints)
            return {'revision': data['revision'], 'candidate': candidate, 'endpoints': endpoints,
                    'can_accept': ok, 'claim': candidate.get('claim'),
                    'matched_terms': candidate.get('matched_terms'),
                    'shared_entity': candidate.get('shared_entity'), 'rule': candidate.get('rule')}

        if batch:
            return {'revision': data['revision'], 'items': [one(cid) for cid in wanted]}
        return one(candidate_id)

    def stale_sources(self, state=None):
        # 编辑前核对原件版本：外部改过就该先刷新，不能拿缓存里的旧版本发布。
        state = state or self.store.read()
        current = {u['id']: u['source_version'] for u in scan(self.config['memory_root'], self.profile)['units']}
        known = {u['id']: u for u in state['units']}
        stale = [u['path'] for uid, u in known.items() if current.get(uid) != u['source_version']]
        stale += [uid for uid in current if uid not in known]
        return sorted(set(stale))

    def edit(self, action, payload, expected_revision, key):
        # 拒绝只是记录判断，不引用来源正文：即使来源坏了也要能拒绝（用户判断不能丢）。
        if action != 'reject':
            stale = self.stale_sources()
            if stale:
                raise MemoryErrorCode('source_changed', 'refresh required before edit: ' + ', '.join(stale[:5]))
        def change(data):
            node_ids = {n['id'] for n in data['nodes']}
            units = {u['id']: u for u in data['units']}
            if action == 'connect':
                a, b = payload.get('from'), payload.get('to')
                if a not in node_ids or b not in node_ids or a == b:
                    raise MemoryErrorCode('invalid_endpoints')
                if payload.get('predicate', 'related_to') != 'related_to':
                    raise MemoryErrorCode('evidence_required', 'Framework manual edits support navigation only')
                refs = [{'unit_id': i, 'source_version': units[i]['source_version']} for i in [a, b] if i in units]
                for ref in refs:
                    fetch(self.config['memory_root'], units[ref['unit_id']], self.profile)
                edge = make_edge(a, 'related_to', b, refs, 'manual_navigation')
                edge['actor'] = 'local_authenticated_editor'
                data['manual_edges'][edge['id']] = edge
                data['disabled'] = [i for i in data['disabled'] if i != edge['id']]
                result = {'edge_id': edge['id']}
            elif action in ('disable', 'restore'):
                edge_id = payload.get('edge_id')
                known = any(e['id'] == edge_id for e in data['edges']) or edge_id in (data.get('disabled') or [])
                if not known:
                    raise MemoryErrorCode('not_found')
                if action == 'disable' and edge_id not in data['disabled']:
                    data['disabled'].append(edge_id)
                elif action == 'restore':
                    data['disabled'] = [i for i in data['disabled'] if i != edge_id]
                result = {'edge_id': edge_id}
            elif action in ('pin', 'unpin'):
                uid = payload.get('unit_id')
                if uid not in units:
                    raise MemoryErrorCode('not_found')
                activation = ensure_state(data)
                entry = activation['units'].get(uid)
                if entry is None:
                    entry = {'first_seen_at': self.now().isoformat().replace('+00:00', 'Z'),
                             'source_version': units[uid]['source_version'], 'pin': False,
                             'last_helpful_at': None, 'last_helpful_day': None, 'feedback_count': 0}
                    activation['units'][uid] = entry
                entry['pin'] = action == 'pin'
                result = {'unit_id': uid, 'pinned': entry['pin']}
            elif action in ('accept', 'reject'):
                candidate = next((c for c in data['candidates'] if c['id'] == payload.get('candidate_id')), None)
                if not candidate:
                    raise MemoryErrorCode('not_found')
                if action == 'accept':
                    # 接受要真的把两端正文读出来；读不到就是 409，不许拿投影顶替
                    for ref in candidate['edge']['evidence_refs']:
                        fetch(self.config['memory_root'], units[ref['unit_id']], self.profile)
                data['decisions'][candidate['id']] = 'accepted' if action == 'accept' else 'rejected'
                result = {'candidate_id': candidate['id']}
            else:
                raise MemoryErrorCode('not_supported')
            data['nodes'], data['edges'], data['candidates'], _assembly_report = assemble(self.config, data['units'], self.entities, data, self.profile)
            return result
        signature = action + ':' + digest(payload)
        return self.store.mutate(expected_revision, key, signature, change)

    def feedback(self, unit_id, expected_source_version, event_id, result,
                 expected_revision, key=None):
        """明确的使用反馈：只有 helpful 才更新活跃时间；不保存问句/聊天/模型正文。

        - event_id 绑定完整语义载荷（unit_id + source_version + result）：
          完全相同才视为重放（返回原结果、不写、不推进 revision）；同 ID 不同载荷 → idempotency_conflict；
        - 唯一事件检查在写锁内的 change() 里做，和“同单元同 UTC 日最多一次有效强化”是两件事；
        - 旧元数据没有语义指纹时保守拒绝，不猜成同一事件；
        - 不接受调用方传时间（用服务端时钟），因此不可能写入未来激活时间。
        """
        if result not in ('helpful', 'not_helpful'):
            raise MemoryErrorCode('invalid_feedback_result')
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise MemoryErrorCode('invalid_event_id')
        if not isinstance(expected_source_version, str) or not expected_source_version:
            raise MemoryErrorCode('invalid_source_version')
        data = self.store.read()
        unit = next((u for u in data['units'] if u['id'] == unit_id), None)
        if not unit:
            raise MemoryErrorCode('not_found')
        if expected_source_version != unit['source_version']:
            raise MemoryErrorCode('source_changed')
        try:
            current = file_digest(Path(self.config['memory_root']) / unit['path'])
        except OSError:
            raise MemoryErrorCode('not_found')
        if current != unit['source_version']:
            raise MemoryErrorCode('source_changed', 'source changed on disk; refresh required')

        now = self.now()
        stamp = now.isoformat().replace('+00:00', 'Z')
        day = now.date().isoformat()
        fingerprint = {'unit_id': unit_id, 'source_version': expected_source_version, 'result': result}

        # 只读快路径：已存在同一事件的完整重放，直接返回原结果（不写、不推进 revision）。
        # 这里用快照做一次核对；真正的唯一性判定仍在 change() 的写锁内再做一遍，防并发穿透。
        prior = ensure_state(data)['feedback'].get(event_id)
        if prior is not None:
            stored = {key: prior.get(key) for key in ('unit_id', 'source_version', 'result')}
            if not all(stored.values()):
                raise MemoryErrorCode('idempotency_conflict',
                                      'event_id exists without semantic fingerprint; refusing to reuse it')
            if stored != fingerprint:
                raise MemoryErrorCode('idempotency_conflict',
                                      'event_id already used with a different unit/version/result')
            entry = ensure_state(data)['units'].get(unit_id)
            summary = resolve(unit, entry, now, self.activation_policy)
            return {'unit_id': unit_id, 'result': prior.get('result'),
                    'effective': bool(prior.get('effective')), 'deduplicated': True,
                    'weight': summary['weight'],
                    'last_helpful_at': (entry or {}).get('last_helpful_at')}

        def change(state):
            state_activation = ensure_state(state)
            prior = state_activation['feedback'].get(event_id)
            if prior is not None:
                stored = {key: prior.get(key) for key in ('unit_id', 'source_version', 'result')}
                if not all(stored.values()):
                    # 旧版本写的记录没有语义指纹：不默认认定是同一事件
                    raise MemoryErrorCode('idempotency_conflict',
                                          'event_id exists without semantic fingerprint; refusing to reuse it')
                if stored != fingerprint:
                    raise MemoryErrorCode('idempotency_conflict',
                                          'event_id already used with a different unit/version/result')
                entry = state_activation['units'].get(unit_id)
                summary = resolve(unit, entry, now, self.activation_policy)
                raise _ReplayEvent({'unit_id': unit_id, 'result': prior.get('result'),
                                    'effective': bool(prior.get('effective')),
                                    'deduplicated': True, 'weight': summary['weight'],
                                    'last_helpful_at': (entry or {}).get('last_helpful_at')})
            entry = state_activation['units'].setdefault(unit_id, {
                'first_seen_at': stamp, 'source_version': unit['source_version'], 'pin': False,
                'last_helpful_at': None, 'last_helpful_day': None, 'feedback_count': 0})
            effective = False
            if result == 'helpful' and entry.get('last_helpful_day') != day:
                entry['last_helpful_at'] = stamp
                entry['last_helpful_day'] = day
                entry['feedback_count'] = int(entry.get('feedback_count') or 0) + 1
                effective = True
            state_activation['feedback'][event_id] = {
                'unit_id': unit_id, 'source_version': expected_source_version,
                'result': result, 'at': stamp, 'effective': effective}
            summary = resolve(unit, entry, now, self.activation_policy)
            return {'unit_id': unit_id, 'result': result, 'effective': effective,
                    'deduplicated': False, 'weight': summary['weight'],
                    'last_helpful_at': entry.get('last_helpful_at'),
                    'feedback_count': entry.get('feedback_count', 0)}

        signature = 'feedback:' + unit_id + ':' + event_id + ':' + result
        # 事件幂等由 change() 在写锁内判定；store 层的 key 只做传输级去重，
        # 不能拿 event_id 当 key（否则 store 会抢在语义核对之前返回旧结果）。
        transport_key = (key + ':' + digest(fingerprint)) if key else uuid.uuid4().hex
        try:
            return self.store.mutate(expected_revision, transport_key, signature, change)
        except _ReplayEvent as replay:
            return replay.payload

    def query(self, text, max_items=3, context=None):
        # context: {"focus_terms": [...]} —— 上一层传下来的会话焦点（可为空）
        if not isinstance(text, str) or not 1 <= len(text) <= 12000:
            raise MemoryErrorCode('invalid_query')
        data = self.store.read()
        qid = uuid.uuid4().hex
        result = {'schema_version': '1.0', 'query_id': qid, 'graph_revision': data['revision'],
                  'status': 'no_match', 'items': [], 'missing': [], 'conflicts': [],
                  'retrieval_mode': 'graph', 'coverage': {'partial': bool(data['coverage'].get('errors'))},
                  'budget': {'expanded_edges': 0, 'exhausted': False}, 'warnings': []}
        if not data['revision']:
            result['status'] = 'unavailable'
            result['missing'] = ['graph_not_built']
            return result

        norm = normalize(text, self.profile)
        focus_terms = (context or {}).get("focus_terms") or []
        branch = route(norm, has_focus=bool(focus_terms))
        intent = ('procedure' if norm['procedure'] else ('history' if norm['history'] else
                  ('current_fact' if norm['fact'] else 'evidence_lookup')))
        result['intent'] = intent

        # ① 目录类：沿用 navigation 状态，不参与打分
        if branch == 'navigation':
            # 导航要带计数与分类，不能只回一个文件名
            hints = self.profile.get('hints') or {}
            want_threads = bool(re.search(hints.get('threads', '待办|悬着|没做完|进展'), text))
            nav_cfg = self.profile.get('navigation') or {}
            target = (nav_cfg.get('threads_file', 'open-threads.md') if want_threads
                      else nav_cfg.get('promises_file', 'promises.md'))
            kind = 'threads' if want_threads else 'promises'
            lines = []
            try:
                lines = (Path(self.config['memory_root']) / target).read_text(encoding='utf-8').splitlines()
            except OSError:
                lines = []
            rows = [ln for ln in lines if ln.strip().startswith('|') and not re.match(r'^\|[\s|:-]+\|$', ln.strip())]
            count = max(len(rows) - 1, 0)
            result['status'] = 'navigation'
            result['retrieval_mode'] = 'navigation'
            result['navigation'] = {'kind': kind, 'target': target, 'count': count}
            result['items'] = [{'memory_id': 'navigation:' + kind, 'source_version': None,
                                'source_kind': 'navigation', 'authority_kind': 'index',
                                'text': '', 'conditions': [], 'temporal_scope': {}, 'maturity': None,
                                'citation': {'path': target, 'locator': kind, 'lines': [1, 1]},
                                'path': {'node_ids': [], 'edges': []}}]
            self.traces[qid] = {'query_id': qid, 'branch': branch, 'target': target, 'kind': kind,
                                'count': count, 'reason': 'category_request', 'paths': [target],
                                'budget': result['budget']}
            return result

        # ①' 有焦点：把上一层传来的焦点词并入本次查询词（焦点分支是可达的）
        if branch == 'focus':
            merged = set(norm['words'])
            for term in focus_terms:
                merged |= tokens(str(term))
            norm = dict(norm)
            norm['words'] = merged
            norm['focus'] = [str(t) for t in focus_terms]

        # ② 只有指代、又没有焦点：明确告诉上层「缺上下文」，不猜
        if branch == 'ambiguous':
            result['status'] = 'ambiguous'
            result['missing'] = ['no_context']
            self.traces[qid] = {'query_id': qid, 'branch': branch, 'reason': 'reference_without_focus',
                                'paths': [], 'budget': result['budget']}
            return result

        ranked = rank(data['units'], norm, self.profile)
        # 走图：从对象实体（没有对象就取词面前三）出发做有界 BFS，记录经过的边。
        adjacency = build_adjacency(data['edges'])
        seeds = ['entity:' + oid for oid in norm['objects'] if ('entity:' + oid) in adjacency]
        if not seeds:
            seeds = [u['id'] for _, u, _ in ranked[:3]]
        by_units = {u['id']: u for u in data['units']}
        paths, expanded = graph_paths(adjacency, seeds, max_hops=2)
        # 图上发现的资料也要能进候选：只在「有对象」时启用，
        # 顺着对象实体走到的单元即使没有词面命中，也算候选（证据来自路径而不是词面）。
        object_seeded = bool(norm['objects']) and any(s.startswith('entity:') for s in seeds)
        graph_only = set()
        extra = []
        if object_seeded:
            scored_ids = {u['id'] for _, u, _ in ranked}
            for uid, (nodes, edge_ids) in paths.items():
                if uid in scored_ids or uid not in by_units:
                    continue
                hops = max(len(edge_ids), 1)
                extra.append((self.profile.scoring().get('graph_only_base', 3.5) / hops, by_units[uid], []))
                graph_only.add(uid)
        if extra:
            ranked = sorted(ranked + extra, key=lambda x: -x[0])
        ranked = sorted([((score * 1.5 if u['id'] in paths else score), u, m) for score, u, m in ranked],
                        key=lambda x: -x[0])
        # 小节级登记：历史问法命中登记对象时，优先到登记过的小节找证据
        rules, reg_entities = load_roles(self.config.get('roles_file', ''))
        wanted_ids = {oid for oid in norm['objects'] if oid in reg_entities}
        if norm['history'] and wanted_ids:
            reg_hits = registered_units(data['units'], rules, wanted_ids)
            # 登记过的历史小节不看词面分数：它被登记就说明「问这件事该到这儿看」
            base = ranked[0][0] if ranked else 5.0
            bumped = []
            for score, unit, matched in ranked:
                bumped.append((base * 1.25 if unit['id'] in reg_hits else score, unit, matched))
            by_uid = {u['id']: u for u in data['units']}
            have = {u['id'] for _, u, _ in bumped}
            for uid in reg_hits - have:
                bumped.append((base * 1.2, by_uid[uid], []))
            ranked = sorted(bumped, key=lambda x: -x[0])
        # 明确单元 / 来源路径定位：即使没有词面命中，也要能把这条材料找回来
        # （它仍然会走后面的来源版本核验；这里只是让它进入候选集合）
        lowered_text = text.lower()
        explicit_ids = {u['id'] for u in data['units']
                        if u['id'].lower() in lowered_text
                        or (u.get('path') and len(u['path']) > 8 and u['path'].lower() in lowered_text)}
        if explicit_ids:
            have = {u['id'] for _, u, _ in ranked}
            fill = max([s for s, _, _ in ranked] or [1.0])
            for u in data['units']:
                if u['id'] in explicit_ids and u['id'] not in have:
                    ranked.append((fill, u, []))
                    have.add(u['id'])
            ranked = sorted(ranked, key=lambda x: -x[0])
        df = {}
        usable = [u for u in data['units'] if u['kind'] != 'source_metadata']
        for u in usable:
            for w in (norm['words'] & tokens(u['search_text'])):
                df[w] = df.get(w, 0) + 1

        base_status, chosen, rivals = classify(ranked, norm, df, len(usable), graph_ok=(graph_only | explicit_ids), allow_common=(branch == 'focus' or bool(norm['objects'])), profile=self.profile)
        result['budget']['expanded_edges'] = expanded
        # 衰减层：只在同一相关度层级内重排；状态与 rivals 仍用衰减前的结果——
        # 年龄不许把问题变成 no_match，也不许拿来消解歧义。
        ordered, activation_explain = order_with_activation(
            ranked, by_units, data, self.now(), self.activation_policy, norm, text)
        result['activation'] = activation_explain

        # ── 统一口径：先选定最终输出集合，再对**这个集合**核验、定型、返回 ──
        if base_status == 'no_match':
            picked = []
        else:
            picked = [row[1] for row in ordered[:max(1, min(5, max_items))]]
        # ── 同一对象的多来源合并（在核验之前并入 picked，走同一套核验与定型）──
        nav_cfg = self.profile.get('navigation') or {}
        threads_file = nav_cfg.get('threads_file', 'open-threads.md')
        if picked and norm['objects'] and not any(i['path'] == threads_file for i in picked):
            alias_words = [w for oid, pat, words in self.profile.objects()
                           if oid in norm['objects'] for w in words]
            thread_path = Path(self.config['memory_root']) / threads_file
            try:
                thread_lines = thread_path.read_text(encoding='utf-8').splitlines()
                thread_version = file_digest(thread_path)
            except OSError:
                thread_lines, thread_version = [], None
            for idx, line in enumerate(thread_lines, start=1):
                if not line.strip().startswith('|'):
                    continue
                cells = [c.strip() for c in line.strip().strip('|').split('|')]
                name_cell = cells[0] if cells else ''
                # 只匹配事项名那一列，避免正文里偶然出现同一个词就当成同一件事
                if any(w in name_cell for w in alias_words):
                    picked.append({
                        'id': 'thread:%s:%d' % (threads_file, idx),
                        'path': threads_file, 'line': idx, 'end_line': idx,
                        'locator': 'thread_row', 'kind': 'thread_row',
                        'authority_kind': 'task_list', 'body': line.strip(),
                        'search_text': line.strip(), 'source_version': thread_version})
                    break

        # ① 核验这个集合里的每一项
        stale = []
        for unit in picked:
            try:
                current = file_digest(Path(self.config['memory_root']) / unit['path'])
            except OSError:
                current = None
            if current != unit['source_version']:
                stale.append(unit['path'])

        # ② 证据充分性也基于同一集合的首项
        codes = needs_more_evidence(norm, picked[0], self.profile) if picked else []
        # 目标字段：只要这一批里有一条落在该字段上，就不算缺（首选可能是"备注"行）
        if "target_field_not_matched" in codes and norm.get("target"):
            pat = self.profile.target_locator(norm["target"])
            if pat and any(re.search(pat, (u.get("locator") or "")) for u in picked):
                codes = [c for c in codes if c != "target_field_not_matched"]

        # ③ 汇总状态：核验失败 > 缺证据 > 候选不确定 > 正常
        if not picked:
            status = 'no_match'
        elif stale or codes:
            status = 'insufficient'
        else:
            status = base_status
        if stale:
            result['warnings'].append('source_changed_or_missing: refresh required')
        result['missing'] = [c for c in (['source_changed'] if stale else []) + codes
                             if c not in result['missing']]
        result['status'] = status

        # ④ 返回的就是刚才核验的那一批
        for unit in picked:
            node_ids, edge_ids = paths.get(unit['id'], ([unit['id']], []))
            act_summary = resolve(unit, activation_entry(ensure_state(data), unit['id']),
                                  self.now(), self.activation_policy)
            act_bypass = bypass_reason(norm, text, unit)
            if act_bypass:
                act_summary['weight'] = 1.0
            act_summary['bypass'] = act_bypass
            result['items'].append({
                'memory_id': unit['id'], 'source_version': unit['source_version'],
                'source_kind': unit['kind'], 'authority_kind': unit['authority_kind'],
                'text': unit['body'], 'conditions': [], 'temporal_scope': {}, 'maturity': None,
                'citation': {'path': unit['path'], 'locator': unit['locator'],
                             'lines': [unit['line'], unit['end_line']]},
                'path': {'node_ids': node_ids, 'edges': edge_ids},
                'activation': act_summary})
        # 只有真的经过边才算图检索；否则老实标成词面兜底
        result['retrieval_mode'] = 'graph' if any(i['path']['edges'] for i in result['items']) else 'lexical_fallback'
        if rivals:
            result['conflicts'] = [{'code': 'ambiguous_candidates',
                                    'candidates': [{'path': u['path'], 'locator': u['locator']} for u in rivals[:4]]}]
        if not result['items'] and result['status'] != 'no_match':
            result['status'] = 'no_match'
        if status == 'insufficient' and 'target_field_not_matched' not in result['missing']:
            pat = None
            if norm['target']:
                pat = self.profile.target_locator(norm['target'])
            if pat and picked and not any(re.search(pat, (u.get('locator') or '')) for u in picked):
                result['missing'].append('target_field_not_matched')
        trace = {'query_id': qid, 'branch': branch, 'intent': intent,
                 'normalized': {'objects': norm['objects'], 'target': norm['target'],
                 'device': norm['device'], 'history': norm['history']},
                 'candidates': len(ranked), 'top_score': round(ranked[0][0], 2) if ranked else 0,
                 'expanded_edges': expanded, 'retrieval_mode': result['retrieval_mode'],
                 'paths': [i['path'] for i in result['items']], 'rivals': len(rivals),
                 'status': result['status'], 'budget': result['budget']}
        self.traces[qid] = trace
        while len(self.traces) > 100:
            self.traces.popitem(last=False)
        return result
