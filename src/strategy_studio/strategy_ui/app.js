(function () {
  'use strict';
  // ══════════════════════════════════════════════════════════════════
  // Strategy Studio — manual decision trees on a scored population
  // ══════════════════════════════════════════════════════════════════
  var $ = function (s) { return document.querySelector('.ss ' + s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll('.ss ' + s)); };
  var BUREAU = /vantage|fico|bureau|equifax|efx|inquir|delinq|dpd|bankrupt|collection|utiliz|tradeline|credit_age|thin_file|revolv|derog|public_record|chargeoff|trade/i;
  var NUM_OPS = ['>=', '>', '<=', '<', '==', '!=', 'between', 'is_missing'];
  var CAT_OPS = ['==', '!=', 'in', 'not_in', 'is_missing'];
  var OP_LABEL = { '>=': '≥', '>': '>', '<=': '≤', '<': '<', '==': '=', '!=': '≠', 'between': 'between',
                   'in': 'in list', 'not_in': 'not in list', 'is_missing': 'is missing' };
  var S = { pop: null, cols: {}, tree: null, meta: null, selected: null, ev: null, tab: 'tree', jt: 'gen',
            strategies: [], timer: null, seq: 0 };

  // ── helpers ──────────────────────────────────────────────────────
  function url(p) { return dataiku.getWebAppBackendUrl(p); }
  async function api(path, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    var r = await fetch(url(path), opt), j;
    try { j = await r.json(); } catch (e) { throw new Error('Backend returned ' + r.status); }
    if (!r.ok || j.error) throw new Error(j.error || ('Backend returned ' + r.status));
    return j;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(x, d) {
    if (x == null || isNaN(x)) return '–';
    return Number(x).toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function pct(x, d) { return x == null || isNaN(x) ? '–' : (100 * x).toFixed(d == null ? 1 : d) + '%'; }
  function money(x) { return x == null || isNaN(x) ? '–' : (x < 0 ? '−$' : '$') + num(Math.abs(x)); }
  var toastTimer;
  function toast(msg, err) {
    var t = $('#toast'); t.textContent = msg; t.hidden = false; t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, err ? 7000 : 2600);
  }
  async function busy(btn, fn) {
    if (btn) btn.disabled = true;
    try { return await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
  }
  function options(sel, values, selected, labelFn) {
    sel.innerHTML = values.map(function (v) {
      return '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(labelFn ? labelFn(v) : v) + '</option>';
    }).join('');
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function source(c) {
    if (S.pop && (c === S.pop.score_col || /^points_/.test(c))) return 'DATAIKU_SCORECARD';
    return BUREAU.test(c) ? 'EQUIFAX_CONSUMER_CREDIT_FILE' : 'APPLICATION_DATA';
  }
  var SRC_LABEL = { DATAIKU_SCORECARD: 'Scorecard', EQUIFAX_CONSUMER_CREDIT_FILE: 'Equifax', APPLICATION_DATA: 'Application' };
  function highlightJson(text) {
    return esc(text)
      .replace(/(&quot;[^&]*?&quot;)(\s*:)/g, '<span class="k">$1</span>$2')
      .replace(/(:\s*)(&quot;.*?&quot;)/g, '$1<span class="s">$2</span>')
      .replace(/(:\s*|\[\s*|,\s*)(-?\d+\.?\d*(?:e[+-]?\d+)?)(?=[\s,\]\n])/gi, '$1<span class="n">$2</span>')
      .replace(/\b(true|false|null)\b/g, '<span class="b">$1</span>');
  }

  // ── tree utilities ───────────────────────────────────────────────
  function walk(n, fn, parent, branch, depth) {
    fn(n, parent || null, branch || null, depth || 0);
    if (n.type === 'split') { walk(n.yes, fn, n, 'yes', (depth || 0) + 1); walk(n.no, fn, n, 'no', (depth || 0) + 1); }
  }
  function find(id) {
    var hit = null;
    walk(S.tree, function (n, p, b) { if (n.id === id) hit = { node: n, parent: p, branch: b }; });
    return hit;
  }
  function nextId() {
    var max = 0;
    walk(S.tree, function (n) { var m = /^n(\d+)$/.exec(n.id); if (m) max = Math.max(max, +m[1]); });
    return 'n' + (max + 1);
  }
  function replaceNode(id, repl) {
    var f = find(id);
    if (!f.parent) S.tree = repl; else f.parent[f.branch] = repl;
  }
  function newLeaf(name, base) {
    var b = base ? clone(base) : null;
    return {
      id: nextId(), type: 'leaf', name: name,
      score_adjust: b ? b.score_adjust : { type: 'none', value: 0 },
      decision: b ? b.decision : { mode: 'cutoff', approve_at: +$('#baseline').value || null, refer_at: null },
      reason_codes: b ? b.reason_codes : [], pricing_tier: b ? b.pricing_tier : '', limit_multiplier: b ? b.limit_multiplier : 1
    };
  }
  function condText(n) {
    var v = n.variable || '?';
    if (n.op === 'is_missing') return v + ' is missing';
    if (n.op === 'between') return v + ' in [' + n.value + ', ' + n.value2 + ']';
    if (n.op === 'in' || n.op === 'not_in') return v + (n.op === 'in' ? ' in ' : ' not in ') + '{' + n.value + '}';
    return v + ' ' + (OP_LABEL[n.op] || n.op) + ' ' + n.value;
  }
  function actionText(l) {
    var p = [], a = l.score_adjust || {}, v = +a.value, d = l.decision || {};
    if (a.type === 'pct' && v) p.push('score ' + (v > 0 ? '+' : '−') + Math.abs(v) + '%');
    if (a.type === 'points' && v) p.push('score ' + (v > 0 ? '+' : '−') + Math.abs(v) + ' pts');
    if (a.type === 'set') p.push('score = ' + a.value);
    if ((d.mode || 'cutoff') === 'cutoff') p.push('≥' + (d.approve_at == null ? '?' : d.approve_at) + (d.refer_at != null && d.refer_at !== '' ? ' / refer ≥' + d.refer_at : ''));
    else p.push(d.mode.toUpperCase());
    if (l.pricing_tier) p.push('tier ' + l.pricing_tier);
    if (l.limit_multiplier != null && +l.limit_multiplier !== 1) p.push('limit ×' + l.limit_multiplier);
    return p.join(' · ');
  }
  function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  // ── tree rendering (top-down tidy layout, inline SVG) ────────────
  var NW = 214, NH = 80, HG = 22, VG = 58, PAD = 18;
  function layout() {
    var pos = {}, col = 0, maxDepth = 0;
    (function place(n, depth) {
      maxDepth = Math.max(maxDepth, depth);
      if (n.type === 'leaf') { pos[n.id] = { x: col * (NW + HG), y: depth * (NH + VG) }; col++; return; }
      place(n.yes, depth + 1); place(n.no, depth + 1);
      pos[n.id] = { x: (pos[n.yes.id].x + pos[n.no.id].x) / 2, y: depth * (NH + VG) };
    })(S.tree, 0);
    return { pos: pos, w: col * (NW + HG) - HG + PAD * 2, h: (maxDepth + 1) * (NH + VG) - VG + PAD * 2 };
  }
  function leafColor(l) {
    var m = (l.decision || {}).mode || 'cutoff';
    return { approve: '#1e8449', refer: '#c27c0e', decline: '#c0392b', cutoff: '#1a6fc4' }[m] || '#1a6fc4';
  }
  function renderTree() {
    if (!S.tree) return;
    var L = layout(), P = L.pos, out = [], errIds = {};
    ((S.ev && S.ev.issues) || []).forEach(function (i) { if (i.level === 'error') errIds[i.node_id] = 1; });
    walk(S.tree, function (n) {
      if (n.type !== 'split') return;
      ['yes', 'no'].forEach(function (b) {
        var a = P[n.id], c = P[n[b].id];
        var x1 = a.x + NW / 2 + PAD, y1 = a.y + NH + PAD, x2 = c.x + NW / 2 + PAD, y2 = c.y + PAD, my = (y1 + y2) / 2;
        out.push('<path class="edge" d="M' + x1 + ',' + y1 + ' C' + x1 + ',' + my + ' ' + x2 + ',' + my + ' ' + x2 + ',' + y2 + '"/>');
        var lx = (x1 + x2) / 2 + (b === 'yes' ? -6 : 6), ly = my - 4;
        var miss = (n.missing === 'yes') === (b === 'yes') && n.op !== 'is_missing' ? ' +missing' : '';
        out.push('<text class="elabel ' + b + '" x="' + lx + '" y="' + ly + '" text-anchor="' + (b === 'yes' ? 'end' : 'start') + '">' +
                 b + '<tspan style="font-weight:400;fill:#8a96a3">' + miss + '</tspan></text>');
      });
    });
    walk(S.tree, function (n) {
      var p = P[n.id], x = p.x + PAD, y = p.y + PAD, st = S.ev && S.ev.nodes ? S.ev.nodes[n.id] : null;
      var leaf = n.type === 'leaf', color = leaf ? leafColor(n) : '#5b6b7c';
      var line1 = leaf ? (n.name || 'Outcome') : (n.name || 'Split');
      var line2 = leaf ? actionText(n) : condText(n);
      var line3 = st ? (num(st.n) + ' · ' + pct(st.share) + (st.bad_rate != null ? ' · bad ' + pct(st.bad_rate) : '') +
                        (leaf ? ' · appr ' + pct(st.approval_rate, 0) : '')) : '';
      out.push('<g class="tn' + (S.selected === n.id ? ' sel' : '') + (errIds[n.id] ? ' err' : '') + '" data-id="' + esc(n.id) + '">' +
        '<rect class="box" x="' + x + '" y="' + y + '" width="' + NW + '" height="' + NH + '" rx="9"/>' +
        '<rect x="' + x + '" y="' + y + '" width="6" height="' + NH + '" rx="3" fill="' + color + '"/>' +
        '<text class="t1" x="' + (x + 14) + '" y="' + (y + 20) + '">' + esc(clip(line1, 30)) + '</text>' +
        '<text x="' + (x + 14) + '" y="' + (y + 40) + '">' + esc(clip(line2, 34)) + '</text>' +
        '<text class="t3" x="' + (x + 14) + '" y="' + (y + 62) + '">' + esc(line3) + '</text>' +
        (leaf ? '' : '<text class="t3" x="' + (x + NW - 10) + '" y="' + (y + 20) + '" text-anchor="end">' + esc(n.id) + '</text>') +
        '</g>');
    });
    // Wide trees shrink to fit the panel (down to 70%), then scroll.
    var avail = $('#treeWrap').clientWidth - 4, scale = avail > 0 && L.w > avail ? Math.max(0.7, avail / L.w) : 1;
    $('#treeWrap').innerHTML = '<svg width="' + Math.round(L.w * scale) + '" height="' + Math.round(L.h * scale) +
      '" viewBox="0 0 ' + L.w + ' ' + L.h + '">' + out.join('') + '</svg>';
  }

  // ── node editor ──────────────────────────────────────────────────
  function varOptions(selected) {
    var groups = { DATAIKU_SCORECARD: [], EQUIFAX_CONSUMER_CREDIT_FILE: [], APPLICATION_DATA: [] };
    Object.keys(S.cols).forEach(function (c) { groups[source(c)].push(c); });
    return Object.keys(groups).map(function (g) {
      if (!groups[g].length) return '';
      return '<optgroup label="' + SRC_LABEL[g] + '">' + groups[g].map(function (c) {
        return '<option value="' + esc(c) + '"' + (c === selected ? ' selected' : '') + '>' + esc(c) + '</option>';
      }).join('') + '</optgroup>';
    }).join('');
  }
  function varHint(c) {
    var i = S.cols[c];
    if (!i) return '';
    var h = '<span class="src ' + source(c) + '">' + SRC_LABEL[source(c)] + '</span> ' + i.kind;
    if (i.kind === 'numeric' && i.min != null) h += ' · ' + num(i.min, 2) + ' – ' + num(i.max, 2) + ' (median ' + num(i.median, 2) + ')';
    if (i.top && i.top.length) h += ' · values: ' + esc(i.top.slice(0, 8).join(', '));
    h += ' · ' + i.missing_pct + '% missing';
    return h;
  }
  function statsLine(id, leaf) {
    var st = S.ev && S.ev.nodes ? S.ev.nodes[id] : null;
    if (!st) return '';
    var t = num(st.n) + ' rows (' + pct(st.share) + ')';
    if (st.bad_rate != null) t += ' · bad rate ' + pct(st.bad_rate);
    if (leaf) {
      t += '<br>Approve ' + pct(st.approval_rate) + ' · refer ' + pct(st.refer_rate) + ' · decline ' + pct(st.decline_rate);
      if (st.bad_rate_approved != null) t += ' · bad rate approved ' + pct(st.bad_rate_approved, 2);
      if (st.profit != null) t += ' · profit ' + money(st.profit);
      if (st.avg_adjusted_score != null) t += ' · avg adjusted score ' + num(st.avg_adjusted_score);
    }
    return '<div class="ed-stats">' + t + '</div>';
  }
  function field(label, html, full) { return '<label' + (full ? ' class="full"' : '') + '>' + label + html + '</label>'; }
  function inp(k, v, type, extra) {
    return '<input data-k="' + k + '" type="' + (type || 'text') + '" value="' + esc(v == null ? '' : v) + '"' + (extra || '') + '>';
  }
  function sel(k, vals, cur, labels) {
    return '<select data-k="' + k + '">' + vals.map(function (v, i) {
      return '<option value="' + esc(v) + '"' + (String(v) === String(cur) ? ' selected' : '') + '>' + esc(labels ? labels[i] : v) + '</option>';
    }).join('') + '</select>';
  }
  function renderEditor() {
    var ed = $('#editor');
    var f = S.selected ? find(S.selected) : null;
    if (!f) { ed.innerHTML = '<div class="hint">Click a node in the tree to edit it.</div>'; return; }
    var n = f.node, h;
    if (n.type === 'split') {
      var kind = (S.cols[n.variable] || {}).kind || 'numeric';
      var ops = kind === 'numeric' ? NUM_OPS : CAT_OPS;
      var top = (S.cols[n.variable] || {}).top || [];
      h = '<div class="ed-title"><span class="kind">CONDITION</span><b>' + esc(n.id) + '</b></div>' + statsLine(n.id, false) +
        '<div class="ed-grid">' +
        field('Node name', inp('name', n.name), true) +
        field('Variable', '<select data-k="variable">' + varOptions(n.variable) + '</select>', true) +
        '<div class="var-hint full">' + varHint(n.variable) + '</div>' +
        field('Operator', sel('op', ops, n.op, ops.map(function (o) { return OP_LABEL[o]; }))) +
        (n.op === 'is_missing' ? '<div></div>' :
          field(n.op === 'in' || n.op === 'not_in' ? 'Values (comma separated)' : n.op === 'between' ? 'From' : 'Value',
                inp('value', n.value, kind === 'numeric' && n.op !== 'in' && n.op !== 'not_in' ? 'number' : 'text', ' list="vals_' + esc(n.id) + '"') +
                '<datalist id="vals_' + esc(n.id) + '">' + top.map(function (v) { return '<option value="' + esc(v) + '">'; }).join('') + '</datalist>')) +
        (n.op === 'between' ? field('To', inp('value2', n.value2, 'number')) : '') +
        (n.op === 'is_missing' ? '' : field('Missing values go to', sel('missing', ['no', 'yes'], n.missing || 'no', ['No branch', 'Yes branch']), true)) +
        '</div><div class="ed-actions">' +
        '<button data-act="toLeaf">Convert to outcome</button>' +
        (f.parent ? '<button data-act="delete" class="danger">Delete branch</button>' : '') + '</div>';
    } else {
      var a = n.score_adjust || { type: 'none' }, d = n.decision || { mode: 'cutoff' };
      h = '<div class="ed-title"><span class="kind leaf">OUTCOME</span><b>' + esc(n.id) + '</b></div>' + statsLine(n.id, true) +
        '<div class="ed-grid">' +
        field('Outcome name', inp('name', n.name), true) +
        field('Score adjustment', sel('adj_type', ['none', 'pct', 'points', 'set'], a.type, ['None', '% of score', 'Points', 'Set score to'])) +
        (a.type === 'none' ? '<div></div>' : field(a.type === 'pct' ? 'Percent (e.g. -10)' : a.type === 'points' ? 'Points (e.g. -25)' : 'New score',
                                                  inp('adj_value', a.value, 'number'))) +
        field('Decision', sel('mode', ['cutoff', 'approve', 'refer', 'decline'], d.mode,
                              ['Score cut-off', 'Approve', 'Refer (manual review)', 'Decline'])) +
        (d.mode === 'cutoff' ? field('Approve if adjusted score ≥', inp('approve_at', d.approve_at, 'number')) : '<div></div>') +
        (d.mode === 'cutoff' ? field('Refer if adjusted score ≥ <span class="hint">optional</span>', inp('refer_at', d.refer_at, 'number'), true) : '') +
        field('Reason codes (comma separated)', inp('reason_codes', (n.reason_codes || []).join(', ')), true) +
        field('Pricing tier', sel('pricing_tier', ['', 'A', 'B', 'C', 'D', 'E'], n.pricing_tier || '', ['—', 'A', 'B', 'C', 'D', 'E'])) +
        field('Credit-limit multiplier', inp('limit_multiplier', n.limit_multiplier, 'number', ' step="0.05" min="0"')) +
        '</div><div class="ed-actions"><button data-act="split" class="primary">Split this outcome</button>' +
        (f.parent ? '<button data-act="delete" class="danger">Delete branch</button>' : '') + '</div>';
    }
    ed.innerHTML = h;
  }
  function onEdit(e) {
    var k = e.target.dataset.k;
    if (!k || !S.selected) return;
    var n = find(S.selected).node, v = e.target.value;
    var numeric = e.target.type === 'number';
    var val = numeric ? (v === '' ? null : +v) : v;
    if (n.type === 'split') {
      if (k === 'variable') {
        n.variable = v;
        var info = S.cols[v] || {};
        var ops = info.kind === 'numeric' ? NUM_OPS : CAT_OPS;
        if (ops.indexOf(n.op) < 0) n.op = ops[0];
        n.value = info.kind === 'numeric' ? (info.median != null ? Math.round(info.median * 100) / 100 : 0) : ((info.top || [])[0] || '');
        n.value2 = info.kind === 'numeric' ? info.max : null;
        renderEditor();
      } else if (k === 'op') { n.op = v; if (v === 'between' && n.value2 == null) n.value2 = (S.cols[n.variable] || {}).max; renderEditor(); }
      else n[k] = val;
    } else {
      if (k === 'adj_type') { n.score_adjust = { type: v, value: v === 'pct' ? -10 : v === 'points' ? -20 : v === 'set' ? (n.decision.approve_at || 0) : 0 }; renderEditor(); }
      else if (k === 'adj_value') n.score_adjust.value = val;
      else if (k === 'mode') { n.decision.mode = v; if (v === 'cutoff' && n.decision.approve_at == null) n.decision.approve_at = +$('#baseline').value; renderEditor(); }
      else if (k === 'approve_at' || k === 'refer_at') n.decision[k] = val;
      else if (k === 'reason_codes') n.reason_codes = v.split(',').map(function (x) { return x.trim().toUpperCase(); }).filter(Boolean);
      else if (k === 'limit_multiplier') n.limit_multiplier = val;
      else n[k] = val;
    }
    changed();
  }
  function onEditorAction(e) {
    var act = e.target.dataset.act;
    if (!act || !S.selected) return;
    var f = find(S.selected), n = f.node;
    if (act === 'split') {
      // Both children copy this outcome's actions; ids are the next two free ones.
      var yes = newLeaf((n.name || 'Outcome') + ' – yes', n);
      var no = newLeaf((n.name || 'Outcome') + ' – no', n);
      no.id = 'n' + (+yes.id.slice(1) + 1);
      var split = { id: n.id, type: 'split', name: 'New condition', variable: S.pop.score_col, op: '>=',
                    value: (n.decision && n.decision.approve_at) || +$('#baseline').value, value2: null, missing: 'no', yes: yes, no: no };
      replaceNode(n.id, split);
      S.selected = split.id;
    } else if (act === 'toLeaf') {
      var leaf = newLeaf(n.name || 'Outcome'); leaf.id = n.id;
      replaceNode(n.id, leaf);
    } else if (act === 'delete') {
      var repl = newLeaf('Outcome'); repl.id = n.id;
      if (!confirm('Replace this branch (and everything under it) with a single outcome?')) return;
      replaceNode(n.id, repl);
    }
    renderEditor(); changed();
  }

  // ── evaluation, KPIs, JSON (debounced on every change) ───────────
  function settings() { return { margin: $('#margin').value, loss: $('#loss').value, baseline_cutoff: $('#baseline').value }; }
  function meta() {
    return { name: $('#stName').value.trim(), strategy_id: $('#stId').value.trim(), status: $('#stStatus').value,
             version: S.meta ? S.meta.version || 1 : 1, description: (S.meta && S.meta.description) || '' };
  }
  function changed() {
    renderTree();
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { busy(null, refresh); }, 250);
  }
  async function refresh() {
    if (!S.tree || !S.pop) return;
    var seq = ++S.seq;
    var body = Object.assign({ tree: S.tree, meta: meta() }, settings());
    var ev = await api('/api/evaluate', body);
    if (seq !== S.seq) return;
    S.ev = ev;
    renderTree(); renderEditor(); renderIssues(); renderKpis();
    await renderJson();
    if (S.tab === 'backtest') renderBacktest();
    if (S.tab === 'export') await renderExportPreview();
  }
  function renderIssues() {
    var iss = (S.ev && S.ev.issues) || [];
    $('#issues').innerHTML = iss.map(function (i) {
      return '<div class="issue ' + i.level + '" data-node="' + esc(i.node_id) + '">' + (i.level === 'error' ? '⛔ ' : '⚠ ') +
        '<b>' + esc(i.node_id) + '</b>: ' + esc(i.message) + '</div>';
    }).join('');
  }
  function renderKpis() {
    var ev = S.ev;
    if (!ev || !ev.valid) { $('#kpis').innerHTML = ''; return; }
    var t = ev.total, b = ev.baseline || {};
    function delta(a, c, fmt, invert) {
      if (a == null || c == null) return '';
      var d = a - c, good = invert ? d < 0 : d > 0;
      if (Math.abs(d) < 1e-9) return '<div class="d hint">= baseline</div>';
      return '<div class="d ' + (good ? 'up' : 'down') + '">' + (d > 0 ? '+' : '−') + fmt(Math.abs(d)) + ' vs baseline</div>';
    }
    var tiles = [
      ['Approval rate', pct(t.approval_rate), delta(t.approval_rate, b.approval_rate, function (x) { return pct(x); })],
      ['Refer rate', pct(t.refer_rate), ''],
      ['Decline rate', pct(t.decline_rate), ''],
      ['Bad rate, approved', pct(t.bad_rate_approved, 2), delta(t.bad_rate_approved, b.bad_rate_approved, function (x) { return pct(x, 2); }, true)],
      ['Profit', money(t.profit), delta(t.profit, b.profit, money)],
      ['Decisions changed', num(b.decisions_changed), '<div class="d hint">vs score ≥ ' + num(b.cutoff) + '</div>']
    ];
    $('#kpis').innerHTML = tiles.map(function (x) {
      return '<div class="kpi"><div class="v">' + x[1] + '</div><div class="l">' + x[0] + '</div>' + x[2] + '</div>';
    }).join('');
  }
  async function renderJson() {
    if (S.jt === 'adv') return;
    var r = await api('/api/export', Object.assign({ tree: S.tree, meta: meta(), format: $('#jsonFormat').value }, settings()));
    $('#jsonGen').innerHTML = highlightJson(r.content);
    $('#jsonNote').textContent = r.format === 'interconnect' ? 'Illustrative InterConnect-style schema · updates on every edit' : 'Updates on every edit';
  }

  // ── population ──────────────────────────────────────────────────
  async function onDataset() {
    var r = await api('/api/dataset_info?dataset=' + encodeURIComponent($('#ds').value));
    var cols = r.columns, m = r.meta || {};
    S.dsMeta = m;
    var sc = m.scorecard_score_column && cols.indexOf(m.scorecard_score_column) >= 0 ? m.scorecard_score_column :
      (cols.indexOf('score') >= 0 ? 'score' : (cols.filter(function (c) { return /score/i.test(c); })[0] || cols[0]));
    options($('#scoreCol'), cols, sc);
    var tg = m.scorecard_target && cols.indexOf(m.scorecard_target) >= 0 ? m.scorecard_target :
      (cols.filter(function (c) { return /^bad|_bad|default/i.test(c); })[0] || '');
    options($('#target'), [''].concat(cols), tg, function (v) { return v || '(none)'; });
    if (m.avg_margin_per_good != null) $('#margin').value = m.avg_margin_per_good;
    if (m.avg_loss_per_bad != null) $('#loss').value = m.avg_loss_per_bad;
    if (m.scorecard_cutoff != null) $('#baseline').value = Math.round(m.scorecard_cutoff);
  }
  async function loadPopulation(btn) {
    await busy(btn, async function () {
      var r = await api('/api/load', { dataset: $('#ds').value, score_col: $('#scoreCol').value, target: $('#target').value, meta: S.dsMeta || {} });
      S.pop = r; S.cols = {};
      r.columns.forEach(function (c) { S.cols[c.name] = c; });
      if (!$('#margin').value && r.meta.margin != null) $('#margin').value = r.meta.margin;
      if (!$('#loss').value && r.meta.loss != null) $('#loss').value = r.meta.loss;
      if (!$('#baseline').value && r.meta.cutoff != null) $('#baseline').value = Math.round(r.meta.cutoff);
      $('#popChips').innerHTML = '<span class="chip g">' + num(r.n_rows) + ' rows</span>' +
        (r.bad_rate != null ? '<span class="chip o">Bad rate ' + pct(r.bad_rate) + '</span>' : '') +
        '<span class="chip">Score ' + num(r.score_min) + '–' + num(r.score_max) + '</span>';
      $('#empty').hidden = true; $('#work').hidden = false;
      $('#outName').value = r.dataset + '_strategy';
      await refreshStrategies();
      if (!S.tree) await newFromTemplate('delinquency_override', true);
      else changed();
      toast('Population loaded: ' + num(r.n_rows) + ' rows');
    });
  }

  // ── strategies ──────────────────────────────────────────────────
  function setMeta(m) {
    S.meta = Object.assign({ version: 1 }, m || {});
    $('#stName').value = S.meta.name || ''; $('#stId').value = S.meta.strategy_id || '';
    $('#stStatus').value = S.meta.status || 'DRAFT'; $('#stVersion').textContent = 'v' + (S.meta.version || 1);
    $('#expName').value = S.meta.name || '';
  }
  async function newFromTemplate(name, quiet) {
    var r;
    try { r = await api('/api/template', { name: name, cutoff: $('#baseline').value }); }
    catch (e) { if (name !== 'cutoff_only') { toast(e.message + ' Using the cut-off template.', true); r = await api('/api/template', { name: 'cutoff_only', cutoff: $('#baseline').value }); } else throw e; }
    S.tree = r.tree; S.selected = S.tree.id; S.file = null;
    var n = S.strategies.length + 1;
    setMeta({ name: name === 'cutoff_only' ? 'STRATEGY_' + n + '_CHAMPION' : 'CHALLENGER_DEMO_' + n,
              strategy_id: 'IDX-' + String(Math.floor(1000 + Math.random() * 9000)),
              status: name === 'cutoff_only' ? 'CHAMPION' : 'CHALLENGER', version: 1 });
    renderStratList(); renderEditor(); changed();
    if (!quiet) toast('New strategy from template');
  }
  async function refreshStrategies() {
    try { S.strategies = (await api('/api/strategy/list')).strategies; }
    catch (e) { S.strategies = []; $('#stratList').innerHTML = '<div class="hint">⚠ ' + esc(e.message) + '</div>'; return; }
    renderStratList();
    var opts = ['__current__'].concat(S.strategies.map(function (s) { return s.file; }));
    var lab = function (v) {
      if (v === '__current__') return 'Current (unsaved edits)';
      var s = S.strategies.filter(function (x) { return x.file === v; })[0];
      return s.name + ' v' + s.version + ' · ' + s.status;
    };
    var champ = S.strategies.filter(function (s) { return s.status === 'CHAMPION'; })[0];
    options($('#cmpChamp'), opts, champ ? champ.file : '__current__', lab);
    options($('#cmpChal'), opts, '__current__', lab);
  }
  function renderStratList() {
    var cur = $('#stName').value;
    if (!S.strategies.length) { $('#stratList').innerHTML = '<div class="hint">No saved strategies yet.</div>'; return; }
    $('#stratList').innerHTML = S.strategies.map(function (s) {
      return '<div class="st' + (s.name === cur ? ' active' : '') + '" data-file="' + esc(s.file) + '"><b><i class="dot ' + esc(s.status) + '"></i>' +
        esc(s.name) + '</b><span>' + esc(s.strategy_id || '') + ' · v' + s.version + ' · ' + esc(s.status) + ' · ' + s.n_leaves + (s.n_leaves === 1 ? ' outcome' : ' outcomes') + '</span></div>';
    }).join('');
  }
  async function openStrategy(file) {
    await busy(null, async function () {
      var r = await api('/api/strategy/load', { file: file });
      S.tree = r.tree; S.selected = r.tree.id; S.file = file;
      setMeta(r.meta);
      var st = r.settings || {};
      if (st.margin != null) $('#margin').value = st.margin;
      if (st.loss != null) $('#loss').value = st.loss;
      if (st.baseline_cutoff != null) $('#baseline').value = st.baseline_cutoff;
      renderStratList(); renderEditor(); changed();
      toast('Opened ' + r.meta.name + ' v' + r.meta.version);
    });
  }
  async function saveStrategy(btn, newVersion) {
    await busy(btn, async function () {
      if (!$('#stName').value.trim()) throw new Error('Give the strategy a name.');
      var r = await api('/api/strategy/save', { meta: meta(), tree: S.tree, settings: settings(), new_version: !!newVersion });
      setMeta(r.meta);
      await refreshStrategies();
      toast((newVersion ? 'Saved new version ' : 'Saved ') + r.meta.name + ' v' + r.meta.version + ' to ' + r.file);
    });
  }

  // ── backtest tab ────────────────────────────────────────────────
  function renderBacktest() {
    var ev = S.ev;
    if (!ev || !ev.valid) { $('#btCompare').innerHTML = '<tr><td class="l">Fix validation errors first.</td></tr>'; $('#btLeaves').innerHTML = ''; return; }
    var t = ev.total, b = ev.baseline || {};
    $('#btBaselineNote').textContent = b.cutoff != null ? '(baseline: approve when score ≥ ' + num(b.cutoff) + ')' : '';
    var rows = [['Approved', num(t.approved), num(b.approved)], ['Referred', num(t.referred), num(b.referred)],
      ['Declined', num(t.declined), num(b.declined)], ['Approval rate', pct(t.approval_rate), pct(b.approval_rate)],
      ['Bad rate, approved', pct(t.bad_rate_approved, 2), pct(b.bad_rate_approved, 2)],
      ['Bads approved', num(t.bads_approved), num(b.bads_approved)], ['Goods approved', num(t.goods_approved), num(b.goods_approved)],
      ['Profit', money(t.profit), money(b.profit)]];
    $('#btCompare').innerHTML = '<tr><th class="l">Metric</th><th>Strategy</th><th>Baseline</th></tr>' +
      rows.map(function (r) { return '<tr><td class="l">' + r[0] + '</td><td><b>' + r[1] + '</b></td><td>' + r[2] + '</td></tr>'; }).join('');
    $('#btLeaves').innerHTML = '<tr><th class="l">Outcome</th><th class="l">Action</th><th>Rows</th><th>Share</th><th>Bad rate</th>' +
      '<th>Approve</th><th>Refer</th><th>Decline</th><th>Bad rate appr.</th><th>Avg adj. score</th><th>Profit</th></tr>' +
      ev.leaves.map(function (l) {
        return '<tr><td class="l"><b>' + esc(l.id) + '</b> ' + esc(l.name) + '</td><td class="l hint">' + esc(l.action) + '</td><td>' + num(l.n) +
          '</td><td>' + pct(l.share) + '</td><td>' + pct(l.bad_rate) + '</td><td>' + pct(l.approval_rate) + '</td><td>' + pct(l.refer_rate) +
          '</td><td>' + pct(l.decline_rate) + '</td><td>' + pct(l.bad_rate_approved, 2) + '</td><td>' + num(l.avg_adjusted_score) +
          '</td><td>' + money(l.profit) + '</td></tr>';
      }).join('');
  }
  async function compare(btn) {
    await busy(btn, async function () {
      var pick = function (v) { return v === '__current__' ? S.tree : v; };
      var r = await api('/api/compare', Object.assign({ champion: pick($('#cmpChamp').value), challenger: pick($('#cmpChal').value) }, settings()));
      var a = r.champion, c = r.challenger;
      var line = function (l, x, y) { return '<tr><td class="l">' + l + '</td><td>' + x + '</td><td>' + y + '</td></tr>'; };
      var html = '<div class="row wrap" style="align-items:flex-start;gap:16px"><table class="tbl" style="max-width:420px">' +
        '<tr><th class="l">Metric</th><th>Champion</th><th>Challenger</th></tr>' +
        line('Approval rate', pct(a.approval_rate), pct(c.approval_rate)) + line('Refer rate', pct(a.refer_rate), pct(c.refer_rate)) +
        line('Bad rate, approved', pct(a.bad_rate_approved, 2), pct(c.bad_rate_approved, 2)) + line('Profit', money(a.profit), money(c.profit)) +
        '</table><table class="tbl" style="max-width:460px"><tr><th class="l">Swap set: champion → challenger</th><th>Rows</th><th>Bad rate</th></tr>' +
        r.swaps.filter(function (s) { return s.n; }).map(function (s) {
          return '<tr><td class="l"><span class="dec ' + s.champion + '">' + s.champion + '</span> → <span class="dec ' + s.challenger + '">' +
            s.challenger + '</span></td><td>' + num(s.n) + '</td><td>' + pct(s.bad_rate) + '</td></tr>';
        }).join('') + '</table></div>';
      $('#cmpOut').innerHTML = html;
    });
  }

  // ── export & deploy tab ─────────────────────────────────────────
  async function renderExportPreview() {
    var r = await api('/api/export', Object.assign({ tree: S.tree, meta: meta(), format: $('#expFormat').value }, settings()));
    S.lastExport = r;
    $('#expPreview').innerHTML = r.extension === 'json' ? highlightJson(r.content) : esc(r.content);
    if (!$('#expName').value) $('#expName').value = meta().name;
  }
  async function renderDeployments() {
    try {
      var d = (await api('/api/deployments')).deployments;
      $('#depHistory').innerHTML = '<tr><th class="l">Strategy</th><th class="l">Env</th><th class="l">Deployment</th><th class="l">When</th><th class="l">Status</th></tr>' +
        (d.length ? d.slice(0, 12).map(function (x) {
          return '<tr><td class="l">' + esc(x.strategy_name) + ' v' + x.version + '</td><td class="l">' + esc(x.environment) + '</td><td class="l">' +
            esc(x.deployment_id) + '</td><td class="l">' + esc((x.deployed_at || '').replace('T', ' ').slice(0, 16)) + '</td><td class="l"><span class="sim">' +
            esc(x.status) + '</span></td></tr>';
        }).join('') : '<tr><td class="l hint" colspan="5">No deployments yet.</td></tr>');
    } catch (e) { $('#depHistory').innerHTML = '<tr><td class="l">⚠ ' + esc(e.message) + '</td></tr>'; }
  }
  async function deploy(btn) {
    await busy(btn, async function () {
      var r = await api('/api/deploy', Object.assign({ tree: S.tree, meta: meta(), environment: $('#depEnv').value,
                                                        file_name: $('#expName').value }, settings()));
      $('#receipt').innerHTML = '<div class="receipt"><div><b>Status</b><span class="sim">' + esc(r.status) + '</span> ' + esc(r.target) + '</div>' +
        '<div><b>Deployment ID</b>' + esc(r.deployment_id) + '</div><div><b>Strategy</b>' + esc(r.strategy_name) + ' v' + r.version +
        ' (' + esc(r.strategy_id) + ')</div><div><b>Environment</b>' + esc(r.environment) + '</div><div><b>Package</b>' + esc(r.package) +
        '</div><div><b>Checksum</b>' + esc(String(r.checksum).slice(0, 23)) + '…</div><div><b>Approval / bad rate</b>' +
        pct(r.kpis.approval_rate) + ' / ' + pct(r.kpis.bad_rate_approved, 2) + '</div><div class="hint" style="margin-top:4px">' + esc(r.note) + '</div></div>';
      await renderDeployments();
      toast('Deployment simulated: ' + r.deployment_id);
    });
  }

  // ── tabs ────────────────────────────────────────────────────────
  async function showTab(t) {
    S.tab = t;
    $$('.tabs button').forEach(function (b) { b.classList.toggle('active', b.dataset.tab === t); });
    $$('.tab').forEach(function (x) { x.hidden = x.id !== 'tab-' + t; });
    if (t === 'backtest') renderBacktest();
    if (t === 'export') { await busy(null, renderExportPreview); await renderDeployments(); }
  }

  // ── wiring ──────────────────────────────────────────────────────
  function on(sel, ev, fn) { $(sel).addEventListener(ev, fn); }
  on('#ds', 'change', function () { busy(null, onDataset); });
  on('#btnLoad', 'click', function () { loadPopulation(this); });
  ['#margin', '#loss', '#baseline'].forEach(function (s) { on(s, 'change', function () { if (S.tree) changed(); }); });
  on('#btnNew', 'click', function () { $('#newMenu').hidden = !$('#newMenu').hidden; });
  on('#newMenu', 'click', function (e) {
    var t = e.target.dataset.tpl; if (!t) return;
    $('#newMenu').hidden = true;
    if (!S.pop) return toast('Load a population first.', true);
    busy(null, function () { return newFromTemplate(t); });
  });
  on('#stratList', 'click', function (e) { var el = e.target.closest('.st'); if (el) openStrategy(el.dataset.file); });
  on('#treeWrap', 'click', function (e) {
    var g = e.target.closest('.tn'); if (!g) return;
    S.selected = g.dataset.id; renderTree(); renderEditor();
  });
  on('#issues', 'click', function (e) {
    var el = e.target.closest('.issue'); if (!el || !find(el.dataset.node)) return;
    S.selected = el.dataset.node; showTab('tree'); renderTree(); renderEditor();
  });
  on('#editor', 'change', onEdit);
  on('#editor', 'click', onEditorAction);
  ['#stName', '#stId', '#stStatus'].forEach(function (s) { on(s, 'change', function () { if (S.tree) busy(null, renderJson); }); });
  on('#btnValidate', 'click', function () {
    busy(this, async function () {
      await refresh();
      var errs = (S.ev.issues || []).filter(function (i) { return i.level === 'error'; }).length;
      var warns = (S.ev.issues || []).length - errs;
      toast(errs ? errs + ' error(s) to fix' : 'Strategy is valid' + (warns ? ' (' + warns + ' warning(s))' : ''), !!errs);
    });
  });
  on('#btnSave', 'click', function () { saveStrategy(this, false); });
  on('#btnSaveVersion', 'click', function () { saveStrategy(this, true); });
  on('#btnRunBacktest', 'click', function () { busy(this, async function () { await refresh(); await showTab('backtest'); }); });
  on('#btnGoExport', 'click', function () { showTab('export'); });
  $$('.tabs button').forEach(function (b) { b.addEventListener('click', function () { showTab(b.dataset.tab); }); });
  $$('.json-tabs button').forEach(function (b) {
    b.addEventListener('click', function () {
      S.jt = b.dataset.jt;
      $$('.json-tabs button').forEach(function (x) { x.classList.toggle('active', x === b); });
      var adv = S.jt === 'adv';
      $('#jsonGen').hidden = adv; $('#jsonAdv').hidden = !adv; $('#btnApplyAdv').hidden = !adv; $('#btnCopyJson').hidden = adv;
      if (adv) {
        busy(null, async function () {
          var r = await api('/api/export', Object.assign({ tree: S.tree, meta: meta(), format: 'generic' }, settings()));
          $('#jsonAdv').value = r.content;
          $('#jsonNote').textContent = 'Edit the tree as JSON (generic or InterConnect format), then apply.';
        });
      } else busy(null, renderJson);
    });
  });
  on('#jsonFormat', 'change', function () { busy(null, renderJson); });
  on('#btnCopyJson', 'click', function () {
    var text = $('#jsonGen').textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
      function () { toast('JSON copied'); }, function () { toast('Select the JSON and copy it manually', true); });
  });
  on('#btnApplyAdv', 'click', function () {
    busy(this, async function () {
      var obj;
      try { obj = JSON.parse($('#jsonAdv').value); } catch (e) { throw new Error('Invalid JSON: ' + e.message); }
      var r = await api('/api/import', obj);
      S.tree = r.tree; S.selected = r.tree.id;
      if (obj.meta) setMeta(Object.assign({}, S.meta, obj.meta));
      renderEditor(); changed(); toast('Advanced JSON applied');
    });
  });
  on('#expFormat', 'change', function () { busy(null, renderExportPreview); });
  on('#btnSaveExport', 'click', function () {
    busy(this, async function () {
      await renderExportPreview();
      var r = await api('/api/save_export', { meta: meta(), format: $('#expFormat').value, file_name: $('#expName').value, content: S.lastExport.content });
      $('#expNote').textContent = 'Saved to ' + r.folder + r.file;
      toast('Export saved');
    });
  });
  on('#btnDownload', 'click', function () {
    busy(this, async function () {
      await renderExportPreview();
      var a = document.createElement('a');
      var name = ($('#expName').value || 'strategy').replace(/[^A-Za-z0-9_.-]+/g, '_') + '.' + $('#expFormat').value + '.' + S.lastExport.extension;
      a.href = URL.createObjectURL(new Blob([S.lastExport.content], { type: 'text/plain' })); a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
    });
  });
  on('#btnDeploy', 'click', function () { deploy(this); });
  on('#btnCompare', 'click', function () { compare(this); });
  on('#btnWriteDs', 'click', function () {
    busy(this, async function () {
      $('#writeNote').textContent = 'Writing…';
      var r = await api('/api/write_dataset', { tree: S.tree, meta: meta(), name: $('#outName').value.trim() });
      var d = r.decisions || {};
      $('#writeNote').innerHTML = (r.created ? 'Created' : 'Overwrote') + ' <b>' + esc(r.dataset) + '</b> on ' + esc(r.connection) + ': ' +
        num(r.n_rows) + ' rows · approve ' + num(d.APPROVE || 0) + ' · refer ' + num(d.REFER || 0) + ' · decline ' + num(d.DECLINE || 0) + '.';
      toast('Decisioned dataset written');
    });
  });

  // ── start ───────────────────────────────────────────────────────
  busy(null, async function () {
    var ds = (await api('/api/datasets')).datasets;
    var pref = ds.filter(function (d) { return /_scorecard$/.test(d); })[0] || ds[0];
    options($('#ds'), ds, pref);
    await onDataset();
    renderEditor();
  });
})();
