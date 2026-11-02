import itertools
import re

from .common import digest, stable_id


def alias_matches(text, alias):
    if re.fullmatch(r'[\x00-\x7f]+', alias):
        return bool(re.search(r'(?<![a-z0-9_-])' + re.escape(alias) + r'(?![a-z0-9_-])', text, re.I))
    return alias.lower() in text.lower()


def tokens(text):
    result = set(re.findall(r'[a-z][a-z0-9_-]{2,}', text.lower()))
    clean = re.sub('什么|怎么|到了没|现在|今天|那个|一下|我们|然后|这个|可以|需要|之前', ' ', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', clean):
        if len(run) <= 8:
            result.add(run)
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    return result


def make_edge(a, predicate, b, evidence, kind='structural'):
    return {'id': stable_id('edge', a + '/' + predicate + '/' + b),
            'from': a, 'predicate': predicate, 'to': b, 'evidence_refs': evidence,
            'assertion_kind': kind, 'review_state': 'accepted', 'lifecycle': 'active'}


def compile_graph(units, entities, old, profile=None):
    if profile is None:
        from .profile import DEFAULT as profile
    cand_cfg = profile.get("candidate") or {}
    stop_words = set(cand_cfg.get("stop_words") or [])
    max_candidates = int(cand_cfg.get("max_candidates", 200))
    per_object = int(cand_cfg.get("max_units_per_object", 60))
    rule_name = cand_cfg.get("rule", "shared-object-and-title/v1")
    nodes = {}
    edges = {}
    members = {}
    by_id = {u['id']: u for u in units}
    def node(identity, label, kind):
        nodes[identity] = {'id': identity, 'label': label, 'kind': kind}
    for entity in entities:
        node('entity:' + entity['id'], entity['name'], entity.get('kind', 'object'))
    for unit in units:
        uid, did = unit['id'], unit['document_id']
        node(uid, unit['label'], unit['kind'])
        nodes[uid]['source_version'] = unit['source_version']
        node(did, unit['path'], 'document')
        evidence = [{'unit_id': uid, 'source_version': unit['source_version']}]
        edge = make_edge(uid, 'located_in', did, evidence)
        edges[edge['id']] = edge
        for entity in entities:
            if any(alias_matches(unit['search_text'], a) for a in entity['aliases']):
                eid = 'entity:' + entity['id']
                edge = make_edge(uid, 'mentions', eid, evidence)
                edges[edge['id']] = edge
                members.setdefault(eid, []).append(unit)
    candidates = {}
    # Bounded object-scoped generation; never equate co-occurrence with the same event.
    for eid, group in members.items():
        eligible = sorted((u for u in group if u['kind'] == 'entry'), key=lambda u: u['path'])
        for a, b in itertools.combinations(eligible[:per_object], 2):
            if a['path'] == b['path']:
                continue
            overlap = tokens(a['label']) & tokens(b['label']) - stop_words
            if not overlap:
                continue
            pair = sorted([a['id'], b['id']])
            versions = sorted([a['source_version'], b['source_version']])
            cid = stable_id('candidate', '/'.join(pair + versions + ['shared-object-v1']))
            edge = make_edge(pair[0], 'related_to', pair[1], [
                {'unit_id': a['id'], 'source_version': a['source_version']},
                {'unit_id': b['id'], 'source_version': b['source_version']}], 'semantic')
            decision = old['decisions'].get(cid, 'candidate')
            candidates[cid] = {'id': cid, 'edge': edge, 'state': decision,
                               'rule': rule_name, 'shared_entity': eid,
                               'matched_terms': sorted(overlap), 'claim': '可能同主题，不代表同一事件'}
            if decision == 'accepted':
                edges[edge['id']] = edge
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break
    for edge in old['manual_edges'].values():
        if edge['from'] not in nodes or edge['to'] not in nodes:
            continue
        if all(ref['unit_id'] in by_id and by_id[ref['unit_id']]['source_version'] == ref['source_version']
               for ref in edge['evidence_refs']):
            edges[edge['id']] = edge
    for edge in edges.values():
        if edge['id'] in old['disabled']:
            edge['lifecycle'] = 'disabled'
    return list(nodes.values()), list(edges.values()), list(candidates.values())
