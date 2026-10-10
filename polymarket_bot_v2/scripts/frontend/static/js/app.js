(function initNav() {
  const path = location.pathname.replace(/\/$/, "") || "/v2";
  document.querySelectorAll(".nav-item[data-page], .nav-sub-item[data-page]").forEach(el => {
    const href = (el.getAttribute("href") || "").split("?")[0];
    if (href === path || path === "/v2" && href === "/v2") {
      el.classList.add("active");
      const group = el.closest(".nav-group");
      if (group) group.classList.add("open");
    }
  });
})();

(function selfHealTopbarAuthState() {
  function maskAddr(addr) {
    if (!addr || addr === "__admin__") return "";
    return String(addr).slice(0, 6) + "..." + String(addr).slice(-4);
  }
  function fetchMe() {
    if (window.veyraGetMe) {
      return window.veyraGetMe({
        force: true
      }).catch(function() {
        return null;
      });
    }
    var sep = "/api/user/me".indexOf("?") === -1 ? "?" : "&";
    return fetch("/api/user/me" + sep + "_=" + Date.now(), {
      credentials: "include",
      cache: "no-store",
      headers: {
        "X-Requested-With": "XMLHttpRequest"
      }
    }).then(function(r) {
      return r.ok ? r.json() : null;
    }).catch(function() {
      return null;
    });
  }
  function paintHeaderUser(me) {
    if (!me || me.authenticated === false) return false;
    var el = document.getElementById("header-user");
    if (!el) return false;
    var displayName = me.display_name || maskAddr(me.wallet_address) || "User";
    el.textContent = displayName;
    try {
      var btn = document.getElementById("vp-user-avatar-btn");
      if (btn && btn.dataset && btn.dataset.guestBound && window.__veyraGuestClickHandler) {
        btn.removeEventListener("click", window.__veyraGuestClickHandler, true);
        delete btn.dataset.guestBound;
      }
    } catch (_) {}
    return true;
  }
  function needsHeal() {
    var el = document.getElementById("header-user");
    if (!el) return false;
    var txt = String(el.textContent || "").trim().toLowerCase();
    return txt === "sign in" || txt === "登录";
  }
  function runHealOnce() {
    if (!needsHeal()) return;
    fetchMe().then(function(me) {
      paintHeaderUser(me);
    }).catch(function() {});
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runHealOnce, {
      once: true
    });
  } else {
    runHealOnce();
  }
  window.setTimeout(runHealOnce, 500);
  window.setTimeout(runHealOnce, 1400);
  window.setTimeout(runHealOnce, 2600);
})();

(function bindOnboardingClose() {
  function closeOnboarding() {
    try {
      var host = document.getElementById("onboarding-host");
      if (host) host.innerHTML = "";
      document.body.classList.remove("onboarding-active");
      if (window.htmx && typeof window.htmx.trigger === "function") {
        window.htmx.trigger(document.body, "refresh-topbar");
      }
    } catch (_) {}
    try {
      if (window.veyraGetMe) {
        window.veyraGetMe({
          force: true
        }).catch(function() {});
      }
    } catch (_) {}
  }
  document.body.addEventListener("close-onboarding", closeOnboarding);
})();

