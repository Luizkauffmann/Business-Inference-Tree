/* Business Inference Tree – browser demo runtime.
   Runs the unchanged Python webapp backends in Pyodide (Python compiled to WebAssembly) and routes
   the UI's backend calls to them, so the whole app works offline in the page: no server, and the
   data never leaves the browser. Copyright 2026 Luiz Kauffmann – Apache License 2.0. */
(function () {
  'use strict';
  var PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
  var py = null, appName = null, backend = null;

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function status(text, err) {
    var s = document.getElementById('bitStatus');
    if (s) { s.textContent = text; s.className = 'bit-status' + (err ? ' err' : ''); }
    var m = document.querySelector('#bitLoading .msg');
    if (m) m.textContent = text;
  }

  window.bitDownload = function (name, text) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
  };

  // The UIs call dataiku.getWebAppBackendUrl(path) then fetch(): route those calls to Python.
  window.dataiku = { getWebAppBackendUrl: function (p) { return 'py:' + p; } };
  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (url, opt) {
    if (typeof url !== 'string' || url.indexOf('py:') !== 0) return nativeFetch(url, opt);
    return new Promise(function (resolve) {
      setTimeout(function () {          // let the page repaint (spinners, toasts) before Python runs
        var u = url.slice(3), q = u.indexOf('?');
        var out = backend.dispatch(appName, q < 0 ? u : u.slice(0, q), q < 0 ? '' : u.slice(q + 1), (opt && opt.body) || null);
        var r = out.toJs(); out.destroy();
        resolve(new Response(r[0], { status: r[1], headers: { 'Content-Type': r[2] } }));
      }, 0);
    });
  };

  async function text(path) {
    var r = await nativeFetch(path);
    if (!r.ok) throw new Error('Could not load ' + path + ' (' + r.status + ')');
    return r.text();
  }

  function refreshDatasets(select) {
    var names = JSON.parse(backend.datasets());
    var s = document.querySelector('#ds');
    if (!s) return;
    s.innerHTML = names.map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }).join('');
    s.value = select; s.dispatchEvent(new Event('change'));
  }

  async function upload(file) {
    if (!file) return;
    status('Reading ' + file.name + '…');
    try {
      var name = backend.load_csv(file.name.replace(/\.csv$/i, ''), await file.text());
      refreshDatasets(name);
      status('Loaded ' + name + ' – pick its target (1 = event) and score, then Load population.');
    } catch (e) { status('Could not read ' + file.name + ': ' + e.message, true); }
  }

  window.BIT = {
    start: async function (cfg) {
      appName = cfg.app;
      try {
        status('Starting Python in your browser (first visit downloads ~15 MB, then it is cached)…');
        py = await loadPyodide({ indexURL: PYODIDE });
        status('Loading numpy and pandas…');
        await py.loadPackage(['numpy', 'pandas']);
        status('Loading the app…');
        py.FS.mkdirTree('/app');
        for (var i = 0; i < cfg.files.length; i++) {
          var f = cfg.files[i], dest = '/app/' + f.replace(/^(py|app)\//, '');
          py.FS.mkdirTree(dest.slice(0, dest.lastIndexOf('/')));
          py.FS.writeFile(dest, await text(f));
        }
        py.runPython("import sys\nsys.path.insert(0, '/app')\nimport demo_backend");
        backend = py.globals.get('demo_backend');
        backend.register(cfg.app, cfg.module);
        status('Loading the sample credit dataset (30,000 card customers)…');
        backend.load_csv('credit_card_default', await text('data/credit_card_default.csv'));
        var u = await (await window.fetch('py:/ui')).json();
        var st = document.createElement('style'); st.textContent = u.css; document.head.appendChild(st);
        document.getElementById('root').innerHTML = u.html;
        new Function(u.js)();
        Object.keys(cfg.defaults || {}).forEach(function (sel) {   // illustrative economics for the sample
          var el = document.querySelector('#root ' + sel); if (el) el.value = cfg.defaults[sel];
        });
        status('Ready · everything runs in your browser – no data leaves this page');
        var up = document.getElementById('bitUpload');
        if (up) up.addEventListener('change', function () { upload(up.files[0]); up.value = ''; });
        var sm = document.getElementById('bitSample');
        if (sm) sm.addEventListener('click', function () { refreshDatasets('credit_card_default'); });
      } catch (e) {
        console.error(e);
        status('Could not start the demo: ' + e.message, true);
      }
    }
  };
})();
