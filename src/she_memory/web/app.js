'use strict';
// She 记忆图谱：全库总览 / 主题局部 / 候选审核
// 约定：接口字段只读不改；所有数量来自同一次 /v1/graph/view 响应。

const $ = id => document.getElementById(id);
const SVGNS = 'http://www.w3.org/2000/svg';
const ENTITY_KINDS = new Set(['entity', 'object', 'project', 'tool', 'topic', 'matter', 'relation_group']);
const LABEL_POOL = 420;
const LOCAL_LIMIT = 300;
const CAND_SIZE = 10;

const S = {
  token: '', connected: false,
  graph: {nodes: [], edges: [], candidates: [], revision: 0, mode: 'overview',
          total_nodes: 0, total_edges: 0, partial: true, coverage: {}, candidate_meta: null},
  nodeById: new Map(), order: [], neighbors: new Map(),
  view: {k: 1, x: 0, y: 0},
  selected: null, hovered: null, selectedEdge: null, highlighted: new Set(),
  filter: {hidden: new Set()},
  layout: {revision: 0, positions: {}},
  positions: {}, nodeEls: new Map(), edgeEls: [], edgeByNode: new Map(),
  labels: [], labelIdx: 0,
  cand: {filter: 'candidate', page: 0, details: new Map(), open: new Set()},
  focus: null, from: null, loadToken: 0,
};

// ── 小工具 ──────────────────────────────────────────────────
function element(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
function notice(text) { $('notice').textContent = text; }
function canvasSize() { const r = $('canvas').getBoundingClientRect(); return {w: r.width, h: r.height}; }

async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'Authorization': `Bearer ${S.token}`, ...(data === undefined ? {} : {'Content-Type': 'application/json'})},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error?.code || `HTTP ${response.status}`);
  return result;
}
function guarded(fn) {
  return async (...args) => {
    try { await fn(...args); }
    catch (error) { notice(`未完成：${error.message}${error.message === 'revision_conflict' ? '（版本冲突，请重新读取）' : ''}`); }
  };
}
const STATUS_TEXT = {candidate: '待审', accepted: '已接受', rejected: '已拒绝'};
const ERROR_TEXT = {
  not_found: '来源单元不存在', source_changed: '来源已变，需要重新读取', stale: '来源已变（文件版本变了）',
  missing: '来源缺失（文件不在了）', unreadable: '来源读不了（权限或读取错误）',
  revision_conflict: '版本冲突', unauthorized: '连接码不对', forbidden_host: '来源校验失败',
};