(function initOnboardingBootstrap() {
  function closeOnboardingVanilla() {
    try {
      var host = document.getElementById("onboarding-host");
      if (host) host.innerHTML = "";
      document.body.classList.remove("onboarding-active");
    } catch (_) {}
    try {
      if (window.htmx && typeof window.htmx.trigger === "function") {
        window.htmx.trigger(document.body, "refresh-topbar");
      }
    } catch (_) {}
    try {
      if (window.veyraGetMe) {
        window.veyraGetMe({
          force: true
        }).catch(function() {});
      }
    } catch (_) {}
  }
  function syncOnboardingInputState() {
    try {
      var input = document.getElementById("onboard-display-name");
      var hint = document.getElementById("onboard-name-hint");
      if (!input || !hint) return;
      var walletRe = /^0x[a-fA-F0-9]{40}$/;
      var validHint = input.getAttribute("data-valid-hint") || "Looks good.";
      var invalidHint = input.getAttribute("data-invalid-hint") || "Use 2-40 valid characters and avoid raw wallet address.";
      var v = (input.value || "").trim();
      var ok = v.length >= 2 && v.length <= 40 && !walletRe.test(v);
      input.classList.toggle("ob-input-valid", ok);
      input.classList.toggle("ob-input-invalid", !ok && v.length > 0);
      hint.textContent = ok ? validHint : invalidHint;
      hint.classList.toggle("ob-hint-valid", ok);
    } catch (_) {}
  }
  function getOnboardingFocusable() {
    var modal = document.getElementById("onboarding-modal");
    if (!modal) return [];
    return Array.prototype.slice.call(modal.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')).filter(function(el) {
      return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    });
  }
  function focusOnboardingPrimary() {
    var modal = document.getElementById("onboarding-modal");
    if (!modal) return;
    var preferred = modal.querySelector('#onboard-display-name, input[type="checkbox"], button[type="submit"]');
    if (preferred && typeof preferred.focus === "function") {
      preferred.focus();
    }
  }
  function activateOnboardingHost() {
    try {
      var host = document.getElementById("onboarding-host");
      if (!host) return;
      if ((host.innerHTML || "").trim()) {
        document.body.classList.add("onboarding-active");
        if (window.lucide) lucide.createIcons();
        syncOnboardingInputState();
        window.setTimeout(focusOnboardingPrimary, 30);
      }
    } catch (_) {}
  }
  function executeInjectedScripts(host) {
    if (!host) return;
    var oldScripts = host.querySelectorAll("script");
    for (var i = 0; i < oldScripts.length; i++) {
      var old = oldScripts[i];
      var fresh = document.createElement("script");
      for (var a = 0; a < old.attributes.length; a++) {
        var attr = old.attributes[a];
        try {
          fresh.setAttribute(attr.name, attr.value);
        } catch (_) {}
      }
      if (!old.src && old.textContent) {
        fresh.textContent = old.textContent;
      }
      old.parentNode.replaceChild(fresh, old);
    }
  }
  function renderOnboardingHtml(html) {
    var host = document.getElementById("onboarding-host");
    if (!host) return;
    host.innerHTML = html || "";
    if ((html || "").trim()) {
      executeInjectedScripts(host);
      activateOnboardingHost();
    } else {
      closeOnboardingVanilla();
    }
  }
  function fetchOnboarding(url, opts) {
    return fetch(url, Object.assign({
      credentials: "include"
    }, opts || {}));
  }
  function bootOnboarding() {
    var host = document.getElementById("onboarding-host");
    if (!host) return;
    if (document.getElementById("onboarding-modal")) return;
    var attempts = 0;
    var maxAttempts = 4;
    function requestModal() {
      host = document.getElementById("onboarding-host");
      if (!host) return;
      if (document.getElementById("onboarding-modal")) return;
      if ((host.innerHTML || "").trim()) return;
      if (attempts >= maxAttempts) return;
      attempts += 1;
      fetchOnboarding("/api/user/onboard-modal").then(function(resp) {
        if (!resp || resp.status === 204 || resp.status === 401 || resp.status === 403) return "";
        return resp.text();
      }).then(function(html) {
        if ((html || "").trim()) {
          renderOnboardingHtml(html);
          return;
        }
        if (!document.getElementById("onboarding-modal") && attempts < maxAttempts) {
          window.setTimeout(requestModal, attempts === 1 ? 450 : 900);
        }
      }).catch(function() {
        if (!document.getElementById("onboarding-modal") && attempts < maxAttempts) {
          window.setTimeout(requestModal, attempts === 1 ? 450 : 900);
        }
      });
    }
    window.setTimeout(requestModal, 120);
  }
  document.addEventListener("input", function(ev) {
    if (ev.target && ev.target.id === "onboard-display-name") {
      syncOnboardingInputState();
    }
  });
  document.addEventListener("keydown", function(ev) {
    var modal = document.getElementById("onboarding-modal");
    if (!modal) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    if (ev.key !== "Tab") return;
    var focusables = getOnboardingFocusable();
    if (!focusables.length) return;
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }, true);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootOnboarding, {
      once: true
    });
  } else {
    bootOnboarding();
  }
})();

const _toastContainer = (() => {
  let c = document.getElementById("toast-container");
  if (!c) {
    c = document.createElement("div");
    c.id = "toast-container";
    c.className = "toast-container";
    document.body.appendChild(c);
  }
  return c;
})();

function _t(key, fb) {
  try {
    if (window.veyraI18n && typeof window.veyraI18n.t === "function") return window.veyraI18n.t(key, fb);
  } catch (_e) {}
  return fb;
}

