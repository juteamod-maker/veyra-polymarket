
(function () {
  var _secLoaded = false;
  var origSwitch = window.switchTab;
  window.switchTab = function (t) {
    origSwitch(t);
    if (t === 'security' && !_secLoaded) {
      _secLoaded = true;
      loadSecurityOverview();
      loadBlacklist();
      loadTempBans();
      loadWhitelist();
      loadSecurityEvents();
    }
  };
  var currentTab = localStorage.getItem('polymarket_active_tab');
  if (currentTab === 'security' && !_secLoaded) {
    _secLoaded = true;
    loadSecurityOverview();
    loadBlacklist();
    loadTempBans();
    loadWhitelist();
    loadSecurityEvents();
  }
})();

function loadSecurityOverview() {
  api('/api/security/overview').then(function (d) {
    if (!d) return;
    document.getElementById('sec-bl-count').textContent = d.blacklist_count || 0;
    document.getElementById('sec-tb-count').textContent = d.temp_ban_count || 0;
    document.getElementById('sec-wl-count').textContent = d.whitelist_count || 0;
    document.getElementById('sec-auto-threshold').textContent = d.auto_ban_threshold || 3;
    var inp = document.getElementById('sec-auto-threshold-input');
    if (inp) inp.value = d.auto_ban_threshold || 3;
  });
}