// ── 视图变换 ────────────────────────────────────────────────
function applyView() {
  $('viewport').setAttribute('transform', `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
  const hud = $('zoom-hud');
  if (hud) hud.textContent = `${Math.round(S.view.k * 100)}%`;
  updateLabels();
}
function screenToWorld(clientX, clientY) {
  const r = $('canvas').getBoundingClientRect();
  return {x: (clientX - r.left - S.view.x) / S.view.k, y: (clientY - r.top - S.view.y) / S.view.k};
}
function zoomAt(clientX, clientY, factor) {
  const r = $('canvas').getBoundingClientRect();
  const px = clientX - r.left, py = clientY - r.top;
  const k2 = Math.min(6, Math.max(0.05, S.view.k * factor));
  const ratio = k2 / S.view.k;
  S.view.x = px - (px - S.view.x) * ratio;
  S.view.y = py - (py - S.view.y) * ratio;
  S.view.k = k2;
  applyView();
}
function fitView() {
  const ids = [...S.nodeById.keys()].filter(id => !S.filter.hidden.has(S.nodeById.get(id).kind));
  if (!ids.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const id of ids) {
    const p = S.positions[id]; if (!p) continue;
    minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
  }
  if (!Number.isFinite(minX)) return;
  const {w, h} = canvasSize();
  const bw = Math.max(maxX - minX, 40), bh = Math.max(maxY - minY, 40);
  const k = Math.min((w - 60) / bw, (h - 60) / bh);
  S.view.k = Math.min(3, Math.max(0.05, k));
  S.view.x = w / 2 - (minX + maxX) / 2 * S.view.k;
  S.view.y = h / 2 - (minY + maxY) / 2 * S.view.k;
  applyView();
}
function centerOn(id, factor = 1) {
  const p = S.positions[id]; if (!p) return;
  const {w, h} = canvasSize();
  if (factor) S.view.k = factor;
  S.view.x = w / 2 - p.x * S.view.k;
  S.view.y = h / 2 - p.y * S.view.k;
  applyView();
}

// ── 布局：世界坐标、按 view_mode/focus 隔离 ──────────────────
function scopeKey() { return S.graph.mode === 'overview' ? 'overview' : 'local:' + (S.focus || '*'); }
function savedFor(id) {
  const key = scopeKey() + '|' + id;
  if (S.layout.positions[key]) return S.layout.positions[key];
  // 旧布局（改动前的裸 id）只对局部视图生效，读得到、但不改写它
  if (S.graph.mode === 'local' && S.layout.positions[id]) return S.layout.positions[id];
  return null;
}
function seedPosition(idx) {
  const a = idx * 2.399963229728653;          // 黄金角，面积均匀
  const r = 46 + Math.sqrt(idx) * 52;
  return {x: Math.cos(a) * r, y: Math.sin(a) * r};
}
function buildPositions() {
  S.positions = {};
  S.order.forEach((node, i) => { S.positions[node.id] = savedFor(node.id) || seedPosition(i); });
}
function saveLayout() {
  const prefix = scopeKey() + '|';
  const merged = {...S.layout.positions};
  for (const [id, p] of Object.entries(S.positions)) {
    merged[prefix + id] = {x: Math.round(p.x * 10) / 10, y: Math.round(p.y * 10) / 10};
  }
  return guarded(async () => {
    const saved = await api('/v1/views/default', {expected_view_revision: S.layout.revision, positions: merged});
    S.layout = saved;
    notice(`布局已保存（${scopeKey()} · ${Object.keys(S.positions).length} 个位置）；语义图版本未改变。`);
  })();
}

// ── 渲染 ────────────────────────────────────────────────────
function buildIndexes() {
  S.nodeById = new Map(S.graph.nodes.map(n => [n.id, n]));
  S.neighbors = new Map();
  S.graph.edges.forEach((e, i) => {
    if (!S.neighbors.has(e.from)) S.neighbors.set(e.from, []);
    if (!S.neighbors.has(e.to)) S.neighbors.set(e.to, []);
    S.neighbors.get(e.from).push(i);
    S.neighbors.get(e.to).push(i);
  });
  const rank = n => (n.id.startsWith('entity:') || ENTITY_KINDS.has(n.kind)) ? 0 : (n.kind === 'document' ? 1 : 2);
  S.order = S.graph.nodes.slice().sort((a, b) => rank(a) - rank(b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

function renderGraph() {
  const edgesFrag = document.createDocumentFragment();
  const nodesFrag = document.createDocumentFragment();
  S.edgeEls = []; S.nodeEls = new Map();

  for (const edge of S.graph.edges) {
    const a = S.positions[edge.from], b = S.positions[edge.to];
    if (!a || !b) continue;                       // 端点缺失的边不画（接口层已保证没有）
    const line = element('line', {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: `edge ${edge.lifecycle === 'disabled' ? 'disabled' : ''} ${S.highlighted.has(edge.id) ? 'highlight' : ''} ${S.selectedEdge === edge.id ? 'selected' : ''}`,
    });
    const title = element('title', {});
    title.textContent = `${edge.predicate} · ${edge.assertion_kind}${edge.lifecycle === 'disabled' ? ' · 已停用（不参与检索遍历）' : ''}`;
    line.append(title);
    line.addEventListener('click', ev => {
      ev.stopPropagation();
      S.selectedEdge = edge.id; S.selected = null;
      $('detail-title').textContent = '关联：' + edge.predicate;
      $('detail-meta').textContent = `${edge.lifecycle} · evidence ${edge.evidence_refs.length} 条 · id ${edge.id.slice(0, 18)}…`;
      $('detail-body').textContent = JSON.stringify(edge, null, 2);
      drawEdgeState();
    });
    edgesFrag.append(line);
    S.edgeEls.push({el: line, from: edge.from, to: edge.to, index: S.edgeEls.length});
  }

  for (const node of S.graph.nodes) {
    const p = S.positions[node.id];
    const isEntity = node.id.startsWith('entity:') || ENTITY_KINDS.has(node.kind);
    const g = element('g', {
      transform: `translate(${p.x},${p.y})`,
      class: `node ${isEntity ? 'entity' : ''} ${node.kind === 'document' ? 'document' : ''} ${S.selected === node.id ? 'selected' : ''}`,
      'data-id': node.id,
    });
    g.append(element('circle', {r: isEntity ? 16 : (node.kind === 'document' ? 11 : 7)}));
    const title = element('title', {});
    title.textContent = fullLabel(node);
    g.append(title);
    const act = (S.activationUnits || {})[node.id];
    if (act) {
      g.dataset.weight = String(act.weight);
      if (act.pinned) g.classList.add('pinned');
      if (act.protected) g.classList.add('protected');
    }

    let dragging = false, moved = false, start;
    g.addEventListener('pointerdown', ev => {
      ev.stopPropagation();
      dragging = true; moved = false; start = {x: ev.clientX, y: ev.clientY};
      g.setPointerCapture(ev.pointerId);
    });
    g.addEventListener('pointermove', ev => {
      if (!dragging) return;
      if (Math.abs(ev.clientX - start.x) + Math.abs(ev.clientY - start.y) > 3) moved = true;
      if (!moved) return;
      const w = screenToWorld(ev.clientX, ev.clientY);
      S.positions[node.id] = {x: Math.max(-9500, Math.min(9500, w.x)), y: Math.max(-9500, Math.min(9500, w.y))};
      g.setAttribute('transform', `translate(${S.positions[node.id].x},${S.positions[node.id].y})`);
      syncEdges(node.id);
      updateLabels();
    });
    g.addEventListener('pointerup', ev => {
      const wasMoved = moved;
      dragging = false; moved = false;
      try { g.releasePointerCapture(ev.pointerId); } catch (e) { /* 已释放 */ }
      if (!wasMoved) selectNode(node.id).catch(err => notice('未完成：' + err.message));
      else notice('位置已调整（世界坐标，不再被 1000×720 限制）。点“保存布局”保留。');
    });
    g.addEventListener('mouseenter', () => { S.hovered = node.id; updateLabels(); });
    g.addEventListener('mouseleave', () => { if (S.hovered === node.id) { S.hovered = null; updateLabels(); } });
    nodesFrag.append(g);
    S.nodeEls.set(node.id, g);
  }

  $('edges').replaceChildren(edgesFrag);
  $('nodes').replaceChildren(nodesFrag);
  applyFilter();
  updateLabels();
}

function fullLabel(node) {
  return node.label || node.id;
}
function syncEdges(nodeId) {
  const idxs = S.neighbors.get(nodeId) || [];
  for (const i of idxs) {
    const item = S.edgeEls[i]; if (!item) continue;
    const a = S.positions[item.from], b = S.positions[item.to];
    if (!a || !b) continue;
    item.el.setAttribute('x1', a.x); item.el.setAttribute('y1', a.y);
    item.el.setAttribute('x2', b.x); item.el.setAttribute('y2', b.y);
  }
}
function drawEdgeState() {
  S.edgeEls.forEach(item => {
    const edge = S.graph.edges[item.index];
    item.el.classList.toggle('highlight', S.highlighted.has(edge.id));
    item.el.classList.toggle('selected', S.selectedEdge === edge.id);
  });
}

// ── 标签分层（按缩放；缩小不铺满） ──────────────────────────
function ensureLabelPool() {
  if (S.labels.length) return;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < LABEL_POOL; i++) {
    const t = element('text', {class: 'lbl', x: 12, y: 4, 'font-size': 11});
    t.setAttribute('visibility', 'hidden');
    frag.append(t);
    S.labels.push(t);
  }
  $('labels').append(frag);
}
function updateLabels() {
  ensureLabelPool();
  const k = S.view.k;
  const {w, h} = canvasSize();
  const minX = (-S.view.x) / k - 60, maxX = (w - S.view.x) / k + 60;
  const minY = (-S.view.y) / k - 40, maxY = (h - S.view.y) / k + 40;
  const picks = [];
  const hidden = S.filter.hidden;
  const push = (node, prio) => {
    if (!node || hidden.has(node.kind)) return;
    const p = S.positions[node.id]; if (!p) return;
    picks.push({node, prio});
  };
  const sel = S.selected && S.nodeById.get(S.selected);
  if (sel) push(sel, 100);
  const hov = S.hovered && S.nodeById.get(S.hovered);
  if (hov) push(hov, 95);
  for (const node of S.graph.nodes) {
    const isEntity = node.id.startsWith('entity:') || ENTITY_KINDS.has(node.kind);
    if (isEntity) push(node, 80);
    else if (node.kind === 'document' && k >= 0.5) {
      const p = S.positions[node.id];
      if (p && p.x >= minX && p.x <= maxX && p.y >= minY && p.y <= maxY) push(node, 50);
    } else if (k >= 1.5) {
      const p = S.positions[node.id];
      if (p && p.x >= minX && p.x <= maxX && p.y >= minY && p.y <= maxY) push(node, 20);
    }
  }
  // 选中/悬停的邻居也露出来（中等倍率下有用）
  if (k >= 0.9 && S.selected) {
    for (const i of (S.neighbors.get(S.selected) || []).slice(0, 40)) {
      const edge = S.graph.edges[i];
      push(S.nodeById.get(edge.from === S.selected ? edge.to : edge.from), 60);
    }
  }
  picks.sort((a, b) => b.prio - a.prio);
  const seen = new Set();
  let used = 0;
  for (const item of picks) {
    if (used >= S.labels.length) break;
    if (seen.has(item.node.id)) continue;
    seen.add(item.node.id);
    const el = S.labels[used++];
    const p = S.positions[item.node.id];
    el.setAttribute('transform', `translate(${p.x},${p.y}) scale(${1 / k})`);
    el.textContent = fullLabel(item.node);
    el.setAttribute('visibility', 'visible');
    el.classList.toggle('entity-label', item.node.id.startsWith('entity:') || ENTITY_KINDS.has(item.node.kind));
  }
  for (let i = used; i < S.labels.length; i++) S.labels[i].setAttribute('visibility', 'hidden');
}

// ── 选择与详情 ──────────────────────────────────────────────
async function selectNode(id) {
  S.selected = id; S.selectedEdge = null;
  const node = S.nodeById.get(id);
  if (!node) return;
  for (const [nid, el] of S.nodeEls) el.classList.toggle('selected', nid === id);
  drawEdgeState();
  updateLabels();
  $('detail-title').textContent = fullLabel(node);
  const kindText = UNIT_KIND_TEXT[node.kind] || node.kind;
  $('detail-meta').textContent = `${kindText} · ${id}`;
  if (node.source_version) {
    try {
      const unit = await api('/v1/fetch', {unit_id: id, expected_source_version: node.source_version});
      $('detail-meta').textContent = `${unit.citation?.path || ''}:${unit.citation?.lines?.[0] || ''} · ${unit.source_kind || node.kind} · v${String(unit.source_version).slice(0, 12)}`;
      $('detail-body').textContent = unit.text || '';
      renderActivationPanel(unit);
    } catch (error) {
      $('detail-body').textContent = `读取失败：${ERROR_TEXT[error.message] || error.message}。这不是原文——请重新读取来源后再看。`;
      renderActivationPanel(null);
    }
  } else {
    $('detail-body').textContent = `${kindText}节点：不是来源单元，没有正文。相邻的来源单元才有原文；可按类型 id（${id}）在检索里引用它。`;
    renderActivationPanel(null);
  }
}

// ── 活跃度 / 保护 / 反馈 ────────────────────────────────────
function applyActivation() {
  const on = $('activation-toggle') && $('activation-toggle').checked;
  for (const [id, el] of S.nodeEls) {
    const act = (S.activationUnits || {})[id];
    if (!on || !act) { el.style.opacity = ''; continue; }
    // 只调透明度，不删节点；最低 0.45，保证仍然点得到
    const weight = typeof act.weight === 'number' ? act.weight : 1;
    el.style.opacity = String(Math.max(0.45, Math.min(1, weight)));
  }
  const pinned = Object.values(S.activationUnits || {}).filter(a => a.pinned).length;
  notice(on
    ? `活跃程度已开：普通经历按权重淡出（最低 0.45，仍可点开）；受保护 ${pinned} 个。`
    : '活跃程度已关：全部节点按原样显示（图仍是全量，没有删除）。');
}

function activationLine(act) {
  if (!act || !act.time_basis) return '活跃度：未记录';
  const bits = [`权重 ${Number(act.weight ?? 1).toFixed(3)}`, `依据 ${act.time_basis}`];
  if (act.age_days !== null && act.age_days !== undefined) bits.push(`约 ${Math.round(act.age_days)} 天`);
  if (act.pinned) bits.push('已保护（pin）');
  else if (act.protected) bits.push('默认受保护');
  if (act.last_helpful_at) bits.push(`最近有帮助 ${act.last_helpful_at}`);
  if (act.time_note) bits.push(`注：${act.time_note}`);
  return bits.join(' · ');
}

function renderActivationPanel(unit) {
  const panel = $('activation-panel');
  panel.replaceChildren();
  panel.classList.remove('hint');
  if (!unit || !unit.activation) { panel.classList.add('hint'); panel.textContent = '这个节点没有活跃度元数据（不是来源单元）。'; return; }
  const act = unit.activation;
  const text = document.createElement('div');
  text.textContent = activationLine(act);
  panel.append(text);
  const row = document.createElement('div');
  row.className = 'activation-actions';
  const pinButton = document.createElement('button');
  pinButton.className = 'secondary';
  pinButton.id = 'activation-pin';
  pinButton.textContent = act.pinned ? '取消保护' : '保护这条记录';
  pinButton.onclick = () => setPin(unit, !act.pinned);
  const help = document.createElement('button');
  help.className = 'secondary'; help.id = 'activation-helpful'; help.textContent = '有帮助';
  help.onclick = () => sendFeedback(unit, 'helpful');
  const nohelp = document.createElement('button');
  nohelp.className = 'secondary'; nohelp.id = 'activation-not-helpful'; nohelp.textContent = '没帮助';
  nohelp.onclick = () => sendFeedback(unit, 'not_helpful');
  row.append(pinButton, help, nohelp);
  panel.append(row);
}

async function setPin(unit, pinned) {
  try {
    await api('/v1/graph/edit', {
      action: pinned ? 'pin' : 'unpin',
      payload: {unit_id: unit.memory_id},
      expected_revision: S.graph.revision,
      idempotency_key: crypto.randomUUID(),
    });
    await loadGraph({keepView: true});
    notice(pinned ? '已保护：这条记录不再随时间衰减。' : '已取消保护：按它原来的时间轨迹计算，不伪造新生日。');
  } catch (error) {
    if (error.message === 'revision_conflict') {
      notice('版本冲突：图已经被别的操作更新（当前页不是最新），请先重新读取，再重试保护。本次没有写入任何状态。');
    } else {
      notice(`保护未写入：${ERROR_TEXT[error.message] || error.message}`);
    }
  }
}

async function sendFeedback(unit, result) {
  try {
    const answer = await api('/v1/feedback', {
      unit_id: unit.memory_id,
      expected_source_version: unit.source_version,
      event_id: crypto.randomUUID(),
      result,
      expected_revision: S.graph.revision,
    });
    await loadGraph({keepView: true});
    notice(result === 'helpful'
      ? (answer.effective ? '已记录「有帮助」：活跃度回升。' : '已记录「有帮助」（同一 UTC 日已强化过一次，本次不再累加）。')
      : '已记录「没帮助」：只记账，不降权。');
  } catch (error) {
    if (error.message === 'revision_conflict') {
      notice('版本冲突：图已经被别的操作更新，请先重新读取，再重试反馈。本次没有任何反馈被写入。');
    } else {
      notice(`反馈未写入：${ERROR_TEXT[error.message] || error.message}`);
    }
  }
}
const UNIT_KIND_TEXT = {
  entity: '对象/实体', object: '对象', project: '项目', tool: '工具', topic: '话题', matter: '事项',
  document: '文档（路径）', entry: '条目', fact: '事实', knowledge: '知识', experience: '经验',
  section: '小节', relation_group: '关联组', source_metadata: '来源元数据', thread_row: '待办行',
};

// ── 筛选 ────────────────────────────────────────────────────
function renderFilters() {
  const counts = new Map();
  for (const node of S.graph.nodes) counts.set(node.kind, (counts.get(node.kind) || 0) + 1);
  const box = $('filters');
  box.replaceChildren();
  box.classList.remove('hint');
  for (const [kind, count] of [...counts.entries()].sort((a, b) => b[1] - a[1])) {
    const label = document.createElement('label');
    label.className = 'filter-item';
    const input = document.createElement('input');
    input.type = 'checkbox'; input.checked = true; input.dataset.kind = kind;
    input.addEventListener('change', () => applyFilter());
    label.append(input, document.createTextNode(` ${UNIT_KIND_TEXT[kind] || kind} ${count}`));
    box.append(label);
  }
}
function applyFilter() {
  const boxes = [...$('filters').querySelectorAll('input[data-kind]')];
  S.filter.hidden = new Set(boxes.filter(b => !b.checked).map(b => b.dataset.kind));
  let visible = 0;
  for (const node of S.graph.nodes) {
    const hidden = S.filter.hidden.has(node.kind);
    if (!hidden) visible++;
    const el = S.nodeEls.get(node.id);
    if (el) el.classList.toggle('hidden', hidden);
  }
  for (const item of S.edgeEls) {
    const a = S.nodeById.get(item.from), b = S.nodeById.get(item.to);
    const hidden = (a && S.filter.hidden.has(a.kind)) || (b && S.filter.hidden.has(b.kind));
    item.el.classList.toggle('hidden', !!hidden);
  }
  const total = S.graph.nodes.length;
  $('filter-state').textContent = S.filter.hidden.size ? `已筛选 ${visible}/${total} 节点（被筛掉不等于未加载）` : `${total}/${total} 节点全部显示`;
  $('visible-count').textContent = `可见 ${visible}/${total} · 线 ${S.graph.edges.length}`;
  updateLabels();
}

// ── 搜索定位 ────────────────────────────────────────────────
function searchNodes(query) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const hits = [];
  for (const node of S.graph.nodes) {
    const label = (node.label || '').toLowerCase();
    if (label.includes(q) || node.id.toLowerCase().includes(q)) hits.push(node);
    if (hits.length >= 40) break;
  }
  return hits;
}
function renderSearchHits(hits, query) {
  const box = $('search-hits');
  box.replaceChildren();
  if (!query.trim()) return;
  if (!hits.length) { box.textContent = `没有匹配「${query}」的节点。`; return; }
  const head = document.createElement('div');
  head.className = 'hint'; head.textContent = `命中 ${hits.length} 个（最多显示 40）`;
  box.append(head);
  for (const node of hits.slice(0, 40)) {
    const row = document.createElement('button');
    row.className = 'hit';
    row.textContent = `${fullLabel(node)} · ${UNIT_KIND_TEXT[node.kind] || node.kind}`;
    row.title = node.id;
    row.addEventListener('click', () => {
      selectNode(node.id).catch(e => notice('未完成：' + e.message));
      centerOn(node.id, Math.max(S.view.k, 1.2));
    });
    box.append(row);
  }
}

// ── 候选审核 ────────────────────────────────────────────────
function filteredCandidates() {
  const all = S.graph.candidates || [];
  if (S.cand.filter === 'all') return all;
  return all.filter(c => c.state === S.cand.filter);
}
function renderCandidates() {
  const box = $('candidates');
  const list = filteredCandidates();
  const pages = Math.max(1, Math.ceil(list.length / CAND_SIZE));
  S.cand.page = Math.min(S.cand.page, pages - 1);
  const slice = list.slice(S.cand.page * CAND_SIZE, (S.cand.page + 1) * CAND_SIZE);
  box.replaceChildren();
  const meta = [
    `当前生成候选 ${(S.graph.candidates || []).length} 条`,
    S.graph.candidate_meta ? `（生成上限 ${S.graph.candidate_meta.generation_cap}，不代表全部潜在关系）` : '',
    `· 待审 ${(S.graph.candidates || []).filter(c => c.state === 'candidate').length}`,
  ].join('');
  $('cand-meta').textContent = meta;
  if (!slice.length) {
    const p = document.createElement('p'); p.className = 'hint'; p.textContent = '这一栏没有候选。';
    box.append(p);
  }
  for (const cand of slice) box.append(candidateCard(cand));
  $('cand-page').textContent = `第 ${S.cand.page + 1}/${pages} 页 · 共 ${list.length} 条`;
  $('cand-prev').disabled = S.cand.page === 0;
  $('cand-next').disabled = S.cand.page >= pages - 1;
  hydrateDetails(slice.map(c => c.id));
}

function candidateCard(cand) {
  const card = document.createElement('div');
  card.className = `candidate state-${cand.state}`;
  card.dataset.id = cand.id;

  const head = document.createElement('div');
  head.className = 'cand-head';
  const state = document.createElement('span');
  state.className = 'cand-state'; state.textContent = STATUS_TEXT[cand.state] || cand.state;
  const terms = document.createElement('span');
  terms.className = 'cand-terms'; terms.textContent = `匹配词：${(cand.matched_terms || []).join('、') || '—'}`;
  head.append(state, terms);

  const claim = document.createElement('div');
  claim.className = 'cand-claim';
  claim.textContent = `${cand.claim || '可能同主题'}（生成规则 ${cand.rule || '—'}）`;

  const pair = document.createElement('div');
  pair.className = 'cand-pair';
  pair.innerHTML = '<div class="end" data-end="0">读取 A 端定位…</div><div class="end" data-end="1">读取 B 端定位…</div>';

  const actions = document.createElement('div');
  actions.className = 'cand-actions';
  const expand = document.createElement('button');
  expand.className = 'secondary'; expand.textContent = '对照两端原文';
  expand.addEventListener('click', () => toggleCandidate(cand, card, expand));
  actions.append(expand);
  if (cand.state === 'candidate') {
    for (const [action, text] of [['accept', '接受'], ['reject', '拒绝']]) {
      const b = document.createElement('button');
      b.textContent = text; b.dataset.action = action; b.disabled = action === 'accept';
      b.title = action === 'accept' ? '两端来源核验通过后才能接受' : '拒绝会记进历史，不依赖来源可读';
      b.addEventListener('click', () => decide(cand, action));
      actions.append(b);
    }
  }
  card.append(head, claim, pair, actions);
  return card;
}

async function hydrateDetails(ids) {
  const missing = ids.filter(id => !S.cand.details.has(id));
  if (!missing.length) return;
  try {
    const result = await api('/v1/candidates/detail', {candidate_ids: missing});
    for (const item of result.items) S.cand.details.set(item.candidate.id, item);
  } catch (error) {
    notice('候选定位读取失败：' + (ERROR_TEXT[error.message] || error.message));
  }
  for (const id of ids) patchCard(id);
}

function patchCard(candId) {
  const card = $('candidates').querySelector(`.candidate[data-id="${CSS.escape(candId)}"]`);
  const detail = S.cand.details.get(candId);
  if (!card || !detail) return;
  const ends = card.querySelectorAll('.end');
  detail.endpoints.forEach((ep, i) => {
    const el = ends[i]; if (!el) return;
    el.replaceChildren();
    const title = document.createElement('div');
    title.className = 'end-title';
    title.textContent = `${i === 0 ? 'A' : 'B'} 端 · ${ep.label || ep.unit_id}`;
    const path = document.createElement('div');
    path.className = 'end-path';
    path.textContent = ep.status === 'ok'
      ? `${ep.path} · ${ep.locator} · 行 ${ep.lines?.[0]}–${ep.lines?.[1]} · v${String(ep.source_version).slice(0, 12)}`
      : `${ERROR_TEXT[ep.status] || ep.status}${ep.reason ? `（${ep.reason}）` : ''}：${ep.path || ep.unit_id}（不能接受；可拒绝并记录判断）`;
    el.classList.toggle('bad', ep.status !== 'ok');
    el.append(title, path);
  });
  const shared = document.createElement('div');
  shared.className = 'cand-shared';
  shared.textContent = `共同实体：${detail.shared_entity || '—'} · 两端状态：${detail.endpoints.map(e => (e.status === 'ok' ? '可读' : ERROR_TEXT[e.status] || e.status)).join(' / ')}`;
  card.querySelector('.cand-claim').after(shared);
  const accept = card.querySelector('button[data-action="accept"]');
  if (accept && detail.can_accept) { accept.disabled = false; accept.title = '接受后写入关联线（revision 冲突会提示重新加载）'; }
}

async function toggleCandidate(cand, card, button) {
  if (S.cand.open.has(cand.id)) {
    S.cand.open.delete(cand.id);
    card.querySelector('.cand-detail')?.remove();
    button.textContent = '对照两端原文';
    return;
  }
  const detail = S.cand.details.get(cand.id);
  if (!detail) { notice('两端定位还在读取，稍等一下再点。'); return; }
  S.cand.open.add(cand.id);
  button.textContent = '收起对照';
  const box = document.createElement('div');
  box.className = 'cand-detail';
  card.append(box);
  for (const ep of detail.endpoints) {
    const block = document.createElement('div');
    block.className = 'end-full';
    const head = document.createElement('div');
    head.className = 'end-path';
    head.textContent = `${ep.label || ep.unit_id} · ${ep.path || ''} · ${ep.locator || ''} · 行 ${ep.lines ? ep.lines.join('–') : '?'} · unit_id ${ep.unit_id} · v${String(ep.source_version).slice(0, 12)}`;
    const pre = document.createElement('pre');
    pre.className = 'end-body';
    pre.textContent = '读取中…';
    block.append(head, pre);
    box.append(block);
    if (ep.status !== 'ok') {
      pre.textContent = `不显示正文：${ERROR_TEXT[ep.status] || ep.status}${ep.reason ? `（${ep.reason}）` : ''}。不使用检索投影或第三条记忆补正文。`;
      pre.classList.add('bad');
      continue;
    }
    try {
      const unit = await api('/v1/fetch', {unit_id: ep.unit_id, expected_source_version: ep.source_version});
      pre.textContent = unit.text || '（空）';
      head.textContent += ` · 读取版本 ${String(unit.source_version).slice(0, 12)}`;
    } catch (error) {
      pre.textContent = `读取失败：${ERROR_TEXT[error.message] || error.message}。可拒绝并记录判断，不会伪装成已接受。`;
      pre.classList.add('bad');
    }
  }
}

async function decide(cand, action) {
  try {
    await api('/v1/graph/edit', {
      action, payload: {candidate_id: cand.id},
      expected_revision: S.graph.revision, idempotency_key: crypto.randomUUID(),
    });
    notice(action === 'accept' ? '已接受：进入历史，刷新后仍在。' : '已拒绝：判断已记录，进入历史。');
    await loadGraph({keepView: true});
  } catch (error) {
    if (error.message === 'revision_conflict') {
      notice('版本冲突：图已经变了（或已有别的决定）。请重新读取后再判断，不自动重试覆盖。');
      await loadGraph({keepView: true});
    } else notice('未完成：' + (ERROR_TEXT[error.message] || error.message));
  }
}

// ── 图加载 ──────────────────────────────────────────────────
async function loadGraph({mode = S.graph.mode, focus = S.focus, keepView = false} = {}) {
  const token = ++S.loadToken;
  const t0 = performance.now();
  const payload = mode === 'overview' ? {mode: 'overview'} : {mode: 'local', focus: focus || null, limit: LOCAL_LIMIT};
  const graph = await api('/v1/graph/view', payload);
  if (token !== S.loadToken) return;            // 期间又切了视图，丢弃旧响应
  S.graph = graph;
  S.focus = mode === 'overview' ? null : focus;
  S.activationUnits = (graph.activation && graph.activation.units) || {};
  buildIndexes();
  buildPositions();
  renderGraph();
  renderFilters();
  renderCandidates();
  applyActivation();
  if (!keepView) fitView();
  const meta = [`图版本 ${graph.revision}`, `${graph.total_nodes ?? graph.nodes.length} 节点 / ${graph.total_edges ?? graph.edges.length} 线`];
  if (graph.mode === 'local' || graph.partial) {
    meta.push(`局部：显示 ${graph.returned_nodes ?? graph.nodes.length}/${graph.total_nodes ?? graph.nodes.length} 个节点`);
  } else meta.push('全库：全部节点与关系');
  $('graph-meta').textContent = meta.join(' · ');
  const cov = graph.coverage || {};
  $('coverage').textContent = `解析覆盖：文件 ${cov.parsed ?? '?'} · 排除 ${cov.excluded ?? '?'} · 错误 ${cov.errors ?? '?'} · 单元 ${cov.units ?? '?'}`;
  $('scope-hint').textContent = mode === 'overview'
    ? `全库总览：${graph.total_nodes} 个节点、${graph.total_edges} 条关系全部在视图里（缩小时显示点与线，放大后出现更多标签）。`
    : `主题局部：显示 ${graph.returned_nodes ?? graph.nodes.length}/${graph.total_nodes} 个节点（围绕「${focusLabel(focus)}」）。`;
  window.__lastLoadMs = performance.now() - t0;
  return graph;
}
function focusLabel(focus) {
  if (!focus) return '对象实体';
  const node = S.nodeById.get(focus);
  return node ? fullLabel(node) : focus;
}

// ── 连接与事件 ──────────────────────────────────────────────
$('connect').addEventListener('click', guarded(async () => {
  const t0 = performance.now();
  S.token = $('token').value.trim();
  if (!S.token) { notice('先填连接码。'); return; }
  const health = await api('/v1/health');
  S.connected = true;
  $('token').value = '';
  $('status').textContent = `已连接 · ${health.mode === 'sandbox' ? '隔离样本' : '正式来源只读'} · v${health.version} · 图 r${health.revision}`;
  S.layout = await api('/v1/views/default');
  await loadGraph({mode: 'overview'});
  // 首次可交互：布局与首帧渲染都完成
  requestAnimationFrame(() => requestAnimationFrame(() => {
    window.__ttfi = performance.now() - t0;
    notice(`已连接。全库 ${S.graph.total_nodes} 节点 / ${S.graph.total_edges} 线已加载（首次可交互 ${Math.round(window.__ttfi)} ms）。`);
  }));
}));

document.querySelectorAll('#mode-switch button').forEach(button => {
  button.addEventListener('click', guarded(async () => {
    const mode = button.dataset.mode;
    document.querySelectorAll('#mode-switch button').forEach(b => b.classList.toggle('on', b === button));
    if (!S.connected) { notice('先连接。'); return; }
    await loadGraph({mode, focus: mode === 'overview' ? null : (S.selected || null)});
    notice(mode === 'overview' ? '全库总览：每个节点和每条关系都在视图里。' : '主题局部：只显示围绕选中节点的一小块，已标注范围。');
  }));
});
document.querySelectorAll('#cand-filter button').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#cand-filter button').forEach(b => b.classList.toggle('on', b === button));
    S.cand.filter = button.dataset.cand;
    S.cand.page = 0;
    S.cand.open.clear();
    renderCandidates();
  });
});
$('cand-prev').addEventListener('click', () => { S.cand.page = Math.max(0, S.cand.page - 1); S.cand.open.clear(); renderCandidates(); });
$('cand-next').addEventListener('click', () => { S.cand.page += 1; S.cand.open.clear(); renderCandidates(); });

$('fit').addEventListener('click', () => { fitView(); notice('已适应画布（全图不裁切）。'); });
$('activation-toggle').addEventListener('change', applyActivation);
$('save-layout').addEventListener('click', saveLayout);
$('refresh').addEventListener('click', guarded(async () => {
  await api('/v1/refresh', {});
  await loadGraph({keepView: true});
  notice('来源重新读取完成；健康状态与解析覆盖已更新。');
}));
$('search-go').addEventListener('click', () => {
  const query = $('search').value;
  const hits = searchNodes(query);
  renderSearchHits(hits, query);
  if (hits.length) { selectNode(hits[0].id).catch(e => notice('未完成：' + e.message)); centerOn(hits[0].id, Math.max(S.view.k, 1.2)); }
});
$('search').addEventListener('keydown', ev => { if (ev.key === 'Enter') $('search-go').click(); });
$('filter-clear').addEventListener('click', () => {
  $('filters').querySelectorAll('input[data-kind]').forEach(input => { input.checked = true; });
  applyFilter();
  notice('筛选已清除：被筛掉的内容本来也都在视图里。');
});

$('set-from').addEventListener('click', () => {
  if (!S.selected) return notice('先选择一个节点。');
  S.from = S.selected;
  $('link-state').textContent = '起点已选。再选目标节点，点“关联到当前节点”。';
});
$('link-to').addEventListener('click', guarded(async () => {
  if (!S.from || !S.selected) return notice('请先选择起点与目标。');
  await api('/v1/graph/edit', {
    action: 'connect', payload: {from: S.from, to: S.selected, predicate: 'related_to'},
    expected_revision: S.graph.revision, idempotency_key: crypto.randomUUID(),
  });
  S.from = null; $('link-state').textContent = '';
  await loadGraph({keepView: true});
  notice('关联已保存。原始记忆没有改变。');
}));
$('edge-action').addEventListener('click', guarded(async () => {
  const edge = S.graph.edges.find(e => e.id === S.selectedEdge);
  if (!edge) return notice('先点击一条连线。');
  await api('/v1/graph/edit', {
    action: edge.lifecycle === 'disabled' ? 'restore' : 'disable', payload: {edge_id: edge.id},
    expected_revision: S.graph.revision, idempotency_key: crypto.randomUUID(),
  });
  await loadGraph({keepView: true});
  notice(edge.lifecycle === 'disabled' ? '已恢复：这条线重新参与检索遍历。' : '已停用：线仍显示（虚线），但不参与检索遍历。');
}));

$('query').addEventListener('click', guarded(async () => {
  const text = $('question').value.trim();
  if (!text) return notice('先写一个问题。');
  const result = await api('/v1/query', {text});
  S.highlighted = new Set(result.items.flatMap(item => (item.path && item.path.edges) || []));
  drawEdgeState();
  const first = result.items[0];
  const nodeId = first && first.path && first.path.node_ids && first.path.node_ids[0];
  if (nodeId && S.nodeById.has(nodeId)) {
    await selectNode(nodeId);
    centerOn(nodeId, Math.max(S.view.k, 1.0));
  }
  $('results').replaceChildren();
  $('detail-title').textContent = `查询结果 · ${result.status}`;
  $('detail-meta').textContent = `图版本 ${result.graph_revision} · ${result.retrieval_mode} · 当前视图 ${S.graph.mode === 'overview' ? '全库总览' : '主题局部'}`;
  $('detail-body').textContent = first?.text || '没有足够资料。';
  for (const item of result.items) {
    const row = document.createElement('div');
    row.className = 'result';
    row.textContent = `${item.citation.path}:${item.citation.lines[0]} · ${item.source_kind}`;
    const b = document.createElement('button');
    b.className = 'secondary'; b.textContent = '读取此来源';
    b.addEventListener('click', () => { $('detail-body').textContent = item.text; });
    row.append(b);
    $('results').append(row);
  }
  notice([...(result.missing || []), ...(result.warnings || []),
    `展开 ${result.budget.expanded_edges} 条边；${result.items.length} 组证据；高亮 ${S.highlighted.size} 条真实 edge id。`].join(' · '));
}));
for (const button of document.querySelectorAll('[data-query]')) {
  button.addEventListener('click', () => { $('question').value = button.dataset.query; $('query').click(); });
}

// 缩放 / 平移
$('canvas').addEventListener('wheel', ev => {
  ev.preventDefault();
  zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.15 : 1 / 1.15);
}, {passive: false});
$('canvas').addEventListener('dblclick', () => { fitView(); notice('已适应画布。'); });
let panning = null;
$('canvas').addEventListener('pointerdown', ev => {
  if (ev.target.id !== 'canvas' && ev.target.id !== 'viewport' && ev.target.id !== 'edges') return;
  panning = {x: ev.clientX, y: ev.clientY, vx: S.view.x, vy: S.view.y};
  $('canvas').setPointerCapture(ev.pointerId);
});
$('canvas').addEventListener('pointermove', ev => {
  if (!panning) return;
  S.view.x = panning.vx + (ev.clientX - panning.x);
  S.view.y = panning.vy + (ev.clientY - panning.y);
  applyView();
});
$('canvas').addEventListener('pointerup', ev => {
  panning = null;
  try { $('canvas').releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
});
window.addEventListener('resize', () => applyView());
ensureLabelPool();
applyView();

// 调试用只读句柄（不影响任何行为；验收脚本用它读取 revision/选中项）
window.__she = S;
