(function () {
  var root = document.getElementById('ss-root');
  fetch(dataiku.getWebAppBackendUrl('/ui')).then(function (r) { return r.json(); }).then(function (u) {
    var s = document.createElement('style'); s.textContent = u.css; document.head.appendChild(s);
    root.innerHTML = u.html;
    new Function(u.js)();
  }).catch(function (e) {
    root.textContent = 'Could not load the app: ' + e.message + '. Is the backend running?';
  });
})();
