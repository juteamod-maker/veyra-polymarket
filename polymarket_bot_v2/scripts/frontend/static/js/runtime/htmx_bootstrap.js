(function () {
  function loadScript(src) {
    var s = document.createElement("script");
    s.src = src;
    s.defer = true;
    document.head.appendChild(s);
  }
  var v = encodeURIComponent(document.documentElement.getAttribute("data-asset-version") || "0");
  loadScript("/v2/static/js/runtime/shared/header_store.js?v=" + v);
  function initByPath() {
    var path = window.location.pathname || "";
    var routeModules = {
      "/go": "/v2/static/js/runtime/htmx_pages/go.js?v=" + v,
      "/v2/go": "/v2/static/js/runtime/htmx_pages/go.js?v=" + v,
      "/v2/refer": "/v2/static/js/runtime/htmx_pages/refer.js?v=" + v,
      "/v2/config": "/v2/static/js/runtime/htmx_pages/config.js?v=" + v,
      "/v2/user": "/v2/static/js/runtime/htmx_pages/user.js?v=" + v,
      "/discover": "/v2/static/js/runtime/htmx_pages/discover.js?v=" + v,
      "/v2/discover": "/v2/static/js/runtime/htmx_pages/discover.js?v=" + v,
      "/v2/deposit": "/v2/static/js/runtime/htmx_pages/deposit.js?v=" + v,
      "/deposit": "/v2/static/js/runtime/htmx_pages/deposit.js?v=" + v,
      "/valuelab": "/v2/static/js/runtime/htmx_pages/valuelab.js?v=" + v,
      "/v2/valuelab": "/v2/static/js/runtime/htmx_pages/valuelab.js?v=" + v
    };
    var moduleSrc = routeModules[path];
    if (!moduleSrc) {
      if (path.indexOf("/event/") === 0 || path.indexOf("/v2/event/") === 0) {
        moduleSrc = "/v2/static/js/runtime/htmx_pages/event_detail.js?v=" + v;
      } else if (path.indexOf("/valuelab/") === 0 || path.indexOf("/v2/valuelab/") === 0) {
        moduleSrc = "/v2/static/js/runtime/htmx_pages/valuelab_detail.js?v=" + v;
      }
    }
    if (!moduleSrc) return;
    loadScript(moduleSrc);
  }
  initByPath();
  window.__VP_HTMX_BOOTSTRAP_READY__ = true;
})();