function showToast(msg, type = "success", duration = 4e3) {
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  const iconMap = {
    success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#F04438" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    warning: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#F59E0B" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
  };
  t.innerHTML = `${iconMap[type] || ""}<span>${msg}</span>`;
  _toastContainer.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    t.style.transform = "translateX(100%)";
    t.style.transition = "all 0.3s";
    setTimeout(() => t.remove(), 300);
  }, duration);
}

async function vFetch(url, opts = {}) {
  const userSession = getCookie("user_session") || "";
  const headers = {
    ...opts.headers || {}
  };
  if (userSession) headers["x-user-session"] = userSession;
  try {
    const resp = await fetch(url, {
      ...opts,
      headers: headers
    });
    if (resp.status === 401) {
      showToast(_t("sidebar.status.toast.authFail", "Authentication failed, please log in again"), "error");
      return null;
    }
    if (resp.status === 403) {
      showToast(_t("sidebar.status.toast.noPerm", "Permission denied"), "error");
      return null;
    }
    return resp;
  } catch (e) {
    showToast(_t("sidebar.status.toast.netFail", "Network request failed"), "error");
    console.error("[vFetch]", e);
    return null;
  }
}

async function vJson(url, opts = {}) {
  const resp = await vFetch(url, opts);
  if (!resp) return null;
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

function fmt$(v, dec = 2) {
  const n = parseFloat(v);
  if (isNaN(n)) return "-";
  return "$" + n.toFixed(dec).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function fmtPct(v, dec = 1) {
  const n = parseFloat(v);
  if (isNaN(n)) return "-";
  return n.toFixed(dec) + "%";
}

function fmtNum(v, dec = 2) {
  const n = parseFloat(v);
  if (isNaN(n)) return "-";
  return n.toFixed(dec).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function fmtPnL(v, dec = 2) {
  const n = parseFloat(v);
  if (isNaN(n)) return "-";
  const s = n >= 0 ? "+" : "";
  return s + "$" + n.toFixed(dec);
}

function pnlClass(v) {
  const n = parseFloat(v);
  if (isNaN(n) || n === 0) return "";
  return n > 0 ? "text-success" : "text-danger";
}

function timeAgo(ts) {
  if (!ts) return "-";
  const d = new Date(ts);
  const now = new Date;
  const sec = Math.floor((now - d) / 1e3);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function fmtDate(ts) {
  if (!ts) return "-";
  const d = new Date(ts);
  return d.toLocaleDateString("zh-CN", {
    month: "2-digit",
    day: "2-digit"
  }) + " " + d.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit"
  });
}

let _sidebarRuntimeTimer = null;

async function _runtimeJson(url) {
  const userSession = getCookie("user_session") || "";
  if (!userSession) return null;
  const headers = {
    "x-user-session": userSession
  };
  try {
    const resp = await fetch(url, {
      headers: headers
    });
    if (!resp.ok) return null;
    return await resp.json().catch(() => null);
  } catch {
    return null;
  }
}

function _setSidebarStatusError(text) {
  const banner = document.getElementById("sidebar-status-banner");
  if (banner) {
    banner.className = "sidebar-status-banner hidden";
    banner.textContent = "";
  }
  const pills = document.getElementById("sidebar-status-pills");
  if (pills && !pills.children.length) {
    _renderSidebarStatusPills([ {
      level: "green",
      label: _t("sidebar.status.autoOn", "Algo")
    }, {
      level: "green",
      label: _t("sidebar.status.normal", "Normal")
    } ]);
  }
}

function _renderSidebarStatusPills(items) {
  const banner = document.getElementById("sidebar-status-banner");
  const pills = document.getElementById("sidebar-status-pills");
  if (!banner || !pills) return;
  banner.className = "sidebar-status-banner hidden";
  banner.textContent = "";
  const sig = JSON.stringify(items);
  if (pills.dataset.sig === sig) return;
  pills.dataset.sig = sig;
  try {
    sessionStorage.setItem("vp_sidebar_pills", sig);
  } catch (e) {}
  pills.innerHTML = "";
  items.forEach(item => {
    const pill = document.createElement("span");
    pill.className = `sidebar-state-pill ${item.level || "green"}`;
    const dot = document.createElement("span");
    dot.className = `state-dot ${item.level || "green"}`;
    const label = document.createElement("span");
    label.className = "state-label";
    label.textContent = item.label || "-";
    pill.appendChild(dot);
    pill.appendChild(label);
    pills.appendChild(pill);
  });
}

function _renderGlobalEmergencyFreeze(text) {
  const banner = document.getElementById("sidebar-status-banner");
  const pills = document.getElementById("sidebar-status-pills");
  if (!banner || !pills) return;
  banner.className = "sidebar-status-banner danger";
  banner.textContent = text || _t("sidebar.status.toast.frozenBanner", "Global emergency freeze");
  pills.innerHTML = "";
}

async function refreshSidebarRuntimeStatus() {
  const pills = document.getElementById("sidebar-status-pills");
  if (!pills) return;
  const runtime = await _runtimeJson("/api/user/runtime-control");
  if (!runtime) {
    _setSidebarStatusError(_t("sidebar.status.fetchError", "Status fetch error"));
    return;
  }
  const globalEmergency = !!runtime.emergency_freeze;
  const maintenanceMode = !!runtime.maintenance_mode;
  if (globalEmergency || maintenanceMode) {
    _renderGlobalEmergencyFreeze(maintenanceMode ? _t("sidebar.status.maintenance", "Maintenance") : _t("sidebar.status.emergency", "Emergency Freeze"));
    return;
  }
  if (runtime.role === "manual_user") {
    const autoEnabled = !!runtime.auto_trading_enabled;
    const frozen = !!runtime.freeze_auto;
    let items = [];
    if (frozen) {
      items = [ {
        level: "red",
        label: _t("sidebar.status.frozen", "Frozen")
      }, {
        level: "green",
        label: _t("sidebar.status.autoSell", "Auto Sell")
      } ];
    } else {
      items = [ {
        level: autoEnabled ? "green" : "red",
        label: autoEnabled ? _t("sidebar.status.autoOn", "Algo") : _t("sidebar.status.autoOff", "Botoff")
      }, {
        level: "green",
        label: _t("sidebar.status.normal", "Normal")
      } ];
    }
    _renderSidebarStatusPills(items);
    return;
  }
  const adminState = await _runtimeJson("/api/system/admin/state");
  const globalFrozen = !!(adminState && adminState.emergency_freeze);
  const globalMaintenance = !!(adminState && adminState.maintenance_mode);
  if (globalFrozen || globalMaintenance) {
    _renderGlobalEmergencyFreeze(globalMaintenance ? _t("sidebar.status.maintenance", "Maintenance") : _t("sidebar.status.emergency", "Emergency Freeze"));
    return;
  }
  _renderSidebarStatusPills([ {
    level: "green",
    label: _t("sidebar.status.autoOn", "Algo")
  }, {
    level: "green",
    label: _t("sidebar.status.normal", "Normal")
  } ]);
}

function showSkeleton(container, count = 3, height = 60) {
  const el = typeof container === "string" ? document.getElementById(container) : container;
  if (!el) return;
  el.innerHTML = Array.from({
    length: count
  }, () => `<div class="skeleton" style="height:${height}px;margin-bottom:8px"></div>`).join("");
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) lucide.createIcons();
  const requiresRuntime = !!(document.body && (document.body.getAttribute("data-requires-runtime") === "1" || document.body.getAttribute("data-requires-pm") === "1"));
  if (!requiresRuntime) {
    return;
  }
  try {
    const cached = sessionStorage.getItem("vp_sidebar_pills");
    if (cached) {
      const items = JSON.parse(cached);
      const pills = document.getElementById("sidebar-status-pills");
      if (pills) {
        pills.dataset.sig = cached;
        items.forEach(item => {
          const pill = document.createElement("span");
          pill.className = `sidebar-state-pill ${item.level || "green"}`;
          const dot = document.createElement("span");
          dot.className = `state-dot ${item.level || "green"}`;
          const label = document.createElement("span");
          label.className = "state-label";
          label.textContent = item.label || "-";
          pill.appendChild(dot);
          pill.appendChild(label);
          pills.appendChild(pill);
        });
      }
    } else {
      _renderSidebarStatusPills([ {
        level: "green",
        label: _t("sidebar.status.autoOn", "Algo")
      }, {
        level: "green",
        label: _t("sidebar.status.normal", "Normal")
      } ]);
    }
  } catch (e) {}
  refreshSidebarRuntimeStatus();
  if (_sidebarRuntimeTimer) clearInterval(_sidebarRuntimeTimer);
  _sidebarRuntimeTimer = setInterval(refreshSidebarRuntimeStatus, 3e4);
  document.addEventListener("i18n:changed", () => {
    const pills = document.getElementById("sidebar-status-pills");
    if (pills) pills.dataset.sig = "";
    try {
      sessionStorage.removeItem("vp_sidebar_pills");
    } catch (_e) {}
    refreshSidebarRuntimeStatus();
  });
});