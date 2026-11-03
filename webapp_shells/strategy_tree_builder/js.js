fetch(dataiku.getWebAppBackendUrl('/ui')).then(function (r) { return r.json(); }).then(function (u) {
  var s = document.createElement('style'); s.textContent = u.css; document.head.appendChild(s);
  document.getElementById('root').innerHTML = u.html;
  new Function(u.js)();
});