function loadBlacklist() {
  api('/api/security/blacklist').then(function (d) {
    if (!d) return;
    var container = document.getElementById('sec-bl-list');
    var bl = d.blacklist || {};
    var ips = Object.keys(bl);
    if (ips.length === 0) {
      container.innerHTML = '<div class="text-gray-500 text-xs py-2">暂无黑名单 IP</div>';
      return;
    }
    var html = '<table class="w-full text-xs"><thead><tr class="text-gray-500 border-b border-gray-700">' +
      '<th class="py-1 text-left">IP</th><th class="py-1 text-left">原因</th>' +
      '<th class="py-1 text-left">来源</th><th class="py-1 text-left">时间</th>' +
      '<th class="py-1 text-right">操作</th></tr></thead><tbody>';
    ips.forEach(function (ip) {
      var info = bl[ip];
      var ts = info.ts ? new Date(info.ts).toLocaleString() : '-';
      var src = info.source === 'auto' ? '<span class="text-yellow">⚡自动</span>'
        : info.source === 'manual_promote' ? '<span class="text-orange-400">⬆️升级</span>'
        : '<span class="text-gray-400">✋手动</span>';
      html += '<tr class="border-b border-gray-800 hover:bg-gray-800/50">' +
        '<td class="py-2 font-mono text-red-400">' + _esc(ip) + '</td>' +
        '<td class="py-2 text-gray-300">' + _esc(info.reason || '') + '</td>' +
        '<td class="py-2">' + src + '</td>' +
        '<td class="py-2 text-gray-500">' + ts + '</td>' +
        '<td class="py-2 text-right"><button onclick="removeFromBlacklist(\'' + _esc(ip) + '\')" ' +
        'class="text-red-400 hover:text-red-300 text-xs">🗑️ 移除</button></td></tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  });
}

function addToBlacklist() {
  var ip = document.getElementById('sec-bl-ip').value.trim();
  var reason = document.getElementById('sec-bl-reason').value.trim() || '手动添加';
  if (!ip) return;
  api('/api/security/blacklist', { method: 'POST', body: JSON.stringify({ ip: ip, reason: reason }) })
    .then(function (d) {
      if (d && d.ok) {
        document.getElementById('sec-bl-ip').value = '';
        document.getElementById('sec-bl-reason').value = '';
        loadBlacklist();
        loadSecurityOverview();
        loadSecurityEvents();
      } else if (d && d.error) {
        alert(d.error);
      }
    }).catch(function (e) { console.error('addToBlacklist:', e); });
}

function removeFromBlacklist(ip) {
  if (!confirm('确认从黑名单移除 ' + ip + '？')) return;
  api('/api/security/blacklist/' + ip, { method: 'DELETE' })
    .then(function (d) {
      if (d && d.ok) {
        loadBlacklist();
        loadSecurityOverview();
        loadSecurityEvents();
      }
    }).catch(function (e) { console.error('removeFromBlacklist:', e); });
}

function loadTempBans() {
  api('/api/security/temp-bans').then(function (d) {
    if (!d) return;
    var container = document.getElementById('sec-tb-list');
    var bans = d.temp_bans || [];
    if (bans.length === 0) {
      container.innerHTML = '<div class="text-gray-500 text-xs py-2">暂无临时封禁</div>';
      return;
    }
    var html = '<table class="w-full text-xs"><thead><tr class="text-gray-500 border-b border-gray-700">' +
      '<th class="py-1 text-left">IP</th><th class="py-1 text-left">剩余时间</th>' +
      '<th class="py-1 text-left">封禁次数</th>' +
      '<th class="py-1 text-right">操作</th></tr></thead><tbody>';
    bans.forEach(function (b) {
      var mins = Math.ceil(b.remaining_sec / 60);
      html += '<tr class="border-b border-gray-800 hover:bg-gray-800/50">' +
        '<td class="py-2 font-mono text-yellow">' + _esc(b.ip) + '</td>' +
        '<td class="py-2 text-gray-300">' + mins + ' 分钟</td>' +
        '<td class="py-2 text-gray-400">' + b.ban_count + ' 次</td>' +
        '<td class="py-2 text-right"><button onclick="promoteToBlacklist(\'' + _esc(b.ip) + '\')" ' +
        'class="text-red-400 hover:text-red-300 text-xs mr-2">🚫 加入黑名单</button></td></tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  });
}

function promoteToBlacklist(ip) {
  if (!confirm('将 ' + ip + ' 升级为永久黑名单？')) return;
  api('/api/security/promote-to-blacklist', { method: 'POST', body: JSON.stringify({ ip: ip }) })
    .then(function (d) {
      if (d && d.ok) {
        loadTempBans();
        loadBlacklist();
        loadSecurityOverview();
        loadSecurityEvents();
      }
    }).catch(function (e) { console.error('promoteToBlacklist:', e); });
}

function _modeLabel(mode) {
  if (mode === '/24') return '<span class="px-1.5 py-0.5 text-xs rounded bg-blue-900 text-blue-300">/24 子网</span>';
  if (mode === '/16') return '<span class="px-1.5 py-0.5 text-xs rounded bg-purple-900 text-purple-300">/16 子网</span>';
  return '<span class="px-1.5 py-0.5 text-xs rounded bg-gray-700 text-gray-300">精确</span>';
}

function loadWhitelist() {
  api('/api/security/whitelist').then(function (d) {
    if (!d) return;
    var container = document.getElementById('sec-wl-list');
    var wl = d.whitelist || [];
    if (wl.length === 0) {
      container.innerHTML = '<div class="text-gray-500 text-xs py-2">暂无白名单 IP</div>';
      return;
    }
    var html = '';
    wl.forEach(function (entry) {
      var ip = typeof entry === 'string' ? entry : entry.ip;
      var mode = typeof entry === 'string' ? 'exact' : (entry.mode || 'exact');
      html += '<div class="flex items-center justify-between py-2 border-b border-gray-800">' +
        '<div class="flex items-center gap-2">' +
        '<span class="font-mono text-sm text-green-400">' + _esc(ip) + '</span>' +
        _modeLabel(mode) +
        '</div>' +
        '<button onclick="removeFromWhitelist(\'' + _esc(ip) + '\')" ' +
        'class="text-red-400 hover:text-red-300 text-xs">🗑️ 移除</button></div>';
    });
    container.innerHTML = html;
  });
}

function addToWhitelist() {
  var ip = document.getElementById('sec-wl-ip').value.trim();
  var mode = document.getElementById('sec-wl-mode').value;
  if (!ip) return;
  api('/api/security/whitelist', { method: 'POST', body: JSON.stringify({ ip: ip, mode: mode }) })
    .then(function (d) {
      if (d && d.ok) {
        document.getElementById('sec-wl-ip').value = '';
        loadWhitelist();
        loadSecurityOverview();
        loadSecurityEvents();
      } else if (d && d.error) {
        alert(d.error);
      }
    }).catch(function (e) { console.error('addToWhitelist:', e); });
}

function removeFromWhitelist(ip) {
  if (!confirm('确认从白名单移除 ' + ip + '？')) return;
  api('/api/security/whitelist/' + ip, { method: 'DELETE' })
    .then(function (d) {
      if (d && d.ok) {
        loadWhitelist();
        loadSecurityOverview();
      }
    }).catch(function (e) { console.error('removeFromWhitelist:', e); });
}

function saveAutoThreshold() {
  var val = parseInt(document.getElementById('sec-auto-threshold-input').value, 10);
  if (isNaN(val) || val < 1) return;
  var statusEl = document.getElementById('sec-auto-save-status');
  api('/api/security/config', { method: 'PATCH', body: JSON.stringify({ auto_ban_threshold: val }) })
    .then(function (d) {
      if (d && d.ok) {
        statusEl.textContent = '✅ 已保存';
        statusEl.style.color = '#22c55e';
        loadSecurityOverview();
      } else {
        statusEl.textContent = '❌ 保存失败';
        statusEl.style.color = '#ef4444';
      }
      setTimeout(function () { statusEl.textContent = ''; }, 3000);
    }).catch(function (e) {
      statusEl.textContent = '❌ 保存失败';
      statusEl.style.color = '#ef4444';
      setTimeout(function () { statusEl.textContent = ''; }, 3000);
    });
}

function loadSecurityEvents() {
  api('/api/security/events?limit=50').then(function (d) {
    if (!d) return;
    var container = document.getElementById('sec-events-list');
    var events = d.events || [];
    if (events.length === 0) {
      container.innerHTML = '<div class="text-gray-500 text-xs py-2">暂无安全事件</div>';
      return;
    }
    var html = '';
    events.forEach(function (e) {
      var ts = e.ts ? new Date(e.ts).toLocaleString() : '-';
      var icon = _eventIcon(e.type);
      var color = _eventColor(e.type);
      html += '<div class="flex items-start gap-3 py-2 border-b border-gray-800">' +
        '<span class="text-sm mt-0.5">' + icon + '</span>' +
        '<div class="flex-1 min-w-0">' +
        '<div class="flex items-center gap-2">' +
        '<span class="text-xs font-semibold ' + color + '">' + _esc(e.type) + '</span>' +
        '<span class="font-mono text-xs text-gray-400">' + _esc(e.ip) + '</span>' +
        '</div>' +
        '<div class="text-xs text-gray-500 mt-0.5">' + _esc(e.detail) + '</div>' +
        '</div>' +
        '<span class="text-xs text-gray-600 whitespace-nowrap">' + ts + '</span></div>';
    });
    container.innerHTML = html;
  });
}

function _eventIcon(type) {
  var m = { blacklist_add: '🚫', blacklist_remove: '🔓', whitelist_add: '✅', temp_ban: '⏳', auth_fail: '⚠️' };
  return m[type] || '📝';
}

function _eventColor(type) {
  var m = { blacklist_add: 'text-red-400', blacklist_remove: 'text-green-400', whitelist_add: 'text-green-400', temp_ban: 'text-yellow', auth_fail: 'text-orange-400' };
  return m[type] || 'text-gray-400';
}

function _esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
