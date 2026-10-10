(function() {
  "use strict";
  if (window.vp && window.vp.__loaded) return;
  function _h(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html != null) el.innerHTML = html;
    return el;
  }
  function _esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function _icon(type) {
    var map = {
      info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
      success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
      warning: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
      danger: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>'
    };
    return map[type] || map.info;
  }
  function _T(key, fb) {
    try {
      if (window.veyraI18n && typeof window.veyraI18n.t === "function") return window.veyraI18n.t(key, fb);
    } catch (e) {}
    return fb;
  }
  var HTTP_KEYS = {
    400: [ "vpUi.http.400", "Invalid request parameters. Please check your input." ],
    401: [ "vpUi.http.401", "Session expired. Please sign in again." ],
    403: [ "vpUi.http.403", "You do not have permission to perform this action." ],
    404: [ "vpUi.http.404", "Resource not found or has been removed." ],
    409: [ "vpUi.http.409", "Operation conflict. Please refresh and try again." ],
    422: [ "vpUi.http.422", "Submitted content does not meet requirements." ],
    429: [ "vpUi.http.429", "Too many requests. Please try again later." ],
    500: [ "vpUi.http.500", "Service is temporarily unavailable. Engineers have been notified." ],
    502: [ "vpUi.http.502", "Upstream service unreachable. Please try again later." ],
    503: [ "vpUi.http.503", "Service under maintenance. Please try again later." ],
    504: [ "vpUi.http.504", "Request timed out. Please try again later." ]
  };
  var ERROR_PATTERNS = [ {
    re: /invalid amount for a marketable .* min size:\s*\$?([\d.]+)/i,
    build: function(m) {
      return {
        title: _T("vpUi.clob.minNotional.title", "Order amount too low"),
        body: _T("vpUi.clob.minNotional.body", "Your order notional is below the platform minimum (" + m[1] + " USDC). Please increase your order amount or pick a market with price closer to the cap.")
      };
    }
  }, {
    re: /Size\s*\(([\d.]+)\)\s*lower than the minimum:\s*([\d.]+)/i,
    build: function(m) {
      return {
        title: _T("vpUi.clob.minSize.title", "Order size too small"),
        body: _T("vpUi.clob.minSize.body", "Your order size (" + m[1] + " shares) is below the platform minimum (" + m[2] + " shares). Please increase your order amount.")
      };
    }
  }, {
    re: /insufficient.*(balance|funds)/i,
    build: function() {
      return {
        title: _T("vpUi.clob.insufficient.title", "Insufficient balance"),
        body: _T("vpUi.clob.insufficient.body", "Your wallet balance is insufficient to complete this order. Please deposit funds or reduce the order amount.")
      };
    }
  }, {
    re: /invalid amounts.*max accuracy/i,
    build: function() {
      return {
        title: _T("vpUi.clob.tickSize.title", "Price precision error"),
        body: _T("vpUi.clob.tickSize.body", "Order price or size precision does not meet platform requirements. The system has auto-adjusted; please retry. If this recurs please contact support.")
      };
    }
  }, {
    re: /not allowed.*resolved|market.*resolved|market closed/i,
    build: function() {
      return {
        title: _T("vpUi.clob.marketResolved.title", "Market resolved"),
        body: _T("vpUi.clob.marketResolved.body", "This market has been resolved or closed and is no longer tradable.")
      };
    }
  }, {
    re: /timeout|timed out/i,
    build: function() {
      return {
        title: _T("vpUi.clob.timeout.title", "Request timeout"),
        body: _T("vpUi.clob.timeout.body", "Network response timed out. Please check your connection and try again.")
      };
    }
  }, {
    re: /signature|sign(ing|ature) failed|nonce/i,
    build: function() {
      return {
        title: _T("vpUi.clob.sigFail.title", "Signature failed"),
        body: _T("vpUi.clob.sigFail.body", "Wallet signature verification failed. Please refresh the page and sign in again.")
      };
    }
  } ];
  function humanizeError(err) {
    var raw = "";
    var status = null;
    if (err == null) raw = ""; else if (typeof err === "string") raw = err; else if (err.error) raw = String(err.error); else if (err.message) raw = String(err.message); else if (err.detail) raw = String(err.detail); else {
      try {
        raw = JSON.stringify(err);
      } catch (_) {
        raw = String(err);
      }
    }
    if (err && typeof err === "object") {
      status = err.status || err.status_code || err.http_status || null;
    }
    for (var i = 0; i < ERROR_PATTERNS.length; i++) {
      var m = raw.match(ERROR_PATTERNS[i].re);
      if (m) {
        var r = ERROR_PATTERNS[i].build(m);
        r.detail = raw;
        return r;
      }
    }
    if (status && HTTP_KEYS[status]) {
      var pair = HTTP_KEYS[status];
      return {
        title: _T("vpUi.unknown.title", "Operation incomplete"),
        body: _T(pair[0], pair[1]),
        detail: raw
      };
    }
    return {
      title: _T("vpUi.unknown.title", "Operation incomplete"),
      body: raw && raw.length < 120 ? raw : _T("vpUi.unknown.body", "The system encountered an unexpected problem. Please try again later. If this recurs please contact support."),
      detail: raw && raw.length >= 120 ? raw : ""
    };
  }
  var _stack = null;
  function _ensureStack() {
    if (!_stack) {
      _stack = _h("div", "vp-toast-stack");
      document.body.appendChild(_stack);
    }
    return _stack;
  }
  function toast(msg, type, duration) {
    type = type || "info";
    duration = duration == null ? 3800 : duration;
    var s = _ensureStack();
    var item = _h("div", "vp-toast vp-toast-" + type);
    var title = "";
    var text = msg || "";
    if (typeof msg === "object" && msg) {
      title = msg.title || "";
      text = msg.body || msg.text || msg.message || "";
    }
    item.innerHTML = '<div class="vp-toast-icon">' + _icon(type === "error" ? "danger" : type) + "</div>" + '<div class="vp-toast-body">' + (title ? '<div class="vp-toast-title">' + _esc(title) + "</div>" : "") + '<div class="vp-toast-text">' + _esc(text) + "</div>" + "</div>" + '<button type="button" class="vp-toast-close" aria-label="' + _esc(_T("vpUi.modal.close", "Close")) + '">×</button>';
    s.appendChild(item);
    requestAnimationFrame(function() {
      item.classList.add("show");
    });
    var t = setTimeout(close, duration);
    item.querySelector(".vp-toast-close").addEventListener("click", function() {
      clearTimeout(t);
      close();
    });
    function close() {
      item.classList.remove("show");
      setTimeout(function() {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, 220);
    }
  }
  function _modal(opts) {
    return new Promise(function(resolve) {
      opts = opts || {};
      var type = opts.type || "info";
      var actions = opts.actions || [ {
        label: _T("vpUi.modal.gotIt", "Got it"),
        value: true,
        variant: "primary"
      } ];
      var width = opts.width || "480px";
      var backdrop = _h("div", "vp-modal-backdrop");
      var card = _h("div", "vp-modal");
      card.style.width = "min(92vw, " + width + ")";
      var detailHtml = "";
      if (opts.detail) {
        detailHtml = '<div class="vp-detail">' + _esc(opts.detail) + "</div>";
      }
      card.innerHTML = '<div class="vp-modal-header">' + '<div class="vp-modal-icon ' + type + '">' + _icon(type) + "</div>" + '<div class="vp-modal-title">' + _esc(opts.title || _T("vpUi.modal.notice", "Notice")) + "</div>" + (opts.dismissible !== false ? '<button type="button" class="vp-modal-close" aria-label="' + _esc(_T("vpUi.modal.close", "Close")) + '">×</button>' : "") + "</div>" + '<div class="vp-modal-body">' + (opts.bodyHtml ? opts.bodyHtml : _esc(opts.body || "")) + detailHtml + "</div>" + '<div class="vp-modal-footer"></div>';
      var footer = card.querySelector(".vp-modal-footer");
      if (!actions || actions.length === 0) {
        footer.parentNode.removeChild(footer);
      } else {
        actions.forEach(function(act) {
          var btn = _h("button", "vp-btn vp-btn-" + (act.variant || "secondary"));
          btn.type = "button";
          btn.textContent = act.label;
          btn.addEventListener("click", function() {
            close(act.value);
          });
          footer.appendChild(btn);
        });
      }
      backdrop.appendChild(card);
      document.body.appendChild(backdrop);
      requestAnimationFrame(function() {
        backdrop.classList.add("show");
      });
      function close(val) {
        backdrop.classList.remove("show");
        setTimeout(function() {
          if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        }, 220);
        document.removeEventListener("keydown", onKey);
        resolve(val);
      }
      function onKey(e) {
        if (e.key === "Escape" && opts.dismissible !== false) close(undefined);
      }
      document.addEventListener("keydown", onKey);
      if (opts.dismissible !== false) {
        var closeBtn = card.querySelector(".vp-modal-close");
        if (closeBtn) closeBtn.addEventListener("click", function() {
          close(undefined);
        });
        backdrop.addEventListener("click", function(e) {
          if (e.target === backdrop) close(undefined);
        });
      }
    });
  }
  function confirm(opts) {
    opts = opts || {};
    var danger = !!opts.danger;
    return _modal({
      type: danger ? "danger" : opts.type || "warning",
      title: opts.title || _T("vpUi.modal.confirm", "Confirm action"),
      body: opts.body || opts.message || "",
      bodyHtml: opts.bodyHtml,
      actions: [ {
        label: opts.cancelText || _T("vpUi.modal.cancel", "Cancel"),
        value: false,
        variant: "secondary"
      }, {
        label: opts.okText || _T("vpUi.modal.confirmBtn", "Confirm"),
        value: true,
        variant: danger ? "danger" : "primary"
      } ]
    }).then(function(v) {
      return v === true;
    });
  }
  function alert(opts) {
    if (typeof opts === "string") opts = {
      body: opts
    };
    opts = opts || {};
    return _modal({
      type: opts.type || "info",
      title: opts.title || _T("vpUi.modal.notice", "Notice"),
      body: opts.body || "",
      bodyHtml: opts.bodyHtml,
      actions: [ {
        label: opts.okText || _T("vpUi.modal.gotIt", "Got it"),
        value: true,
        variant: "primary"
      } ]
    });
  }
  function error(err, opts) {
    opts = opts || {};
    var f = humanizeError(err);
    return _modal({
      type: "danger",
      title: opts.title || f.title,
      body: f.body,
      detail: opts.showDetail !== false ? f.detail : "",
      actions: [ {
        label: opts.okText || _T("vpUi.modal.gotIt", "Got it"),
        value: true,
        variant: "secondary"
      } ]
    });
  }
  function success(opts) {
    if (typeof opts === "string") opts = {
      body: opts
    };
    opts = opts || {};
    return _modal({
      type: "success",
      title: opts.title || _T("vpUi.modal.success", "Success"),
      body: opts.body || "",
      bodyHtml: opts.bodyHtml,
      actions: [ {
        label: opts.okText || _T("vpUi.modal.ok", "OK"),
        value: true,
        variant: "success"
      } ]
    });
  }
  window.vp = {
    __loaded: true,
    toast: toast,
    confirm: confirm,
    alert: alert,
    error: error,
    success: success,
    modal: _modal,
    humanizeError: humanizeError
  };
})();