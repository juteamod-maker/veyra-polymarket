(function () {
  var root = document.documentElement;
  var v = encodeURIComponent(root.getAttribute('data-asset-version') || '0');

  function appendScript(src, onload, onerror) {
    var s = document.createElement('script');
    s.src = src;
    s.defer = true;
    if (onload) s.onload = onload;
    if (onerror) s.onerror = onerror;
    document.head.appendChild(s);
    return s;
  }

  appendScript('/v2/static/js/runtime/htmx_bootstrap.js?v=' + v);
})();
