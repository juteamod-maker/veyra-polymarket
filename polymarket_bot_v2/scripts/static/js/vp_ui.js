
(function () {
  'use strict';
  if (window.vp && window.vp.__loaded) return;

  function _h(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html != null) el.innerHTML = html;
    return el;
  }
  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function _icon(type) {
    var map = {
      info:    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
      success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
      warning: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
      danger:  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>'
    };
    return map[type] || map.info;
  }

  var HTTP_FRIENDLY = {
    400: '请求参数有误，请检查输入',
    401: '登录已过期，请重新登录',
    403: '您没有权限执行该操作',
    404: '资源不存在或已被移除',
    409: '操作冲突，请刷新后重试',
    422: '提交内容不符合要求',
    429: '操作过于频繁，请稍后再试',
    500: '服务暂时遇到问题，工程师已收到通知',
    502: '上游服务暂时无法连接，请稍后重试',
    503: '服务正在维护，请稍后再试',
    504: '请求超时，请稍后再试'
  };

  var ERROR_PATTERNS = [
    {
      re: /invalid amount for a marketable .* min size:\s*\$?([\d.]+)/i,
      build: function (m) {
        return {
          title: '下单金额过低',
          body: '本次下单的实际成交金额低于平台最低限制（' + m[1] + ' USDC）。请尝试增加下单金额，或选择价格更接近上限的市场。'
        };
      }
    },
    {
      re: /Size\s*\(([\d.]+)\)\s*lower than the minimum:\s*([\d.]+)/i,
      build: function (m) {
        return {
          title: '下单份额过少',
          body: '本次下单份额（' + m[1] + ' 份）低于平台最低要求（' + m[2] + ' 份）。请增加下单金额以满足最低份额。'
        };
      }
    },
    {
      re: /insufficient.*(balance|funds)/i,
      build: function () { return { title: '余额不足', body: '您的钱包余额不足以完成本次下单，请先充值或减少下单金额。' }; }
    },
    {
      re: /invalid amounts.*max accuracy/i,
      build: function () { return { title: '价格精度错误', body: '订单价格或份额精度不符合平台要求，系统已自动调整，请重试。如反复出现请联系客服。' }; }
    },
    {
      re: /not allowed.*resolved|market.*resolved|market closed/i,
      build: function () { return { title: '市场已结算', body: '该市场已结算或已关闭，无法继续交易。' }; }
    },
    {
      re: /timeout|timed out/i,
      build: function () { return { title: '请求超时', body: '网络响应超时，请检查网络后重试。' }; }
    },
    {
      re: /signature|sign(ing|ature) failed|nonce/i,
      build: function () { return { title: '签名失败', body: '钱包签名验证失败，请刷新页面重新登录。' }; }
    }
  ];

  function humanizeError(err) {
    var raw = '';
    var status = null;
    if (err == null) raw = '';
    else if (typeof err === 'string') raw = err;
    else if (err.error) raw = String(err.error);
    else if (err.message) raw = String(err.message);
    else if (err.detail) raw = String(err.detail);
    else { try { raw = JSON.stringify(err); } catch(_) { raw = String(err); } }
    if (err && typeof err === 'object') {
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
    if (status && HTTP_FRIENDLY[status]) {
      return { title: '操作未完成', body: HTTP_FRIENDLY[status], detail: raw };
    }
    return {
      title: '操作未完成',
      body: raw && raw.length < 120 ? raw : '系统遇到了一个未预期的问题，请稍后重试。如反复出现请联系客服。',
      detail: raw && raw.length >= 120 ? raw : ''
    };
  }

  var _stack = null;
  function _ensureStack() {
    if (!_stack) {
      _stack = _h('div', 'vp-toast-stack');
      document.body.appendChild(_stack);
    }
    return _stack;
  }
  function toast(msg, type, duration) {
    type = type || 'info';
    duration = duration == null ? 3800 : duration;
    var s = _ensureStack();
    var item = _h('div', 'vp-toast vp-toast-' + type);
    var title = '';
    var text = msg || '';
    if (typeof msg === 'object' && msg) {
      title = msg.title || '';
      text = msg.body || msg.text || msg.message || '';
    }
    item.innerHTML =
      '<div class="vp-toast-icon">' + _icon(type === 'error' ? 'danger' : type) + '</div>' +
      '<div class="vp-toast-body">' +
        (title ? '<div class="vp-toast-title">' + _esc(title) + '</div>' : '') +
        '<div class="vp-toast-text">' + _esc(text) + '</div>' +
      '</div>' +
      '<button type="button" class="vp-toast-close" aria-label="关闭">×</button>';
    s.appendChild(item);
    requestAnimationFrame(function () { item.classList.add('show'); });
    var t = setTimeout(close, duration);
    item.querySelector('.vp-toast-close').addEventListener('click', function () { clearTimeout(t); close(); });
    function close() {
      item.classList.remove('show');
      setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 220);
    }
  }

  function _modal(opts) {
    return new Promise(function (resolve) {
      opts = opts || {};
      var type = opts.type || 'info';
      var actions = opts.actions || [{ label: '我知道了', value: true, variant: 'primary' }];
      var width = opts.width || '480px';

      var backdrop = _h('div', 'vp-modal-backdrop');
      var card = _h('div', 'vp-modal');
      card.style.width = 'min(92vw, ' + width + ')';
      var detailHtml = '';
      if (opts.detail) {
        detailHtml = '<div class="vp-detail">' + _esc(opts.detail) + '</div>';
      }
      card.innerHTML =
        '<div class="vp-modal-header">' +
          '<div class="vp-modal-icon ' + type + '">' + _icon(type) + '</div>' +
          '<div class="vp-modal-title">' + _esc(opts.title || '提示') + '</div>' +
          (opts.dismissible !== false ? '<button type="button" class="vp-modal-close" aria-label="关闭">×</button>' : '') +
        '</div>' +
        '<div class="vp-modal-body">' + (opts.bodyHtml ? opts.bodyHtml : _esc(opts.body || '')) + detailHtml + '</div>' +
        '<div class="vp-modal-footer"></div>';
      var footer = card.querySelector('.vp-modal-footer');
      actions.forEach(function (act) {
        var btn = _h('button', 'vp-btn vp-btn-' + (act.variant || 'secondary'));
        btn.type = 'button';
        btn.textContent = act.label;
        btn.addEventListener('click', function () { close(act.value); });
        footer.appendChild(btn);
      });
      backdrop.appendChild(card);
      document.body.appendChild(backdrop);
      requestAnimationFrame(function () { backdrop.classList.add('show'); });

      function close(val) {
        backdrop.classList.remove('show');
        setTimeout(function () {
          if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        }, 220);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function onKey(e) {
        if (e.key === 'Escape' && opts.dismissible !== false) close(undefined);
      }
      document.addEventListener('keydown', onKey);
      if (opts.dismissible !== false) {
        var closeBtn = card.querySelector('.vp-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', function () { close(undefined); });
        backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(undefined); });
      }
    });
  }

  function confirm(opts) {
    opts = opts || {};
    var danger = !!opts.danger;
    return _modal({
      type: danger ? 'danger' : (opts.type || 'warning'),
      title: opts.title || '确认操作',
      body: opts.body || opts.message || '',
      bodyHtml: opts.bodyHtml,
      actions: [
        { label: opts.cancelText || '取消', value: false, variant: 'secondary' },
        { label: opts.okText || '确认', value: true, variant: danger ? 'danger' : 'primary' }
      ]
    }).then(function (v) { return v === true; });
  }

  function alert(opts) {
    if (typeof opts === 'string') opts = { body: opts };
    opts = opts || {};
    return _modal({
      type: opts.type || 'info',
      title: opts.title || '提示',
      body: opts.body || '',
      bodyHtml: opts.bodyHtml,
      actions: [{ label: opts.okText || '我知道了', value: true, variant: 'primary' }]
    });
  }

  function error(err, opts) {
    opts = opts || {};
    var f = humanizeError(err);
    return _modal({
      type: 'danger',
      title: opts.title || f.title,
      body: f.body,
      detail: opts.showDetail !== false ? f.detail : '',
      actions: [{ label: opts.okText || '我知道了', value: true, variant: 'secondary' }]
    });
  }

  function success(opts) {
    if (typeof opts === 'string') opts = { body: opts };
    opts = opts || {};
    return _modal({
      type: 'success',
      title: opts.title || '操作成功',
      body: opts.body || '',
      bodyHtml: opts.bodyHtml,
      actions: [{ label: opts.okText || '好的', value: true, variant: 'success' }]
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
