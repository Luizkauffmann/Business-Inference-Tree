(function () {
  'use strict';
  // ══════════════════════════════════════════════════════════════════
  // Strategy Tree Builder – D3 tree canvas, virtualised attribute ranking,
  // optbinning splits, node calculations and configurable node metrics.
  // ══════════════════════════════════════════════════════════════════
  var $ = function (s) { return document.querySelector('.tb ' + s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll('.tb ' + s)); };
  var CARD_W = 210, ROW_H = 30, LS_KEY = 'tb_display_v1';
  var D3_URLS = ['https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js', 'https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js'];
  var S = {
    loaded: false, cfg: null, columns: [], colByName: {}, tree: null, metrics: {}, summary: null, labels: {},
    selected: null, collapsed: {}, undo: [], rel: null, relView: [], relSel: null, draft: null, tab: 'split',
    display: { metrics: ['n', 'share', 'bad_rate', 'pd', 'gini', 'entropy', 'dominant'], custom_metrics: [] }, pd: null,
    configs: [], models: [], transform: null, layout: null, evalSeq: 0, functions: {}
  };

  // ── utilities ──────────────────────────────────────────────────────
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
  function pct(x, d) { return x == null || isNaN(x) ? '–' : (100 * x).toFixed(d == null ? 1 : d) + '%'; }
  function num(x, d) { return x == null || isNaN(x) ? '–' : Number(x).toLocaleString('en-US', { maximumFractionDigits: d == null ? 0 : d }); }
  function sig(x) { return x == null || isNaN(x) ? '–' : Math.abs(x) >= 1000 ? num(x) : Number(x).toPrecision(3).replace(/\.?0+$/, ''); }
  function money(x) { return x == null || isNaN(x) ? '–' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 }); }
  function debounce(fn, ms) { var t; return function () { var a = arguments, me = this; clearTimeout(t); t = setTimeout(function () { fn.apply(me, a); }, ms); }; }
  var toastT;
  function toast(msg, err) {
    var t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(function () { t.hidden = true; }, err ? 7000 : 3500);
  }
  function busy(btn, on) { if (btn) btn.disabled = !!on; }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function lsGet() { try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) { return null; } }
  function lsSet(v) { try { localStorage.setItem(LS_KEY, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function loadScript(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = rej; document.head.appendChild(s);
    });
  }
  async function ensureD3() {
    if (window.d3 && d3.tree) return;
    for (var i = 0; i < D3_URLS.length; i++) {
      try { await loadScript(D3_URLS[i]); if (window.d3 && d3.tree) return; } catch (e) { /* next */ }
    }
    throw new Error('Could not load D3 (cdnjs / jsdelivr blocked?).');
  }

  // ── tree helpers (client side) ─────────────────────────────────────
  function walk(n, fn, depth, parent, idx) {
    fn(n, depth || 0, parent || null, idx == null ? null : idx);
    (n.children || []).forEach(function (c, i) { walk(c, fn, (depth || 0) + 1, n, i); });
  }
  function findNode(id) { var f = null; walk(S.tree, function (n) { if (n.id === id) f = n; }); return f; }
  function parentOf(id) { var f = null; walk(S.tree, function (n, d, p, i) { if (n.id === id) f = { p: p, i: i }; }); return f; }
  function nextId() { var m = 0; walk(S.tree, function (n) { var k = /^n(\d+)$/.exec(n.id); if (k) m = Math.max(m, +k[1]); }); return m; }
  function newLeaf(id, name, tmpl) {
    var lf = tmpl ? clone(tmpl) : { mode: 'cutoff', score_expr: S.cfg && S.cfg.score_col || '', approve_at: null, refer_at: null, reason_codes: [], pricing_tier: '', limit_expr: '', outputs: [] };
    return { id: id, name: name, calcs: [], split: null, children: [], leaf: lf };
  }
  function nBranches(sp) {
    var n = sp.kind === 'numeric' ? (sp.cuts || []).length + 1 : sp.kind === 'categorical' ? (sp.groups || []).length + (sp.other_separate ? 1 : 0) : (sp.rules || []).length + 1;
    return n + (sp.kind !== 'rule' && sp.missing_separate ? 1 : 0);
  }
  function firstLeafOf(n) { var f = null; walk(n, function (x) { if (!f && !x.split && x.leaf) f = x.leaf; }); return f; }
  function commit(mutate, keepSel) {
    S.undo.push(JSON.stringify(S.tree)); if (S.undo.length > 60) S.undo.shift();
    mutate(S.tree);
    if (!keepSel && S.selected && !findNode(S.selected)) S.selected = S.tree.id;
    S.rel = null;
    evaluate();
  }

  // ── init ───────────────────────────────────────────────────────────
  async function init() {
    var saved = lsGet(); if (saved && saved.metrics) S.display = saved;
    try {
      var r = await api('/api/datasets');
      $('#ds').innerHTML = r.datasets.map(function (d) { return '<option>' + esc(d) + '</option>'; }).join('');
      var pref = ['applications_demo_enriched', 'applications_joined_scored'];
      for (var i = 0; i < pref.length; i++) if (r.datasets.indexOf(pref[i]) >= 0) { $('#ds').value = pref[i]; break; }
      await onDataset();
    } catch (e) { toast(e.message, true); }
    ensureD3().catch(function (e) { toast(e.message, true); });
  }
  async function onDataset() {
    var r = await api('/api/columns?dataset=' + encodeURIComponent($('#ds').value));
    var cols = r.columns, opts = '<option value="">—</option>' + cols.map(function (c) { return '<option>' + esc(c) + '</option>'; }).join('');
    $('#target').innerHTML = opts; $('#scoreCol').innerHTML = opts;
    $('#target').value = ['bad_12m_flag_demo', 'bad_12m_flag'].filter(function (c) { return cols.indexOf(c) >= 0; })[0] ||
      cols.filter(function (c) { return /bad|default|target/i.test(c); })[0] || '';
    $('#scoreCol').value = ['policy_score_existing', 'score'].filter(function (c) { return cols.indexOf(c) >= 0; })[0] ||
      cols.filter(function (c) { return /score/i.test(c); })[0] || '';
  }

  async function loadPopulation() {
    var btn = $('#btnLoad'); busy(btn, true); btn.textContent = 'Loading…';
    try {
      await ensureD3();
      var r = await api('/api/load', { dataset: $('#ds').value, target: $('#target').value || null, score_col: $('#scoreCol').value || null,
        margin: $('#margin').value, loss: $('#loss').value, sample_rows: $('#sampleRows').value || null });
      S.loaded = true; S.cfg = r.cfg; S.columns = r.columns; S.colByName = {}; S.functions = r.functions || {};
      r.columns.forEach(function (c) { S.colByName[c.name] = c; });
      S.metricLabels = r.metric_labels;
      S.tree = r.starter; S.selected = S.tree.id; S.collapsed = {}; S.undo = []; S.rel = null;
      $('#popInfo').textContent = num(r.n) + ' rows · ' + (r.n_known != null ? num(r.n_known) + ' with outcome · event rate ' + pct(r.bad_rate) : 'no target') +
        (r.prebin_rows < r.n ? ' · relevance on a ' + num(r.prebin_rows) + '-row sample' : '') +
        (r.cfg.sample_rows ? ' · random sample loaded (use Backtest in database for the full table)' : ' · all rows in memory');
      $('#outName').placeholder = S.cfg.dataset + '_tree';
      $('#emptyMsg').hidden = true;
      $('#fnHelp').innerHTML = Object.keys(S.functions).map(function (k) { return '<code>' + esc(k) + '</code> ' + esc(S.functions[k]); }).join('<br>');
      setupCanvas(); $('#relSearch').value = ''; setTab('split'); pdInputs(S.tree.pd_settings);
      await evaluate(true);
      refreshLists();
    } catch (e) { toast(e.message, true); }
    finally { busy(btn, false); btn.textContent = 'Load population'; }
  }
  async function refreshLists() {
    api('/api/trees').then(function (r) {
      $('#savedSel').innerHTML = '<option value="">—</option>' + r.trees.map(function (t) {
        return '<option value="' + esc(t.path) + '">' + esc(t.name) + ' v' + esc(t.version) + ' · ' + esc(t.status || '') + '</option>';
      }).join('');
    }).catch(function () { });
    api('/api/vml_models').then(function (r) {
      S.models = r.models;
      $('#vmlSel').innerHTML = r.models.length ? r.models.map(function (m, i) {
        return '<option value="' + i + '">' + esc(m.label) + (m.algorithm === 'RANDOM_FOREST_CLASSIFICATION' ? ' (forest: tree 1)' : '') + '</option>';
      }).join('') : '<option value="">No decision-tree models found</option>';
    }).catch(function () { $('#vmlSel').innerHTML = '<option value="">Visual ML listing unavailable</option>'; });
    api('/api/binning_configs').then(function (r) {
      S.configs = r.configs;
      $('#cfgSel').innerHTML = '<option value="">—</option>' + r.configs.map(function (c, i) {
        return '<option value="' + i + '">' + esc(c.name) + ' (' + c.variables.length + ' vars)</option>';
      }).join('');
    }).catch(function () { });
  }

  // ── evaluation ─────────────────────────────────────────────────────
  async function evaluate(fit) {
    var seq = ++S.evalSeq;
    try {
      var r = await api('/api/evaluate', { tree: S.tree, display: S.display });
      if (seq !== S.evalSeq) return;
      showIssues(r.issues);
      if (r.metrics) { S.metrics = r.metrics; S.summary = r.summary; S.pd = r.pd || null; }
      renderPdStatus();
      renderKpis(); render(fit); renderPanel();
      if (S.tab === 'split' && (!S.rel || S.rel.node_id !== S.selected) && r.metrics) loadRelevance();
      guidance();
    } catch (e) { toast(e.message, true); }
  }
  function showIssues(list) {
    var box = $('#issues');
    if (!list || !list.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = list.map(function (i) {
      return '<div class="' + (i.level === 'error' ? 'err' : 'warn') + '">' + (i.level === 'error' ? '✖ ' : '⚠ ') + '<b>' + esc(i.node_id) + '</b> ' + esc(i.message) + '</div>';
    }).join('');
  }
  function renderKpis() {
    var s = S.summary || {};
    var k = [['Approval', pct(s.approval_rate)], ['Refer', pct(s.refer_rate)], ['Decline', pct(s.decline_rate)],
      ['Event rate approved', pct(s.bad_rate_approved, 2)], ['Profit', money(s.profit)], ['Leaves', num(s.leaves)]];
    var rootPd = S.tree && S.metrics[S.tree.id] ? S.metrics[S.tree.id].pd : null;
    if (rootPd != null) k.splice(4, 0, ['Avg predicted event rate', pct(rootPd, 2)]);
    if (s.no_decision_rate) k.splice(3, 0, ['No decision (segment)', pct(s.no_decision_rate)]);
    $('#kpis').innerHTML = k.map(function (x) { return '<div class="kpi"><div class="v">' + x[1] + '</div><div class="l">' + x[0] + '</div></div>'; }).join('');
  }
  var guidance = debounce(async function () {
    try {
      var r = await api('/api/guidance', { tree: S.tree });
      var g = $('#guide');
      if (!r.steps.length) { g.hidden = true; return; }
      g.hidden = false;
      g.innerHTML = '<b>Guided next splits:</b>' + r.steps.map(function (s, i) {
        return '<span class="step" data-node="' + esc(s.node_id) + '" data-var="' + esc(s.variable) + '" title="IV ' + s.iv.toFixed(3) +
          ' · event rate ' + pct(s.bad_rate) + ' · also: ' + esc(s.runners_up.join(', ')) + '">' + (i + 1) + '. ' + esc(s.name) + ' → <b>' + esc(s.variable) +
          '</b> (+' + pct(s.gain, 0) + ' info)</span>';
      }).join('');
      $$('#guide .step').forEach(function (el) {
        el.onclick = function () { select(el.getAttribute('data-node'), 'split'); S.pendingVar = el.getAttribute('data-var'); loadRelevance(); };
      });
    } catch (e) { $('#guide').hidden = true; }
  }, 600);

  // ── D3 canvas: layout + viewport culling + collapsible subtrees ────
  var svg, gRoot, gLinks, gLabels, gNodes, zoom, rafPending = false;
  function setupCanvas() {
    if (svg) return;
    svg = d3.select('.tb #svg');
    gRoot = svg.append('g'); gLinks = gRoot.append('g'); gLabels = gRoot.append('g'); gNodes = gRoot.append('g');
    zoom = d3.zoom().scaleExtent([0.08, 2.5]).on('zoom', function (e) {
      gRoot.attr('transform', e.transform); S.transform = e.transform; scheduleCull();
    });
    svg.call(zoom).on('dblclick.zoom', null);
    window.addEventListener('resize', debounce(scheduleCull, 150));
  }
  function metricRows() {
    var rows = S.display.metrics.filter(function (k) { return k !== 'split'; });
    return rows.concat((S.display.custom_metrics || []).map(function (c) { return 'cm_' + c.name; }));
  }
  function cardH() { return 46 + metricRows().length * 15 + 22; }
  function fmtMetric(k, v) {
    if (v == null) return '–';
    if (/^(share|bad_rate|good_rate|dominant|approval_rate|split_gain)$/.test(k)) return pct(v);
    if (k === 'pd') return pct(v, 2);
    if (k === 'n' || k === 'n_known' || k === 'bads') return num(v);
    if (k === 'gini' || k === 'entropy' || k === 'split_iv') return Number(v).toFixed(3);
    if (k === 'profit') return money(v);
    if (k === 'avg_score') return num(v, 1);
    return sig(v);
  }
  function metricLabel(k) {
    if (k.indexOf('cm_') === 0) return k.slice(3);
    return (S.metricLabels || {})[k] || k;
  }
  function render(fit) {
    if (!svg || !S.tree) return;
    var root = d3.hierarchy(S.tree, function (d) { return S.collapsed[d.id] ? null : (d.children && d.children.length ? d.children : null); });
    var h = cardH();
    d3.tree().nodeSize([CARD_W + 34, h + 76]).separation(function (a, b) { return a.parent === b.parent ? 1 : 1.15; })(root);
    S.layout = root;
    if (fit || !S.transform) fitView(); else cull();
  }
  function fitView() {
    if (!S.layout) return;
    var ns = S.layout.descendants(), x0 = d3.min(ns, function (d) { return d.x; }) - CARD_W / 2 - 20, x1 = d3.max(ns, function (d) { return d.x; }) + CARD_W / 2 + 20;
    var y1 = d3.max(ns, function (d) { return d.y; }) + cardH() + 20;
    var el = $('#canvas'), W = el.clientWidth, H = el.clientHeight;
    var k = Math.min(1.1, Math.max(0.08, Math.min(W / (x1 - x0), H / (y1 + 40))));
    var t = d3.zoomIdentity.translate(W / 2 - k * (x0 + x1) / 2, 24).scale(k);
    svg.call(zoom.transform, t);
  }
  function scheduleCull() {
    if (rafPending) return; rafPending = true;
    requestAnimationFrame(function () { rafPending = false; cull(); });
  }
  function cull() {
    if (!S.layout) return;
    var t = S.transform || d3.zoomIdentity, el = $('#canvas'), W = el.clientWidth, H = el.clientHeight, h = cardH(), m = 160;
    var vx0 = (-t.x - m) / t.k, vx1 = (W - t.x + m) / t.k, vy0 = (-t.y - m) / t.k, vy1 = (H - t.y + m) / t.k;
    function vis(d) { return d.x + CARD_W / 2 >= vx0 && d.x - CARD_W / 2 <= vx1 && d.y + h >= vy0 && d.y <= vy1; }
    var nodes = S.layout.descendants().filter(vis);
    var links = S.layout.links().filter(function (l) { return vis(l.source) || vis(l.target); });
    var lowDetail = t.k < 0.35;
    gLinks.selectAll('path.link').data(links, function (d) { return d.target.data.id; }).join('path').attr('class', 'link')
      .attr('d', function (d) {
        var sy = d.source.y + h, ty = d.target.y, my = (sy + ty) / 2;
        return 'M' + d.source.x + ',' + sy + 'V' + my + 'H' + d.target.x + 'V' + ty;
      });
    var lab = gLabels.selectAll('g.elabel').data(lowDetail ? [] : links, function (d) { return d.target.data.id; })
      .join(function (en) { var g = en.append('g').attr('class', 'elabel'); g.append('rect'); g.append('text'); return g; });
    lab.each(function (d) {
      var mt = S.metrics[d.source.data.id] || {}, idx = d.source.data.children.indexOf(d.target.data);
      var txt = (mt.split && mt.split[idx]) || ('branch ' + (idx + 1));
      if (txt.length > 34) txt = txt.slice(0, 33) + '…';
      var g = d3.select(this), y = (d.source.y + h + d.target.y) / 2 + 12;
      g.select('text').attr('x', d.target.x).attr('y', y + 4).attr('text-anchor', 'middle').text(txt);
      var w = txt.length * 5.4 + 10;
      g.select('rect').attr('x', d.target.x - w / 2).attr('y', y - 7).attr('width', w).attr('height', 15).attr('rx', 7);
    });
    var sel = gNodes.selectAll('g.node').data(nodes, function (d) { return d.data.id; })
      .join(function (en) {
        var g = en.append('g').on('click', function (e, d) { select(d.data.id); });
        return g;
      });
    sel.attr('transform', function (d) { return 'translate(' + (d.x - CARD_W / 2) + ',' + d.y + ')'; })
      .attr('class', function (d) { return 'node' + (d.data.split ? '' : ' leaf') + (d.data.id === S.selected ? ' sel' : ''); })
      .each(function (d) { drawCard(d3.select(this), d, h, lowDetail); });
  }
  function riskColor(br) {
    var root = (S.metrics[S.tree.id] || {}).bad_rate;
    if (br == null || root == null) return '#9aa7b5';
    var r = br / Math.max(root, 1e-6);
    return r < 0.6 ? '#1e8449' : r < 0.9 ? '#7cb342' : r < 1.2 ? '#c9a227' : r < 2 ? '#e67e22' : '#c0392b';
  }
  function decisionChip(n) {
    if (n.split) return null;
    var lf = n.leaf || {}, m = lf.mode || 'cutoff';
    if (m === 'cutoff') return { t: '≥ ' + (lf.approve_at == null ? '?' : lf.approve_at) + (lf.refer_at != null ? ' / R ' + lf.refer_at : ''), c: '#1a6fc4' };
    if (m === 'none') return { t: 'SEGMENT', c: '#6b7684' };
    if (m === 'output') return { t: '= ' + (lf.decision_output || '?'), c: '#7b4bb7' };
    return { t: m.toUpperCase(), c: m === 'approve' ? '#1e8449' : m === 'refer' ? '#c27c0e' : '#c0392b' };
  }
  function drawCard(g, d, h, low) {
    var n = d.data, mt = S.metrics[n.id] || {}, key = JSON.stringify([n.name, n.split && n.split.variable, n.leaf && [n.leaf.mode, n.leaf.approve_at, n.leaf.refer_at, n.leaf.decision_output],
      S.collapsed[n.id], low, mt.n, mt.bad_rate, metricRows().join(','), (n.calcs || []).length, n.children && n.children.length]);
    if (g.attr('data-k') === key) return;   // unchanged → no DOM work
    g.attr('data-k', key); g.selectAll('*').remove();
    g.append('rect').attr('class', 'card').attr('width', CARD_W).attr('height', h);
    g.append('rect').attr('class', 'riskbar').attr('x', 0).attr('y', 0).attr('width', 5).attr('height', h).attr('fill', riskColor(mt.bad_rate));
    var title = n.name || n.id; if (title.length > 30) title = title.slice(0, 29) + '…';
    g.append('text').attr('class', 'title').attr('x', 12).attr('y', 17).text(title);
    var sub = n.split ? (n.split.kind === 'rule' ? 'rules → ' + nBranches(n.split) + ' branches' : n.split.variable + ' → ' + nBranches(n.split) + ' branches')
      : 'leaf · ' + n.id;
    if ((n.calcs || []).length) sub += ' · ƒ' + n.calcs.length;
    g.append('text').attr('class', 'mk').attr('x', 12).attr('y', 32).text(sub.length > 40 ? sub.slice(0, 39) + '…' : sub);
    if (!low) {
      metricRows().forEach(function (k, i) {
        var y = 50 + i * 15;
        g.append('text').attr('class', 'mk').attr('x', 12).attr('y', y).text(metricLabel(k));
        g.append('text').attr('class', 'mv').attr('x', CARD_W - 10).attr('y', y).text(fmtMetric(k, mt[k]));
      });
    }
    var chip = decisionChip(n);
    if (chip) {
      var c = g.append('g').attr('class', 'chip');
      var w = chip.t.length * 6.2 + 14;
      c.append('rect').attr('x', 12).attr('y', h - 19).attr('width', w).attr('height', 15).attr('rx', 7).attr('fill', chip.c);
      c.append('text').attr('x', 12 + w / 2).attr('y', h - 8).attr('text-anchor', 'middle').text(chip.t);
    }
    if (n.children && n.children.length) {
      var tg = g.append('g').attr('class', 'tog').attr('transform', 'translate(' + (CARD_W / 2) + ',' + (h + 1) + ')')
        .on('click', function (e) { e.stopPropagation(); S.collapsed[n.id] = !S.collapsed[n.id]; render(); });
      tg.append('circle').attr('r', 8);
      var hidden = 0; if (S.collapsed[n.id]) walk(n, function () { hidden++; });
      tg.append('text').attr('y', 4).text(S.collapsed[n.id] ? '+' : '−');
      if (S.collapsed[n.id]) g.append('text').attr('class', 'mk').attr('x', CARD_W / 2 + 12).attr('y', h + 5).text((hidden - 1) + ' hidden');
    }
  }

  // ── selection + panel ──────────────────────────────────────────────
  function select(id, tab) {
    S.selected = id; S.draft = null; S.relSel = null;
    if (tab) setTab(tab);
    gNodes && gNodes.selectAll('g.node').attr('class', function (d) { return 'node' + (d.data.split ? '' : ' leaf') + (d.data.id === S.selected ? ' sel' : ''); });
    renderPanel();
    if (S.tab === 'split') loadRelevance();
  }
  function setTab(t) {
    S.tab = t;
    $$('.tabs button').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-tab') === t); });
    ['split', 'logic', 'metrics'].forEach(function (x) { $('#tab-' + x).hidden = x !== t; });
    if (t === 'split' && S.loaded && (!S.rel || S.rel.node_id !== S.selected)) loadRelevance();
  }
  function renderPanel() {
    if (!S.tree) return;
    var n = findNode(S.selected) || S.tree; S.selected = n.id;
    var mt = S.metrics[n.id] || {};
    $('#nodeName').value = n.name || '';
    $('#nodeSub').textContent = n.id + ' · ' + num(mt.n) + ' rows · event rate ' + pct(mt.bad_rate, 2) + (n.split ? ' · split node' : ' · leaf');
    $('#btnPrune').hidden = !n.split;
    $('#btnEditSplit').hidden = !n.split;
    renderLogic(n); renderNodeMetrics(n);
    if (!S.draft) $('#splitBox').hidden = true;
  }
  function renderNodeMetrics(n) {
    var mt = S.metrics[n.id] || {};
    var keys = Object.keys(S.metricLabels || {}).filter(function (k) { return k !== 'split'; }).concat(
      (S.display.custom_metrics || []).map(function (c) { return 'cm_' + c.name; }));
    var rows = keys.map(function (k) { return '<tr><td>' + esc(metricLabel(k)) + '</td><td class="n">' + fmtMetric(k, mt[k]) + '</td></tr>'; });
    if (mt.split) rows.push('<tr><td>Split values</td><td>' + mt.split.map(esc).join('<br>') + '</td></tr>');
    if (mt.dominant_class) rows.push('<tr><td>Dominant class</td><td class="n">' + esc(mt.dominant_class) + '</td></tr>');
    (mt.errors || []).forEach(function (e) { rows.push('<tr><td colspan="2" style="color:#c0392b">' + esc(e) + '</td></tr>'); });
    $('#nodeMetrics').innerHTML = rows.join('');
  }

  // ── relevance list (virtualised + debounced search) ────────────────
  async function loadRelevance() {
    if (!S.loaded || !S.cfg.target) { $('#relInfo').textContent = S.loaded ? 'Choose a target to rank attributes.' : ''; return; }
    var id = S.selected;
    $('#relInfo').textContent = 'Ranking attributes…';
    try {
      var r = await api('/api/relevance', { tree: S.tree, node_id: id });
      if (id !== S.selected) return;
      S.rel = r; filterRel();
      $('#relInfo').textContent = r.attributes.length + ' attributes · relevance = information gain vs the best attribute (100%) on ' +
        num(r.n_known) + ' rows with outcome' + (r.sampled ? ' (sample)' : '') + '. Click one for the optimal split.';
      if (S.pendingVar) { var v = S.pendingVar; S.pendingVar = null; pickVariable(v, true); }
    } catch (e) { $('#relInfo').textContent = e.message; }
  }
  function filterRel() {
    if (!S.rel) { S.relView = []; drawRel(); return; }
    var q = ($('#relSearch').value || '').toLowerCase().trim(), hide = $('#relHide').checked;
    var hideFlags = ['outcome_window', 'id_like', 'constant', 'decision_output', 'date', 'suspicious'];
    S.relView = S.rel.attributes.filter(function (a) {
      if (q && a.variable.toLowerCase().indexOf(q) < 0) return false;
      if (hide && a.flags.some(function (f) { return hideFlags.indexOf(f) >= 0; })) return false;
      return true;
    });
    $('#relList').scrollTop = 0; drawRel();
  }
  function drawRel() {
    var box = $('#relList'), H = box.clientHeight || 290, top = box.scrollTop;
    var first = Math.max(0, Math.floor(top / ROW_H) - 5), last = Math.min(S.relView.length, Math.ceil((top + H) / ROW_H) + 5);
    $('#relSpacer').style.height = (S.relView.length * ROW_H) + 'px';
    var rows = $('#relRows'); rows.style.transform = 'translateY(' + (first * ROW_H) + 'px)';
    var html = [];
    for (var i = first; i < last; i++) {
      var a = S.relView[i];
      html.push('<div class="rrow' + (a.variable === S.relSel ? ' on' : '') + '" data-v="' + esc(a.variable) + '" title="' + esc(a.variable) +
        ' · ' + a.kind + ' · gain ' + pct(a.gain, 1) + ' of node entropy' + (a.flags.length ? ' · ' + esc(a.flags.join(', ')) : '') + '">' +
        '<div class="nm">' + esc(a.variable) + (a.flags.length ? '<span class="fl">' + esc(a.flags.join(' ')) + '</span>' : '') + '</div>' +
        '<div class="bar"><span style="width:' + Math.max(1, a.relevance).toFixed(1) + '%"></span><em>' + a.relevance.toFixed(0) + '%</em></div>' +
        '<div class="iv">' + a.iv.toFixed(2) + '</div></div>');
    }
    rows.innerHTML = html.join('');
  }

  // ── split suggestion / editing ─────────────────────────────────────
  function pickVariable(v, suggest) {
    S.relSel = v; drawRel(); S.draftHist = [];
    var c = S.colByName[v];
    S.draft = { kind: c && c.kind === 'categorical' ? 'categorical' : 'numeric', variable: v, cuts: [], groups: [], rules: [], missing_branch: 0 };
    var cfg = currentConfig();
    $('#btnFromCfg').hidden = !(cfg && cfg.variables.indexOf(v) >= 0);
    if (cfg) $('#btnFromCfg').textContent = 'Use saved binning (' + cfg.name + ')';
    $('#splitBox').hidden = false; $('#sbTitle').textContent = 'Split on ' + v; $('#sbSource').textContent = '';
    $('#sbPreview').innerHTML = '';
    fillDraftForm();
    if (suggest) suggestSplit();
  }
  function currentConfig() { var i = $('#cfgSel').value; return i === '' ? null : S.configs[+i]; }
  async function suggestSplit() {
    if (!S.draft || !S.draft.variable) return;
    var btn = $('#btnSuggest'); busy(btn, true); btn.textContent = 'Optimising…';
    try {
      var r = await api('/api/suggest', { tree: S.tree, node_id: S.selected, variable: S.draft.variable,
        kind: S.draft.kind === 'rule' ? null : S.draft.kind, max_bins: +$('#sbBins').value || 4,
        min_share: (+$('#sbMin').value || 5) / 100, monotonic: $('#sbTrend').value, missing: $('#sbMiss').value });
      S.draft = r.split; $('#sbSource').textContent = r.split.method || r.split.source;
      fillDraftForm(); showPreview(r.preview, r.labels);
    } catch (e) { toast(e.message, true); }
    finally { busy(btn, false); btn.textContent = 'Optimal split (optbinning)'; }
  }
  async function splitFromConfig() {
    var cfg = currentConfig(); if (!cfg || !S.draft) return;
    try {
      var r = await api('/api/split_from_config', { tree: S.tree, node_id: S.selected, path: cfg.path, variable: S.draft.variable });
      S.draft = r.split; $('#sbSource').textContent = 'Binning v2: ' + cfg.name;
      fillDraftForm(); showPreview(r.preview, r.labels);
    } catch (e) { toast(e.message, true); }
  }
  function fillDraftForm() {
    var d = S.draft; if (!d) return;
    $('#sbKind').value = d.kind;
    $('#sbVar').value = d.variable || '';
    $('#sbCuts').value = (d.cuts || []).map(function (c) { return +(+c).toPrecision(8); }).join(', ');
    $('#sbGroups').value = (d.groups || []).map(function (g) { return g.join(', '); }).join('\n');
    $('#sbRules').value = (d.rules || []).join('\n');
    kindVisibility(); branchSelects();
  }
  function kindVisibility() {
    var k = $('#sbKind').value;
    $('#sbVarWrap').hidden = k === 'rule'; $('#sbCutsWrap').hidden = k !== 'numeric';
    $('#sbGroupsWrap').hidden = k !== 'categorical'; $('#sbRulesWrap').hidden = k !== 'rule';
    $('#sbMissWrap').hidden = k === 'rule'; $('#sbOtherWrap').hidden = k !== 'categorical';
  }
  function draftFromForm() {
    var k = $('#sbKind').value, d = { kind: k, source: (S.draft && S.draft.source) || 'manual' };
    if (k === 'rule') {
      d.rules = $('#sbRules').value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      return d;
    }
    d.variable = $('#sbVar').value.trim();
    if (k === 'numeric') d.cuts = $('#sbCuts').value.split(',').map(function (s) { return parseFloat(s); }).filter(function (x) { return !isNaN(x); });
    else d.groups = $('#sbGroups').value.split('\n').map(function (l) { return l.split(',').map(function (s) { return s.trim(); }).filter(Boolean); }).filter(function (g) { return g.length; });
    var mb = $('#sbMissBr').value;
    if (mb === 'sep') { d.missing_separate = true; } else { d.missing_branch = +mb || 0; }
    if (k === 'categorical') {
      var ob = $('#sbOtherBr').value;
      if (ob === 'sep') d.other_separate = true; else d.other_branch = +ob || 0;
    }
    return d;
  }
  function branchSelects() {
    var d = draftFromForm(), base = d.kind === 'numeric' ? (d.cuts || []).length + 1 : (d.groups || []).length;
    var opts = [];
    for (var i = 0; i < base; i++) opts.push('<option value="' + i + '">branch ' + (i + 1) + '</option>');
    var cur = S.draft || {};
    $('#sbMissBr').innerHTML = opts.join('') + '<option value="sep">own "Missing" branch</option>';
    $('#sbMissBr').value = cur.missing_separate ? 'sep' : String(Math.min(cur.missing_branch || 0, Math.max(0, base - 1)));
    $('#sbOtherBr').innerHTML = opts.join('') + '<option value="sep">own "Other" branch</option>';
    $('#sbOtherBr').value = cur.other_separate ? 'sep' : String(Math.min(cur.other_branch || 0, Math.max(0, base - 1)));
  }
  async function previewDraft() {
    var d = draftFromForm(); S.draft = d;
    try {
      var r = await api('/api/preview', { tree: S.tree, node_id: S.selected, split: d });
      showPreview(r.preview, r.labels);
    } catch (e) { toast(e.message, true); }
  }
  function showPreview(p, labels) {
    S.draftLabels = labels; S.bins = p.bins || null; S.binSel = [];
    S.binSummary = '<div class="hint" style="margin-top:4px">Split IV ' + (p.iv == null ? '–' : p.iv.toFixed(3)) +
      ' · information gain ' + pct(p.gain) + ' of the node entropy · ' + p.branches.length + ' branches</div>';
    renderBins();
  }

  // ── manual bin overrides (Binning v2-style) ────────────────────────
  function fv(x) { return x == null || isNaN(x) ? '–' : Number(x).toFixed(4); }
  // Edits change S.draft (cuts / groups / rules / missing routing) and re-run the preview.
  function binRows() { return S.bins ? S.bins.rows : []; }
  function woeCell(w) {
    if (w == null) return '<td class="n">–</td>';
    var c = Math.abs(w) < 0.05 ? 'var(--muted)' : w > 0 ? 'var(--green)' : 'var(--red)';
    return '<td class="n" style="color:' + c + ';font-weight:600">' + w.toFixed(4) + '</td>';
  }
  function trendCell(t) {
    return '<td class="tr ' + (t || '') + '">' + (t === 'up' ? '↑' : t === 'down' ? '↓' : '') + '</td>';
  }
  function binActions(r, i, rows) {
    var k = S.bins.kind, out = [];
    if (k === 'numeric' && i < rows.length - 1) out.push('<button class="ib" data-act="edge" data-i="' + i + '" title="Edit boundary with the next bin">✎</button>');
    if (k === 'categorical' && r.members) out.push('<button class="ib" data-act="members" data-i="' + i + '" title="Edit the categories of this group">✎</button>');
    if (k === 'rule' && i < rows.length - 1) out.push('<button class="ib" data-act="rule" data-i="' + i + '" title="Edit this rule">✎</button>');
    out.push('<button class="ib" data-act="split" data-i="' + i + '" title="' + (k === 'numeric' ? 'Split this bin in two' : k === 'categorical' ? 'Move categories to a new group' : 'Add a rule after this one') + '">⊕</button>');
    return out.join('');
  }
  function binTableHtml(full) {
    var b = S.bins, rows = binRows(), sel = S.binSel, k = b.kind;
    var h = ['<table class="tbl bins' + (full ? ' full' : '') + '"><tr><th></th><th>Label</th>' + (full ? '<th>Group</th>' : '') +
      '<th class="n">Count</th>' + (full ? '<th class="n">Events</th><th class="n">Non-events</th>' : '') +
      '<th class="n">Event rate</th><th class="n">WOE</th>' + (full ? '<th class="n">IV contrib</th>' : '') + '<th>Trend</th><th>Actions</th></tr>'];
    var m = b.missing;
    if (m && k !== 'rule') {
      var opts = rows.map(function (r, i) { return '<option value="' + i + '"' + (m.to === i ? ' selected' : '') + '>→ group ' + (i + 1) + '</option>'; }).join('');
      h.push('<tr class="miss"><td></td><td><i>Missing</i> <select class="missTo"><option value="sep"' + (m.to === 'separate' ? ' selected' : '') + '>own group</option>' + opts + '</select></td>' +
        (full ? '<td><i>' + (m.to === 'separate' ? rows.length + 1 : (m.to + 1)) + '</i></td>' : '') + '<td class="n"><i>' + num(m.n) + '</i></td>' +
        (full ? '<td class="n"><i>' + num(m.events) + '</i></td><td class="n"><i>' + num(m.non_events) + '</i></td>' : '') +
        '<td class="n"><i>' + pct(m.bad_rate) + '</i></td>' + woeCell(m.woe) + (full ? '<td class="n"><i>' + (m.iv_contrib == null ? '–' : m.iv_contrib.toFixed(4)) + '</i></td>' : '') + '<td></td><td></td></tr>');
    }
    rows.forEach(function (r, i) {
      var on = sel.indexOf(i) >= 0, lab = r.label + (k === 'categorical' && r.n_categories > 4 ? ' (' + r.n_categories + ' values)' : '');
      h.push('<tr class="bin' + (on ? ' on' : '') + '" data-i="' + i + '"><td><input type="checkbox" class="bcb"' + (on ? ' checked' : '') + '></td>' +
        '<td class="lab" title="' + esc(r.label) + '">' + esc(lab) + '</td>' + (full ? '<td>' + (i + 1) + '</td>' : '') + '<td class="n">' + num(r.n) + '</td>' +
        (full ? '<td class="n">' + num(r.events) + '</td><td class="n">' + num(r.non_events) + '</td>' : '') +
        '<td class="n">' + pct(r.bad_rate) + '</td>' + woeCell(r.woe) + (full ? '<td class="n">' + (r.iv_contrib == null ? '–' : r.iv_contrib.toFixed(4)) + '</td>' : '') +
        trendCell(r.trend) + '<td class="acts">' + binActions(r, i, rows) + '</td></tr>');
    });
    h.push('</table>');
    return h.join('');
  }
  function selBarHtml() {
    var sel = S.binSel.slice().sort(function (a, b) { return a - b; });
    if (sel.length < 2) return '<div class="hint selbar">Click a row to select · Shift/Ctrl+click for multi-select' + (S.draftHist && S.draftHist.length ? ' · <a href="#" class="bUndo">undo last bin edit</a>' : '') + '</div>';
    return '<div class="selbar on"><b>' + sel.length + ' bins selected (Groups ' + sel.map(function (i) { return i + 1; }).join(', ') + ')</b>' +
      '<button class="primary bMerge">⊕ Merge ' + sel.length + ' bins</button><button class="bClear">Clear selection</button></div>';
  }
  function renderBins() {
    var box = $('#sbPreview');
    if (!S.bins) { box.innerHTML = S.binSummary || ''; return; }
    box.innerHTML = '<div class="row-between"><b class="bt-title">Bin statistics</b><button class="mini bFull" title="Open the full table">⤢ Full table</button></div>' +
      selBarHtml() + '<div class="bins-wrap">' + binTableHtml(false) + '</div>' + S.binSummary;
    bindBins(box);
    if (!$('#binModal').hidden) renderBinModal();
  }
  function renderBinModal() {
    var box = $('#binModalBody');
    box.innerHTML = selBarHtml() + '<div class="bins-wrap">' + binTableHtml(true) + '</div>' + (S.binSummary || '');
    bindBins(box);
  }
  function bindBins(box) {
    box.querySelectorAll('tr.bin').forEach(function (tr) {
      tr.addEventListener('click', function (e) {
        if (e.target.closest('button')) return;
        var i = +tr.getAttribute('data-i'), at = S.binSel.indexOf(i);
        if (e.shiftKey && S.binSel.length) {
          var last = S.binSel[S.binSel.length - 1], lo = Math.min(last, i), hi = Math.max(last, i);
          for (var j = lo; j <= hi; j++) if (S.binSel.indexOf(j) < 0) S.binSel.push(j);
        } else if (e.ctrlKey || e.metaKey || e.target.classList.contains('bcb')) {
          if (at >= 0) S.binSel.splice(at, 1); else S.binSel.push(i);
        } else {
          S.binSel = at >= 0 && S.binSel.length === 1 ? [] : [i];
        }
        renderBins();
      });
    });
    box.querySelectorAll('button.ib').forEach(function (bt) {
      bt.onclick = function () { binAction(bt.getAttribute('data-act'), +bt.getAttribute('data-i')); };
    });
    var ms = box.querySelector('.missTo');
    if (ms) ms.onchange = function () {
      var v = ms.value;
      editDraft(function (d) { if (v === 'sep') { d.missing_separate = true; delete d.missing_branch; } else { d.missing_separate = false; d.missing_branch = +v; } });
    };
    var mg = box.querySelector('.bMerge'); if (mg) mg.onclick = mergeBins;
    var cl = box.querySelector('.bClear'); if (cl) cl.onclick = function () { S.binSel = []; renderBins(); };
    var un = box.querySelector('.bUndo'); if (un) un.onclick = function (e) { e.preventDefault(); undoBinEdit(); };
    var fu = box.querySelector('.bFull'); if (fu) fu.onclick = function () { $('#binModal').hidden = false; renderBinModal(); };
  }
  function editDraft(mutate) {
    var d = clone(S.draft);
    S.draftHist = S.draftHist || []; S.draftHist.push(clone(S.draft)); if (S.draftHist.length > 40) S.draftHist.shift();
    mutate(d);
    d.source = 'manual'; S.draft = d; S.binSel = [];
    $('#sbSource').textContent = 'manual override';
    fillDraftForm(); return previewDraft();
  }
  function undoBinEdit() {
    if (!S.draftHist || !S.draftHist.length) return;
    S.draft = S.draftHist.pop(); fillDraftForm(); previewDraft();
  }
  // index remapping for missing / other routing after groups are removed or inserted
  function remap(d, fn) {
    if (!d.missing_separate && d.missing_branch != null) d.missing_branch = fn(+d.missing_branch);
    if (d.kind === 'categorical' && !d.other_separate && d.other_branch != null) d.other_branch = fn(+d.other_branch);
  }
  function mergeBins() {
    var sel = S.binSel.slice().sort(function (a, b) { return a - b; }), k = S.bins.kind, rows = binRows();
    if (sel.length < 2) return;
    var lo = sel[0], hi = sel[sel.length - 1];
    if (k === 'numeric') {
      if (hi - lo + 1 !== sel.length) { toast('Numeric bins can only be merged with their neighbours: select adjacent groups.', true); return; }
      editDraft(function (d) {
        d.cuts = d.cuts.slice(0, lo).concat(d.cuts.slice(hi));
        remap(d, function (b) { return b <= lo ? b : b <= hi ? lo : b - (hi - lo); });
      });
    } else if (k === 'categorical') {
      editDraft(function (d) {
        var ng = d.groups.length, otherSel = d.other_separate && sel.indexOf(ng) >= 0;
        var real = sel.filter(function (i) { return i < ng; });
        if (!real.length) return;
        var tgt = real[0], merged = [];
        real.forEach(function (i) { merged = merged.concat(d.groups[i]); });
        var keep = [], newIdx = {};
        d.groups.forEach(function (g, i) {
          if (i === tgt) { newIdx[i] = keep.length; keep.push(merged); }
          else if (real.indexOf(i) >= 0) { newIdx[i] = null; }
          else { newIdx[i] = keep.length; keep.push(g); }
        });
        real.forEach(function (i) { newIdx[i] = newIdx[tgt]; });
        d.groups = keep;
        remap(d, function (b) { return b < ng ? newIdx[b] : b; });
        if (otherSel) { d.other_separate = false; d.other_branch = newIdx[tgt]; }
      });
    } else {
      var nr = rows.length - 1;
      if (sel.indexOf(nr) >= 0) { toast('"Otherwise" cannot be merged; merge rules with each other.', true); return; }
      editDraft(function (d) {
        var merged = sel.map(function (i) { return '(' + d.rules[i] + ')'; }).join(' or ');
        d.rules = d.rules.filter(function (r, i) { return sel.indexOf(i) < 0 || i === lo; });
        d.rules[lo] = merged;
      });
    }
  }
  function binAction(act, i) {
    var r = binRows()[i], d = S.draft, k = S.bins.kind;
    if (act === 'edge') {
      var lo = i > 0 ? d.cuts[i - 1] : r.min, hi = i + 2 <= d.cuts.length ? d.cuts[i + 1] : binRows()[i + 1].max;
      askValue('Edit boundary — Group ' + (i + 1) + ' → Group ' + (i + 2), 'New threshold value', d.cuts[i], lo, hi,
        'Current: <b>' + fv(d.cuts[i]) + '</b> | Valid range: ' + fv(lo) + ' – ' + fv(hi),
        function (v) { editDraft(function (x) { x.cuts[i] = v; }); });
    } else if (act === 'split' && k === 'numeric') {
      var a = r.lo != null ? r.lo : r.min, b = r.hi != null ? r.hi : r.max;
      if (a == null || b == null || !(b > a)) { toast('This bin has no spread of values to split.', true); return; }
      var dflt = r.median != null && r.median > a && r.median < b ? r.median : (a + b) / 2;
      askValue('Split bin — Group ' + (i + 1), 'Cut value (rows below go to the first part)', +dflt.toPrecision(6), a, b,
        'Bin: ' + esc(r.label) + ' | Valid range: ' + fv(a) + ' – ' + fv(b) + ' (exclusive) | median ' + fv(r.median),
        function (v) {
          editDraft(function (x) { x.cuts.splice(i, 0, v); remap(x, function (bb) { return bb > i ? bb + 1 : bb; }); });
        });
    } else if (act === 'members') {
      askText('Edit categories — Group ' + (i + 1), 'Categories (comma separated)', (d.groups[i] || []).join(', '), function (t) {
        var vals = t.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
        if (!vals.length) { toast('A group needs at least one category; merge it instead.', true); return; }
        editDraft(function (x) {
          x.groups = x.groups.map(function (g, j) { return j === i ? vals : g.filter(function (v) { return vals.indexOf(String(v)) < 0; }); });
          if (x.groups.some(function (g) { return !g.length; })) toast('A group became empty; merge or remove it.', true);
        });
      });
    } else if (act === 'split' && k === 'categorical') {
      var cats = (r.members || []).slice();
      (r.categories || []).forEach(function (c) { if (cats.indexOf(c.value) < 0) cats.push(c.value); });
      var counts = {}; (r.categories || []).forEach(function (c) { counts[c.value] = c.n; });
      if (cats.length < (r.members ? 2 : 1)) { toast('Not enough categories in this group to split.', true); return; }
      askPick('Move categories to a new group — ' + esc(r.label), cats, counts, function (picked) {
        if (!picked.length || (r.members && picked.length === cats.length)) { toast('Pick some, but not all, categories.', true); return; }
        editDraft(function (x) {
          var at = r.members ? i + 1 : x.groups.length;
          if (r.members) x.groups[i] = x.groups[i].filter(function (v) { return picked.indexOf(String(v)) < 0; });
          x.groups.splice(at, 0, picked);
          remap(x, function (bb) { return bb >= at ? bb + 1 : bb; });
        });
      });
    } else if (act === 'rule') {
      askText('Edit rule ' + (i + 1), 'Formula (first true rule wins)', d.rules[i], function (t) {
        if (!t.trim()) return; editDraft(function (x) { x.rules[i] = t.trim(); });
      });
    } else if (act === 'split' && k === 'rule') {
      askText('Add a rule after ' + (i < d.rules.length ? 'rule ' + (i + 1) : 'the last rule'), 'Formula', '', function (t) {
        if (!t.trim()) return; editDraft(function (x) { x.rules.splice(Math.min(i + 1, x.rules.length), 0, t.trim()); });
      });
    }
  }
  // small modal helpers
  function openEdit(title, html, onApply) {
    $('#editTitle').textContent = title; $('#editBody').innerHTML = html; $('#editModal').hidden = false;
    $('#btnEditApply').onclick = function () { if (onApply() !== false) $('#editModal').hidden = true; };
    var f = $('#editBody input, #editBody textarea'); if (f) { f.focus(); f.select && f.select(); }
  }
  function askValue(title, label, cur, lo, hi, info, cb) {
    openEdit(title, '<label>' + esc(label) + '<input id="edVal" type="number" step="any" value="' + (cur == null ? '' : +(+cur).toPrecision(8)) + '"></label><div class="hint">' + info + '</div>',
      function () {
        var v = parseFloat($('#edVal').value);
        if (isNaN(v)) { toast('Enter a number.', true); return false; }
        if ((lo != null && v <= lo) || (hi != null && v >= hi)) { toast('Value must be inside the valid range (' + fv(lo) + ' – ' + fv(hi) + ').', true); return false; }
        cb(v);
      });
  }
  function askText(title, label, cur, cb) {
    openEdit(title, '<label>' + esc(label) + '<textarea id="edTxt" rows="3">' + esc(cur || '') + '</textarea></label>', function () { cb($('#edTxt').value); });
  }
  function askPick(title, cats, counts, cb) {
    openEdit(title, '<div class="hint">Ticked categories move to a new group.</div><div class="pick">' + cats.map(function (c, j) {
      return '<label class="chk"><input type="checkbox" data-j="' + j + '"> ' + esc(c) + (counts[c] != null ? ' <span class="hint">(' + num(counts[c]) + ')</span>' : '') + '</label>';
    }).join('') + '</div>', function () {
      cb($$('#editBody .pick input:checked').map(function (x) { return cats[+x.getAttribute('data-j')]; }));
    });
  }
  function editExistingSplit() {
    var n = findNode(S.selected); if (!n || !n.split) return;
    S.relSel = null; drawRel(); S.draftHist = [];
    S.draft = clone(n.split); S.draft.source = n.split.source || 'manual';
    $('#splitBox').hidden = false; $('#sbTitle').textContent = 'Edit split' + (n.split.variable ? ' on ' + n.split.variable : '');
    $('#sbSource').textContent = S.draft.source; $('#btnFromCfg').hidden = true;
    fillDraftForm(); previewDraft();
  }
  function applyDraft() {
    var d = draftFromForm(), id = S.selected;
    var probe = d.kind === 'rule' ? (d.rules || []).length : d.kind === 'numeric' ? (d.cuts || []).length : (d.groups || []).length;
    if (!probe) { toast('Define at least one cut, group or rule.', true); return; }
    if (d.kind !== 'rule' && !d.variable) { toast('Choose the variable.', true); return; }
    var cur = findNode(id);
    var keepKids = cur && cur.split && (cur.children || []).length === nBranches(d);
    if (cur && cur.split && !keepKids && (cur.children || []).some(function (c) { return c.split; }) &&
        !window.confirm('The number of branches changes, so the sub-trees under this node will be replaced by leaves. Continue?')) return;
    api('/api/preview', { tree: S.tree, node_id: id, split: d }).then(function (r) {
      commit(function () {
        var n = findNode(id), tmpl = n.leaf || firstLeafOf(n), base = nextId(), old = n.split;
        var auto = function (c) {   // child names generated from the previous split are refreshed
          return !old || (old.kind === 'rule' ? (old.rules || []).concat(['Otherwise']).indexOf(c.name) >= 0
                                              : String(c.name || '').indexOf(old.variable + ' ') === 0);
        };
        n.split = d; n.leaf = null;
        if (keepKids) {
          n.children.forEach(function (c, i) { if (auto(c)) c.name = d.kind === 'rule' ? r.labels[i] : d.variable + ' ' + r.labels[i]; });
        } else {
          n.children = r.labels.map(function (lab, i) {
            return newLeaf('n' + (base + i + 1), d.kind === 'rule' ? lab : d.variable + ' ' + lab, tmpl);
          });
        }
      }, true);
      S.draft = null; S.draftHist = []; $('#splitBox').hidden = true;
      toast((keepKids ? 'Split updated (sub-trees kept): ' : 'Split applied: ') + r.labels.length + ' branches.');
    }).catch(function (e) { toast(e.message, true); });
  }
  function manualSplit() {
    S.relSel = null; drawRel();
    S.draft = { kind: 'rule', rules: [], source: 'manual' }; S.draftHist = [];
    $('#splitBox').hidden = false; $('#sbTitle').textContent = 'Manual split'; $('#sbSource').textContent = 'manual';
    $('#btnFromCfg').hidden = true; $('#sbPreview').innerHTML = '';
    fillDraftForm();
  }
  function prune() {
    var id = S.selected;
    commit(function () {
      var n = findNode(id), lf = firstLeafOf(n);
      n.split = null; n.children = []; n.leaf = clone(lf || newLeaf('x').leaf);
    }, true);
  }


  // ── leaf event-rate prediction ─────────────────────────────────────
  function pdInputs(st) {
    st = st || {};
    $('#pdCred').value = st.credibility == null ? 30 : st.credibility;
    $('#pdThr').value = st.threshold == null ? '' : st.threshold;
    $('#pdHold').value = st.holdout_pct == null ? 0 : st.holdout_pct;
  }
  function pdFromInputs() {
    var t = $('#pdThr').value;
    return { credibility: Math.max(0, +$('#pdCred').value || 0), threshold: t === '' ? null : +t,
             holdout_pct: Math.min(50, Math.max(0, +$('#pdHold').value || 0)), seed: 42 };
  }
  function onPdSettings() {
    var v = pdFromInputs();
    commit(function (t) { t.pd_settings = v; }, true);
  }
  function renderPdStatus() {
    var p = S.pd, box = $('#pdStatus');
    $('#btnUnfreeze').hidden = !(S.tree && S.tree.pd_model);
    if (!p || !p.state) { box.className = 'pd-status'; box.innerHTML = S.cfg && S.cfg.target ? '' : 'Choose a target to predict event rates.'; return; }
    var f = p.frozen || {}, thr = p.threshold == null ? '–' : pct(p.threshold, 2);
    if (p.state === 'frozen') {
      box.className = 'pd-status frozen';
      box.innerHTML = '<b>Frozen</b> on ' + esc(f.dataset) + ' · ' + num(f.n_known) + ' outcomes · ' + esc((f.frozen_at || '').slice(0, 10)) +
        '<br>credibility ' + esc(f.credibility) + (f.holdout_pct ? ' · holdout ' + esc(f.holdout_pct) + '%' : '') + ' · predict 1 if rate ≥ ' + thr;
    } else if (p.state === 'stale') {
      box.className = 'pd-status stale';
      box.innerHTML = '<b>Tree changed since freezing</b> – showing live in-sample rates. Freeze again to update the stored rates.';
    } else if (!p.source) {
      box.className = 'pd-status'; box.innerHTML = 'Choose a target to predict event rates.';
    } else {
      box.className = 'pd-status';
      box.innerHTML = '<b>Live</b> (in-sample, recomputed on every edit) · predict 1 if rate ≥ ' + thr + '<br>Freeze to store the rates with the tree for scoring and backtests.';
    }
  }
  function renderLeafPd(n) {
    var box = $('#lfPd'), L = S.pd && S.pd.leaves ? S.pd.leaves[n.id] : null;
    if (!L || L.pd == null) { box.innerHTML = '<span class="hint">The predicted event rate appears here once a target is loaded.</span>'; return; }
    var thr = S.pd.threshold, cls = thr != null && L.pd >= thr ? 1 : 0;
    box.innerHTML = '<div><span class="big">' + pct(L.pd, 2) + '</span>predicted event rate (<code>tree_event_rate</code> = <code>tree_proba_1</code>) → <code>tree_prediction</code> = <b>' + cls + '</b></div>' +
      '<div class="hint">Observed event rate ' + pct(L.raw, 2) + ' on ' + num(L.n_known) + ' outcomes (' + num(L.bads) + ' events) · 95% interval ' +
      pct(L.lower, 1) + ' – ' + pct(L.upper, 1) + ' · ' + (S.pd.state === 'frozen' ? 'frozen' : 'live, in-sample') + '</div>';
  }
  async function freezePd() {
    if (!S.cfg || !S.cfg.target) { toast('Load the population with a target (1 = event) first.', true); return; }
    try {
      var settings = pdFromInputs();
      var r = await api('/api/freeze_pd', { tree: S.tree, settings: settings });
      commit(function (t) { t.pd_settings = settings; t.pd_model = r.pd_model; }, true);
      toast('Leaf event rates frozen on ' + r.pd_model.dataset + ' (' + num(r.pd_model.n_known) + ' outcomes). Save the tree to keep them.');
    } catch (e) { toast(e.message, true); }
  }
  function unfreezePd() { commit(function (t) { delete t.pd_model; }, true); }
  function openCalibration() {
    var p = S.pd, c = p && p.calibration;
    if (!c) { toast('Load a population with a target to see the calibration.', true); return; }
    var k = [['Outcomes', num(c.n_known)], ['Observed event rate', pct(c.observed, 2)], ['Avg predicted event rate', pct(c.predicted, 2)],
      ['AUC', c.auc == null ? '–' : c.auc.toFixed(3)], ['Gini', c.gini == null ? '–' : c.gini.toFixed(3)], ['KS', c.ks == null ? '–' : c.ks.toFixed(3)],
      ['Brier', c.brier == null ? '–' : c.brier.toFixed(4)]];
    $('#calBody').innerHTML = '<div class="hint">Rows: <b>' + esc(p.calibration_scope) + '</b> · rates: ' + esc(p.state === 'frozen' ? 'frozen' : 'live') + ' · ' + esc(p.source || '') + '</div>' +
      '<div class="cal-kpis">' + k.map(function (x) { return '<div>' + x[0] + '<b>' + x[1] + '</b></div>'; }).join('') + '</div>' +
      '<table class="tbl"><tr><th>Leaf</th><th class="n">Outcomes</th><th class="n">Predicted event rate</th><th class="n">Observed event rate</th><th class="n">Observed − predicted</th><th class="n">Observed 95% interval</th><th>Predicted inside?</th></tr>' +
      c.leaves.map(function (l) {
        return '<tr><td>' + esc(l.leaf) + ' · ' + esc(l.name) + '</td><td class="n">' + num(l.n_known) + '</td><td class="n">' + pct(l.predicted, 2) + '</td><td class="n">' + pct(l.observed, 2) +
          '</td><td class="n">' + (l.diff == null ? '–' : (l.diff > 0 ? '+' : '') + (100 * l.diff).toFixed(2) + ' pts') + '</td><td class="n">' + pct(l.obs_lower, 1) + ' – ' + pct(l.obs_upper, 1) +
          '</td><td class="' + (l.inside == null ? '' : l.inside ? 'good' : 'bad') + '">' + (l.inside == null ? '–' : l.inside ? 'yes' : 'no') + '</td></tr>';
      }).join('') + '</table>' +
      '<p class="hint">Backtest: freeze the leaf rates (optionally with a holdout %), save the tree, then load another population – e.g. a later period – and open the saved tree: ' +
      'this table then compares the frozen rates with what actually happened. In credit data, outcomes usually exist for booked loans only.</p>';
    $('#calModal').hidden = false;
  }


  // ── backtest in database (full table, SQL export run by the warehouse) ──
  async function backtestDb() {
    if (!S.loaded) { toast('Load a population first.', true); return; }
    var btn = $('#btnDbBt'); busy(btn, true); btn.textContent = 'Running in database…';
    try {
      var r = await api('/api/backtest_db', { tree: S.tree, meta: meta() });
      var m = r.mem, d = r.db;
      var line = function (label, a, b, fmt, diff) {
        var dv = a == null || b == null ? '–' : diff(b - a);
        return '<tr><td>' + label + '</td><td class="n">' + fmt(a) + '</td><td class="n">' + fmt(b) + '</td><td class="n">' + dv + '</td></tr>';
      };
      var pts = function (x) { return (x > 0 ? '+' : '') + (100 * x).toFixed(2) + ' pts'; };
      var p2 = function (x) { return pct(x, 2); };
      var memLabel = r.sample_rows ? 'In memory (random sample, ' + num(r.rows_in_memory) + ' rows)' : 'In memory (all ' + num(r.rows_in_memory) + ' rows)';
      $('#dbBody').innerHTML = '<div class="hint">Table <code>' + esc(r.table) + '</code> · query ' + r.seconds.toFixed(1) + ' s' +
        (r.rates_note ? ' · leaf event rates in formulas: ' + esc(r.rates_note) : '') + '</div>' +
        '<table class="tbl"><tr><th>KPI</th><th class="n">' + memLabel + '</th><th class="n">In database (full table)</th><th class="n">Difference</th></tr>' +
        line('Rows', m.n, d.n, num, function (x) { return (x > 0 ? '+' : '') + num(x); }) +
        line('Approval', m.approval_rate, d.approval_rate, pct, pts) +
        line('Refer', m.refer_rate, d.refer_rate, pct, pts) +
        line('Decline', m.decline_rate, d.decline_rate, pct, pts) +
        (m.no_decision_rate || d.no_decision_rate ? line('No decision (segment)', m.no_decision_rate, d.no_decision_rate, pct, pts) : '') +
        line('Event rate (rows with outcome)', m.event_rate, d.event_rate, p2, pts) +
        line('Event rate approved', m.event_rate_approved, d.event_rate_approved, p2, pts) +
        (d.avg_predicted != null ? line('Avg predicted event rate (frozen)', m.avg_predicted, d.avg_predicted, p2, pts) : '') +
        (d.profit != null ? line('Profit', m.profit, d.profit, money, function (x) { return (x > 0 ? '+' : '−') + money(Math.abs(x)).replace('−', ''); }) +
          line('Profit per 1,000 rows', m.profit_per_row, d.profit_per_row, function (x) { return x == null ? '–' : money(x * 1000); }, function (x) { return (x > 0 ? '+' : '−') + money(Math.abs(x * 1000)).replace('−', ''); }) : '') +
        '</table>' +
        '<h3 style="margin-top:12px">By leaf</h3><table class="tbl"><tr><th>Leaf</th><th class="n">Share in memory</th><th class="n">Share in database</th>' +
        '<th class="n">Event rate in memory</th><th class="n">Event rate in database</th><th class="n">Difference</th></tr>' +
        r.leaves.map(function (l) {
          var dd = l.event_rate_mem == null || l.event_rate_db == null ? '–' : pts(l.event_rate_db - l.event_rate_mem);
          return '<tr><td>' + esc(l.leaf) + '</td><td class="n">' + pct(l.share_mem) + '</td><td class="n">' + pct(l.share_db) + '</td><td class="n">' +
            pct(l.event_rate_mem, 2) + '</td><td class="n">' + pct(l.event_rate_db, 2) + '</td><td class="n">' + dd + '</td></tr>';
        }).join('') + '</table>' +
        '<details style="margin-top:10px"><summary class="hint">SQL run in the database</summary><pre class="sqlbox">' + esc(r.sql) + '</pre></details>';
      $('#dbModal').hidden = false;
    } catch (e) { toast(e.message, true); }
    finally { busy(btn, false); btn.textContent = 'Backtest in database'; }
  }

  // ── logic tab: calculations + leaf decision ────────────────────────
  function calcRow(c, cls) {
    return '<div class="calc-row ' + cls + '"><input class="cn" type="text" placeholder="name" value="' + esc(c.name || '') + '">' +
      '<input class="ce" type="text" placeholder="formula" value="' + esc(c.expr || '') + '"><button class="danger rm">✕</button></div>';
  }
  function renderLogic(n) {
    $('#calcList').innerHTML = (n.calcs || []).map(function (c) { return calcRow(c, 'calc'); }).join('') ||
      '<div class="hint">No calculations. Example: <code>adj_score</code> = <code>policy_score_existing - 15 * inquiries_6m_count</code></div>';
    var leaf = !n.split, isRoot = S.tree && n.id === S.tree.id;
    $('#leafBox').hidden = !leaf;
    $('#defOutBox').hidden = !isRoot;
    if (isRoot) $('#defOutList').innerHTML = (S.tree.default_outputs || []).map(function (o) { return calcRow(o, 'dout'); }).join('');
    if (leaf) {
      var lf = n.leaf || {};
      $('#lfMode').value = lf.mode || 'cutoff'; $('#lfTier').value = lf.pricing_tier || '';
      $('#lfScore').value = lf.score_expr || ''; $('#lfScore').placeholder = (S.cfg && S.cfg.score_col) || 'score formula';
      $('#lfApprove').value = lf.approve_at == null ? '' : lf.approve_at; $('#lfRefer').value = lf.refer_at == null ? '' : lf.refer_at;
      $('#lfReasons').value = (lf.reason_codes || []).join(', '); $('#lfLimit').value = lf.limit_expr || '';
      $('#outList').innerHTML = (lf.outputs || []).map(function (o) { return calcRow(o, 'out'); }).join('');
      var own = (lf.outputs || []).map(function (o) { return o.name; });
      var inh = (S.tree.default_outputs || []).map(function (o) { return o.name; }).filter(function (x) { return own.indexOf(x) < 0; });
      $('#inheritedOuts').innerHTML = inh.length ? 'Also from the root (default outputs): <code>' + inh.map(esc).join('</code>, <code>') + '</code>' : '';
      S.lfDecOut = lf.decision_output || '';
      $('#lfDecOut').innerHTML = '';
      renderLeafPd(n);
      leafVisibility();
    }
    bindRemovers();
  }
  function bindRemovers() { $$('.calc-row .rm').forEach(function (b) { b.onclick = function () { b.parentNode.remove(); }; }); }
  function leafVisibility() {
    var m = $('#lfMode').value, c = m === 'cutoff';
    $('#lfCutWrap').hidden = !c; $('#lfDecOutWrap').hidden = m !== 'output';
    $('#lfScoreWrap').hidden = m === 'approve' || m === 'refer' || m === 'decline';
    if (m === 'output') {
      var names = readRows('out').map(function (o) { return o.name; });
      (S.tree.default_outputs || []).forEach(function (o) { if (names.indexOf(o.name) < 0) names.push(o.name); });
      var cur = $('#lfDecOut').value || S.lfDecOut || '';
      $('#lfDecOut').innerHTML = names.length ? names.map(function (x) { return '<option' + (x === cur ? ' selected' : '') + '>' + esc(x) + '</option>'; }).join('')
        : '<option value="">add an output first</option>';
    }
  }
  function readRows(cls) {
    return $$('#tab-logic .calc-row.' + cls).map(function (r) {
      return { name: r.querySelector('.cn').value.trim(), expr: r.querySelector('.ce').value.trim() };
    }).filter(function (c) { return c.name && c.expr; });
  }
  function applyLogic() {
    var id = S.selected, calcs = readRows('calc'), outs = readRows('out'), douts = readRows('dout');
    commit(function () {
      var n = findNode(id);
      n.calcs = calcs;
      if (n.id === S.tree.id) { if (douts.length) S.tree.default_outputs = douts; else delete S.tree.default_outputs; }
      if (!n.split) {
        var num_ = function (v) { return v === '' || v == null ? null : +v; };
        n.leaf = { mode: $('#lfMode').value, score_expr: $('#lfScore').value.trim(), approve_at: num_($('#lfApprove').value),
          refer_at: num_($('#lfRefer').value), reason_codes: $('#lfReasons').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
          pricing_tier: $('#lfTier').value.trim(), limit_expr: $('#lfLimit').value.trim(), outputs: outs };
        if (n.leaf.mode === 'output') n.leaf.decision_output = $('#lfDecOut').value;
      }
    }, true);
    toast('Logic applied to ' + id + '.');
  }

  // ── metrics configuration ──────────────────────────────────────────
  function openMetrics() {
    var labels = S.metricLabels || {};
    $('#metricChecks').innerHTML = Object.keys(labels).map(function (k) {
      return '<label class="inline"><input type="checkbox" value="' + k + '"' + (S.display.metrics.indexOf(k) >= 0 ? ' checked' : '') + '> ' + esc(labels[k]) + '</label>';
    }).join('');
    $('#customList').innerHTML = (S.display.custom_metrics || []).map(cmRow).join('');
    bindCm(); $('#metricsModal').hidden = false;
  }
  function cmRow(c) {
    var aggs = ['mean', 'sum', 'min', 'max', 'median', 'std', 'rate', 'count'];
    return '<div class="cm-row"><input class="cn" placeholder="name" value="' + esc(c.name || '') + '"><select class="ca">' +
      aggs.map(function (a) { return '<option' + (a === (c.agg || 'mean') ? ' selected' : '') + '>' + a + '</option>'; }).join('') +
      '</select><input class="ce" placeholder="formula" value="' + esc(c.expr || '') + '"><button class="danger rm">✕</button></div>';
  }
  function bindCm() { $$('.cm-row .rm').forEach(function (b) { b.onclick = function () { b.parentNode.remove(); }; }); }
  function saveMetrics() {
    S.display = {
      metrics: $$('#metricChecks input:checked').map(function (i) { return i.value; }),
      custom_metrics: $$('#customList .cm-row').map(function (r) {
        return { name: r.querySelector('.cn').value.trim().replace(/\W+/g, '_'), agg: r.querySelector('.ca').value, expr: r.querySelector('.ce').value.trim() };
      }).filter(function (c) { return c.name && c.expr; })
    };
    lsSet(S.display); $('#metricsModal').hidden = true;
    if (gNodes) gNodes.selectAll('g.node').attr('data-k', null);
    evaluate();
  }

  // ── save / open / import / export ──────────────────────────────────
  function meta() { return { name: $('#stName').value, status: $('#stStatus').value }; }
  async function save() {
    try {
      var r = await api('/api/save', { meta: meta(), tree: S.tree, display: S.display });
      $('#stName').value = r.name; toast('Saved ' + r.name + ' v' + r.version + ' to decision_strategies' + r.path); refreshLists();
    } catch (e) { toast(e.message, true); }
  }
  async function openSaved() {
    var p = $('#savedSel').value; if (!p) return;
    try {
      var d = await api('/api/tree?path=' + encodeURIComponent(p));
      S.undo.push(JSON.stringify(S.tree));
      S.tree = d.tree; if (d.display && d.display.metrics) S.display = d.display;
      $('#stName').value = (d.meta || {}).name || ''; $('#stStatus').value = (d.meta || {}).status || 'DRAFT';
      S.selected = S.tree.id; S.collapsed = {}; S.rel = null; pdInputs(S.tree.pd_settings);
      if (gNodes) gNodes.selectAll('g.node').attr('data-k', null);
      await evaluate(true);
      if (d.meta && d.meta.dataset && d.meta.dataset !== S.cfg.dataset) toast('Saved on ' + d.meta.dataset + ', evaluated on ' + S.cfg.dataset + '.');
    } catch (e) { toast(e.message, true); }
  }
  async function importVml() {
    var m = S.models[+$('#vmlSel').value]; if (!m) { toast('Choose a decision-tree model.', true); return; }
    var btn = $('#btnVml'); busy(btn, true); btn.textContent = 'Importing…';
    try {
      var r = await api('/api/vml_import', Object.assign({}, m, { approve_class: $('#vmlClass').value || null, threshold: +$('#vmlThr').value || 0.5 }));
      S.undo.push(JSON.stringify(S.tree));
      S.tree = r.tree; S.selected = S.tree.id; S.collapsed = {}; S.rel = null;
      var depth = 0; walk(S.tree, function (n, d) { depth = Math.max(depth, d); });
      if (depth > 4) walk(S.tree, function (n, d) { if (d === 4 && n.children && n.children.length) S.collapsed[n.id] = true; });
      if (gNodes) gNodes.selectAll('g.node').attr('data-k', null);
      await evaluate(true);
      toast('Imported ' + r.report.leaves + ' leaves (approve class "' + r.report.approve_class + '")' +
        (r.report.max_share_gap != null ? ' · max leaf-share gap vs model ' + pct(r.report.max_share_gap) : '') + '.');
    } catch (e) { toast(e.message, true); }
    finally { busy(btn, false); btn.textContent = 'Import tree'; }
  }
  async function doExport() {
    try {
      var r = await api('/api/export', { format: $('#expFmt').value, meta: meta(), tree: S.tree });
      var blob = new Blob([r.body], { type: 'text/plain' }), a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = ($('#stName').value || 'strategy_tree') + '.' + r.ext; a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
    } catch (e) { toast(e.message, true); }
  }
  async function sendStudio(force) {
    try {
      var r = await api('/api/send_to_studio', { meta: meta(), tree: S.tree, force: !!force });
      if (r.status === 'needs_confirmation') {
        if (window.confirm('Strategy Studio cannot represent everything in this tree:\n\n• ' + r.issues.join('\n• ') +
          '\n\nSend a simplified version anyway?')) return sendStudio(true);
        return;
      }
      toast('Sent to Strategy Studio: decision_strategies' + r.path);
    } catch (e) { toast(e.message, true); }
  }
  async function writeDataset() {
    var btn = $('#btnWrite'); busy(btn, true);
    try {
      var r = await api('/api/write_dataset', { tree: S.tree, meta: meta(), output: $('#outName').value.trim() || null });
      toast('Wrote ' + num(r.rows) + ' rows to ' + r.dataset + '.');
    } catch (e) { toast(e.message, true); }
    finally { busy(btn, false); }
  }

  // ── events ─────────────────────────────────────────────────────────
  $('#ds').onchange = function () { onDataset().catch(function (e) { toast(e.message, true); }); };
  $('#btnLoad').onclick = loadPopulation;
  $$('.tabs button').forEach(function (b) { b.onclick = function () { setTab(b.getAttribute('data-tab')); }; });
  $('#relSearch').addEventListener('input', debounce(filterRel, 200));
  $('#relHide').onchange = filterRel;
  $('#relList').addEventListener('scroll', function () { if (!drawRel.raf) drawRel.raf = requestAnimationFrame(function () { drawRel.raf = null; drawRel(); }); });
  $('#relRows').addEventListener('click', function (e) {
    var row = e.target.closest('.rrow'); if (row) pickVariable(row.getAttribute('data-v'), true);
  });
  $('#btnSuggest').onclick = suggestSplit;
  $('#btnFromCfg').onclick = splitFromConfig;
  $('#cfgSel').onchange = function () { if (S.draft && S.draft.variable) pickVariable(S.draft.variable, false); };
  $('#sbKind').onchange = function () { S.draft = draftFromForm(); kindVisibility(); branchSelects(); };
  $('#sbCuts').addEventListener('input', debounce(branchSelects, 250));
  $('#sbGroups').addEventListener('input', debounce(branchSelects, 250));
  $('#btnPreview').onclick = previewDraft;
  $('#btnApply').onclick = applyDraft;
  $('#btnManual').onclick = manualSplit;
  $('#btnPrune').onclick = prune;
  $('#btnEditSplit').onclick = editExistingSplit;
  $('#btnEditCancel').onclick = function () { $('#editModal').hidden = true; };
  $('#btnBinClose').onclick = function () { $('#binModal').hidden = true; };
  $('#btnAddCalc').onclick = function () {
    if ($('#calcList .hint')) $('#calcList').innerHTML = '';
    $('#calcList').insertAdjacentHTML('beforeend', calcRow({}, 'calc')); bindRemovers();
  };
  $('#btnAddOut').onclick = function () { $('#outList').insertAdjacentHTML('beforeend', calcRow({}, 'out')); bindRemovers(); };
  $('#btnAddDefOut').onclick = function () { $('#defOutList').insertAdjacentHTML('beforeend', calcRow({}, 'dout')); bindRemovers(); };
  $('#outList').addEventListener('change', function () { if ($('#lfMode').value === 'output') leafVisibility(); });
  $('#lfMode').onchange = leafVisibility;
  $('#btnApplyLogic').onclick = applyLogic;
  $('#nodeName').addEventListener('change', function () {
    var id = S.selected, v = $('#nodeName').value;
    commit(function () { findNode(id).name = v; }, true);
  });
  $('#btnMetrics').onclick = openMetrics;
  $('#btnMetricsClose').onclick = function () { $('#metricsModal').hidden = true; };
  $('#btnAddCustom').onclick = function () { $('#customList').insertAdjacentHTML('beforeend', cmRow({})); bindCm(); };
  $('#btnMetricsSave').onclick = saveMetrics;
  $('#btnUndo').onclick = function () {
    if (!S.undo.length) return;
    S.tree = JSON.parse(S.undo.pop()); if (!findNode(S.selected)) S.selected = S.tree.id; S.rel = null;
    if (gNodes) gNodes.selectAll('g.node').attr('data-k', null);
    evaluate();
  };
  $('#btnFit').onclick = fitView;
  $('#btnExpand').onclick = function () { S.collapsed = {}; render(true); };
  $('#btnSave').onclick = save;
  $('#btnOpen').onclick = openSaved;
  $('#btnVml').onclick = importVml;
  $('#btnExport').onclick = doExport;
  $('#btnStudio').onclick = function () { sendStudio(false); };
  $('#btnWrite').onclick = writeDataset;
  ['#pdCred', '#pdThr', '#pdHold'].forEach(function (k) { $(k).addEventListener('change', onPdSettings); });
  $('#btnFreeze').onclick = freezePd;
  $('#btnUnfreeze').onclick = unfreezePd;
  $('#btnCalib').onclick = openCalibration;
  $('#btnCalClose').onclick = function () { $('#calModal').hidden = true; };
  $('#btnDbBt').onclick = backtestDb;
  $('#btnDbClose').onclick = function () { $('#dbModal').hidden = true; };

  init();
})();
