(function() {
  "use strict";
  if (window.__VP_USER_HTMX_BOOTSTRAPPED__) {
    return;
  }
  window.__VP_USER_HTMX_BOOTSTRAPPED__ = true;
  var assetV = window.__ASSET_VERSION__ || window.__assetVersion || "0";
  function loadScript(src) {
    return new Promise(function(resolve, reject) {
      var existed = document.querySelector('script[data-vp-user-src="' + src + '"]');
      if (existed) {
        if (existed.getAttribute("data-loaded") === "1") {
          resolve();
          return;
        }
        existed.addEventListener("load", function onLoad() {
          existed.removeEventListener("load", onLoad);
          resolve();
        });
        existed.addEventListener("error", function onErr() {
          existed.removeEventListener("error", onErr);
          reject(new Error("failed to load: " + src));
        });
        return;
      }
      var s = document.createElement("script");
      s.src = src;
      s.defer = true;
      s.async = false;
      s.setAttribute("data-vp-user-src", src);
      s.onload = function() {
        s.setAttribute("data-loaded", "1");
        resolve();
      };
      s.onerror = function() {
        reject(new Error("failed to load: " + src));
      };
      document.head.appendChild(s);
    });
  }
  async function boot() {
    if (typeof window.initUserProfileApp !== "function") {
      await loadScript("/v2/static/js/runtime/pages/user_profile_app.js?v=" + assetV);
    }
    if (typeof window.initUserProfileApp === "function" && !window.__VP_USER_APP_BOOTED__) {
      window.initUserProfileApp();
    }
  }
  boot().catch(function(err) {
    try {
      console.error("[user-htmx] bootstrap failed:", err);
    } catch (_) {}
  });
})();