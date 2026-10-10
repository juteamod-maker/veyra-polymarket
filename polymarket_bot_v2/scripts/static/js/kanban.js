
var KMAP = null, _kMapInitialized = false;
var kAllMarkers = [], kMarkerLayerMap = {}, kSelectedCategory = null;
var kModalStep = 1, kLastClickLatLng = null, kWeatherCities = [];

function initOrRefreshKanbanMap() {
  if (_kMapInitialized) {
    setTimeout(function() {
      if (KMAP) {
        KMAP.invalidateSize();
        kLoadAllMarkers();
      }
    }, 50);
    return;
  }
  setTimeout(kInitMap, 100);
}

function kInitMap() {
  var container = document.getElementById('kanban-map');
  if (!container) return;

  if (!container.offsetParent && container.offsetHeight === 0) {
    setTimeout(kInitMap, 100);
    return;
  }

  KMAP = L.map('kanban-map', {
    center: [48, -10],
    zoom: 4,
    minZoom: 1,
    maxZoom: 6,
    zoomControl: true,
    worldCopyJump: true,
  });

  var gaodeLayer = L.tileLayer(
    'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    {
      subdomains: ['1','2','3','4'],
      attribution: '&copy; <a href="https://amap.com">高德地图</a>',
      maxZoom: 18,
    }
  );

  var osmLayer = L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {
      subdomains: ['a','b','c'],
      attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    }
  );

  gaodeLayer.on('tileerror', function() {
    if (KMAP.hasLayer(gaodeLayer)) {
      KMAP.removeLayer(gaodeLayer);
      osmLayer.addTo(KMAP);
    }
  });
  gaodeLayer.addTo(KMAP);

  KMAP.on('click', function(e) {
    kLastClickLatLng = e.latlng;
    if (kModalStep === 2 && kSelectedCategory !== 'weather') {
      var latEl = document.getElementById('k-manual-lat');
      var lonEl = document.getElementById('k-manual-lon');
      if (latEl) latEl.value = e.latlng.lat.toFixed(4);
      if (lonEl) lonEl.value = e.latlng.lng.toFixed(4);
      kValidateStep2();
    }
  });

  _kMapInitialized = true;

  var loadingEl = document.getElementById('kanban-loading');
  if (loadingEl) loadingEl.style.display = 'none';
  var legendEl = document.getElementById('kanban-legend');
  if (legendEl) legendEl.style.display = 'block';

  kLoadAllMarkers();
  kLoadWeatherCities();

  setTimeout(function() { if (KMAP) KMAP.invalidateSize(); }, 200);
}

function kLoadAllMarkers() {
  fetch('/api/map/markers')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      kAllMarkers = data.markers || [];
      kRenderSidebar();
      kRenderMapMarkers();
      kUpdateHeaderStats();
    })
    .catch(function(e) {
      kShowToast('❌ 加载标记失败: ' + e.message, 'error');
    });
}

function kLoadWeatherCities() {
  fetch('/api/map/weather-cities')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      kWeatherCities = data.cities || [];
      kPopulateCityDropdown();
    });
}

function kGetCategoryEmoji(cat) {
  var map = { weather:'⛅', sports:'⚽', crypto:'₿', finance:'💰', geopolitics:'🌍', tech:'💻', pop_culture:'🎭', custom:'📌' };
  return map[cat] || '📍';
}

function kGetPinClass(cat) {
  var map = { weather:'pin-weather', sports:'pin-sports', crypto:'pin-crypto', custom:'pin-custom' };
  return map[cat] || 'pin-default';
}

function kCreatePin(cat) {
  var emoji = kGetCategoryEmoji(cat);
  var pinCls = kGetPinClass(cat);
  return L.divIcon({
    className: '',
    html: '<div class="map-pin ' + pinCls + '"><div class="circle">' + emoji + '</div><div class="stem"></div></div>',
    iconSize: [32, 40],
    iconAnchor: [16, 40],
    popupAnchor: [0, -40],
  });
}

function kRenderMapMarkers() {
  if (!KMAP) return;
  Object.values(kMarkerLayerMap).forEach(function(lm) { KMAP.removeLayer(lm); });
  kMarkerLayerMap = {};

  kAllMarkers.forEach(function(m) {
    var lm = L.marker([m.lat, m.lon], { icon: kCreatePin(m.category || 'custom') });
    lm.bindPopup(kBuildPopupHtml(m), {
      autoClose: false,
      closeOnClick: false,
      maxWidth: 300,
      minWidth: 240,
    });
    lm.addTo(KMAP);
    kMarkerLayerMap[m.id] = lm;

    if (m.category === 'weather') {
      kFetchAndUpdatePopupTemp(m.id, m.city);
    }
  });
}

function kBuildPopupHtml(m) {
  var eventsHtml = '';
  var events = m.events || [];
  if (events.length === 0) {
    eventsHtml = '<div class="popup-empty">暂无事件记录</div>';
  } else {
    events.slice(0, 8).forEach(function(ev) {
      var isBet = ev.is_bet;
      var clickAttr = isBet
        ? ('onclick="kOpenBetDetail(\'' + kEscHtml(ev.market_slug || ev.bet_id || '') + '\')" ')
        : '';
      var outcomeTag = isBet && ev.outcome
        ? '<span style="color:' + (ev.outcome === 'YES' ? '#4ade80' : '#f87171') + ';font-weight:700;">' + ev.outcome + '</span> '
        : '';
      var priceTag = isBet && ev.market_price != null
        ? '<span style="color:#64748b;"> @ ' + (ev.market_price * 100).toFixed(0) + '¢</span>'
        : '';
      eventsHtml += '<div class="popup-event' + (isBet ? ' is-bet' : '') + '" ' + clickAttr + '>'
        + '<div class="ev-title">' + kEscHtml(ev.title || m.city) + '</div>'
        + (ev.description ? '<div class="ev-desc">' + kEscHtml(ev.description).substring(0, 100) + (ev.description.length > 100 ? '…' : '') + '</div>' : '')
        + (isBet ? '<div class="ev-meta">' + outcomeTag + priceTag + '</div>' : '')
        + '</div>';
    });
    if (events.length > 8) {
      eventsHtml += '<div class="popup-empty">还有 ' + (events.length - 8) + ' 个事件...</div>';
    }
  }

  return '<div class="popup-inner">'
    + '<div class="popup-city">' + kEscHtml(m.city) + '</div>'
    + '<div class="popup-meta" id="kpopup-temp-' + m.id + '">'
    + (m.category === 'weather'
        ? '<span class="spinner" style="width:12px;height:12px;border-width:2px;"></span>'
        : '<span class="popup-time">📍 ' + kEscHtml(m.category || '自定义') + '</span>')
    + '</div>'
    + '<hr class="popup-divider"/>'
    + '<div class="popup-events">' + eventsHtml + '</div>'
    + (!m.is_auto ? '<div class="popup-add-btn" onclick="kAddEventToMarker(\'' + m.id + '\')">＋ 添加事件</div>' : '')
    + '</div>';
}

function kFetchAndUpdatePopupTemp(markerId, cityName) {
  fetch('/api/map/cityinfo/' + encodeURIComponent(cityName))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var el = document.getElementById('kpopup-temp-' + markerId);
      if (el) {
        if (d.status === 'ok' && d.today_max_c != null) {
          var timeStr = d.local_time ? d.local_time.replace('T', ' ').substring(5, 16) : '';
          var wuUrl = d.settlement_url || '';
          var tempLabel = '🌡 今日高 ' + d.today_max_c + '°C';
          var tempHtml = wuUrl
            ? '<a href="' + wuUrl + '" target="_blank" rel="noopener" class="popup-temp" style="text-decoration:underline;cursor:pointer;">' + tempLabel + '</a>'
            : '<span class="popup-temp">' + tempLabel + '</span>';
          var curHtml = d.current_temp_c != null
            ? '<span class="popup-temp" style="color:#60a5fa;">当前 ' + d.current_temp_c + '°C</span>'
            : '';
          el.innerHTML = tempHtml
            + curHtml
            + (timeStr ? '<span class="popup-time">🕐 ' + timeStr + '</span>' : '')
            + (d.timezone ? '<span class="popup-time">' + d.timezone + '</span>' : '');
        } else {
          el.innerHTML = '<span class="popup-time">🌡 温度暂不可用</span>';
        }
      }
    })
    .catch(function() {
      var el = document.getElementById('kpopup-temp-' + markerId);
      if (el) el.innerHTML = '<span class="popup-time">🌡 温度暂不可用</span>';
    });
}

function kOpenBetDetail(marketSlug) {
  if (!marketSlug) return;
  if (typeof switchTab === 'function') {
    switchTab('bets');
  }
}

function kUpdateHeaderStats() {
  var autoC = kAllMarkers.filter(function(m) { return m.is_auto; }).length;
  var manualC = kAllMarkers.filter(function(m) { return !m.is_auto; }).length;
  var totalEv = kAllMarkers.reduce(function(s, m) { return s + (m.events || []).length; }, 0);
  var q = function(id, val) {
    var el = document.getElementById(id);
    if (el) { var strong = el.querySelector('strong'); if (strong) strong.textContent = val; }
  };
  q('k-header-auto-count', autoC);
  q('k-header-manual-count', manualC);
  q('k-header-total-events', totalEv);
}

function kRenderSidebar(filter) {
  var content = document.getElementById('k-sidebar-content');
  var empty = document.getElementById('k-sidebar-empty');
  if (!content) return;
  filter = (filter || '').toLowerCase();

  var visible = kAllMarkers.filter(function(m) {
    return !filter || m.city.toLowerCase().indexOf(filter) !== -1;
  });

  if (visible.length === 0) {
    content.innerHTML = '';
    if (empty) empty.classList.remove('hidden');
    return;
  }
  if (empty) empty.classList.add('hidden');

  var groups = {};
  visible.forEach(function(m) {
    var cat = m.category || 'custom';
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(m);
  });

  var catNames = { weather:'⛅ 天气城市', sports:'⚽ 体育赛事', crypto:'₿ Crypto', finance:'💰 金融', geopolitics:'🌍 地缘政治', tech:'💻 科技', pop_culture:'🎭 流行文化', custom:'📌 自定义' };
  var catOrder = ['weather', 'sports', 'crypto', 'finance', 'geopolitics', 'tech', 'pop_culture', 'custom'];

  var html = '';
  catOrder.forEach(function(cat) {
    if (!groups[cat]) return;
    html += '<div class="sidebar-section"><div class="sidebar-title">' + (catNames[cat] || cat) + '</div>';
    groups[cat].forEach(function(m) {
      var evCount = (m.events || []).length;
      var badgeClass = m.is_auto ? 'badge-blue' : 'badge-gray';
      html += '<div class="sidebar-item" onclick="kFocusMarker(\'' + m.id + '\')">'
        + '<div class="si-icon">' + kGetCategoryEmoji(m.category) + '</div>'
        + '<div class="flex-1 min-w-0"><div class="si-name">' + kEscHtml(m.city) + '</div>'
        + '<div class="si-count">' + (m.is_auto ? '自动检测' : '手动添加') + '</div></div>'
        + '<div class="sidebar-badge ' + badgeClass + '">' + evCount + '</div>'
        + (m.is_auto ? '' : '<span onclick="event.stopPropagation();kDeleteMarker(\'' + m.id + '\')" title="删除" style="color:#ef444480;font-size:14px;cursor:pointer;padding:2px 4px;" onmouseover="this.style.color=\'#ef4444\'" onmouseout="this.style.color=\'#ef444480\'">✕</span>')
        + '</div>';
    });
    html += '</div>';
  });
  content.innerHTML = html;
}

function kFilterSidebar(val) {
  kRenderSidebar(val);
}

function kFocusMarker(markerId) {
  if (!KMAP) return;
  var m = kAllMarkers.find(function(x) { return x.id === markerId; });
  if (!m) return;
  KMAP.flyTo([m.lat, m.lon], 5, { duration: 0.8 });
  var lm = kMarkerLayerMap[markerId];
  if (lm) {
    setTimeout(function() { lm.openPopup(); }, 900);
    if (m.category === 'weather') {
      kFetchAndUpdatePopupTemp(m.id, m.city);
    }
  }
}

function kDeleteMarker(markerId) {
  if (!confirm('确认删除该手动标记？')) return;
  fetch('/api/map/markers/' + markerId, { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.status === 'ok') {
        kShowToast('🗑 标记已删除');
        kLoadAllMarkers();
      } else {
        kShowToast('❌ ' + (d.error || '删除失败'), 'error');
      }
    });
}

function kShowAddModal() {
  kSelectedCategory = null;
  kModalStep = 1;
  kRenderStepUI();
  var el = document.getElementById('k-add-modal');
  if (el) el.classList.add('show');
}

function kHideAddModal() {
  var el = document.getElementById('k-add-modal');
  if (el) el.classList.remove('show');
  kSelectedCategory = null;
  kModalStep = 1;
}

function kSelectCategory(cat) {
  kSelectedCategory = cat;
  document.querySelectorAll('#k-add-modal .cat-card').forEach(function(el) {
    el.classList.toggle('selected', el.dataset.cat === cat);
  });
  var btn = document.getElementById('k-modal-next-btn');
  if (btn) btn.disabled = false;
}

function kRenderStepUI() {
  ['1','2','3'].forEach(function(s) {
    var dot = document.getElementById('k-step-dot-' + s);
    if (!dot) return;
    var n = parseInt(s);
    dot.classList.toggle('active', n === kModalStep);
    dot.classList.toggle('done', n < kModalStep);
    if (s < '3') {
      var line = document.getElementById('k-step-line-' + s);
      if (line) line.classList.toggle('done', n < kModalStep);
    }
  });
  var s1 = document.getElementById('k-step-1');
  var s2 = document.getElementById('k-step-2');
  var s3 = document.getElementById('k-step-3');
  if (s1) s1.classList.toggle('hidden', kModalStep !== 1);
  if (s2) s2.classList.toggle('hidden', kModalStep !== 2);
  if (s3) s3.classList.toggle('hidden', kModalStep !== 3);
  var backBtn = document.getElementById('k-modal-back-btn');
  if (backBtn) backBtn.style.display = kModalStep > 1 ? '' : 'none';

  var nextBtn = document.getElementById('k-modal-next-btn');
  if (nextBtn) {
    nextBtn.textContent = kModalStep === 3 ? '💾 保存' : '下一步 →';
    if (kModalStep === 1) {
      nextBtn.disabled = !kSelectedCategory;
    } else if (kModalStep === 2) {
      nextBtn.disabled = true;
      kValidateStep2();
    } else if (kModalStep === 3) {
      kUpdateConfirmCard();
      var titleEl = document.getElementById('k-event-title');
      nextBtn.disabled = !(titleEl && titleEl.value.trim());
    }
  }
}

function kShowLocInputForCategory(cat) {
  var wEl = document.getElementById('k-loc-weather');
  var mEl = document.getElementById('k-loc-manual');
  if (wEl) wEl.classList.toggle('hidden', cat !== 'weather');
  if (mEl) mEl.classList.toggle('hidden', cat === 'weather');
}

function kModalNext() {
  if (kModalStep === 1) {
    if (!kSelectedCategory) return;
    kModalStep = 2;
    kRenderStepUI();
    kShowLocInputForCategory(kSelectedCategory);
  } else if (kModalStep === 2) {
    kModalStep = 3;
    kRenderStepUI();
    if (kSelectedCategory === 'weather') {
      var sel = document.getElementById('k-weather-city-select');
      var cityName = sel && sel.options[sel.selectedIndex] && sel.options[sel.selectedIndex].dataset.name;
      var titleEl = document.getElementById('k-event-title');
      if (cityName && titleEl && !titleEl.value) {
        titleEl.value = cityName;
      }
    }
  } else if (kModalStep === 3) {
    kSaveNewMarker();
  }
}

function kModalBack() {
  if (kModalStep > 1) { kModalStep--; kRenderStepUI(); }
}

function kValidateStep2() {
  var btn = document.getElementById('k-modal-next-btn');
  if (!btn) return;
  if (kSelectedCategory === 'weather') {
    var sel = document.getElementById('k-weather-city-select');
    btn.disabled = !(sel && sel.value);
    if (sel && sel.value) {
      var opt = sel.options[sel.selectedIndex];
      var preview = document.getElementById('k-weather-city-preview');
      if (preview) {
        preview.classList.remove('hidden');
        preview.textContent = '📍 ' + opt.dataset.name + '  (' + opt.dataset.lat + ', ' + opt.dataset.lon + ')';
      }
    }
  } else {
    var city = document.getElementById('k-manual-city');
    var lat = document.getElementById('k-manual-lat');
    var lon = document.getElementById('k-manual-lon');
    btn.disabled = !(city && city.value.trim() && lat && lat.value && lon && lon.value);
  }
}

function kOnWeatherCitySelect() {
  kValidateStep2();
}

function kUpdateConfirmCard() {
  var text = '';
  if (kSelectedCategory === 'weather') {
    var sel = document.getElementById('k-weather-city-select');
    var opt = sel && sel.options[sel.selectedIndex];
    text = '城市: ' + (opt && opt.dataset.name || '—') + '\n坐标: (' + (opt && opt.dataset.lat) + ', ' + (opt && opt.dataset.lon) + ')';
  } else {
    var c = document.getElementById('k-manual-city');
    var la = document.getElementById('k-manual-lat');
    var lo = document.getElementById('k-manual-lon');
    text = '城市: ' + (c && c.value) + '\n坐标: (' + (la && la.value) + ', ' + (lo && lo.value) + ')';
  }
  var card = document.getElementById('k-confirm-text');
  if (card) card.textContent = text;
}

function kSaveNewMarker() {
  var city, lat, lon, cat;
  cat = kSelectedCategory;

  if (cat === 'weather') {
    var sel = document.getElementById('k-weather-city-select');
    var opt = sel.options[sel.selectedIndex];
    city = opt.dataset.name;
    lat = parseFloat(opt.dataset.lat);
    lon = parseFloat(opt.dataset.lon);
  } else {
    var cityEl = document.getElementById('k-manual-city');
    var latEl = document.getElementById('k-manual-lat');
    var lonEl = document.getElementById('k-manual-lon');
    city = cityEl.value.trim();
    lat = parseFloat(latEl.value);
    lon = parseFloat(lonEl.value);
  }

  var titleEl = document.getElementById('k-event-title');
  var descEl = document.getElementById('k-event-desc');
  var title = titleEl ? titleEl.value.trim() : '';
  var desc = descEl ? descEl.value.trim() : '';

  if (!city || isNaN(lat) || isNaN(lon) || !title) {
    kShowToast('⚠ 请填写必填项目', 'warn');
    return;
  }

  var btn = document.getElementById('k-modal-next-btn');
  if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }

  fetch('/api/map/markers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      city: city, lat: lat, lon: lon, category: cat,
      events: [{ title: title, description: desc, is_bet: false }],
    }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.status === 'ok') {
      kShowToast('✅ 标记已保存');
      kHideAddModal();
      kLoadAllMarkers();
    } else {
      kShowToast('❌ ' + (d.error || '保存失败'), 'error');
      if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
    }
  })
  .catch(function(e) {
    kShowToast('❌ 网络错误: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
  });
}

function kAddEventToMarker(markerId) {
  var title = prompt('事件标题:');
  if (!title) return;
  var desc = prompt('事件描述 (可选):') || '';
  var m = kAllMarkers.find(function(x) { return x.id === markerId; });
  if (!m) return;

  fetch('/api/map/markers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      city: m.city, lat: m.lat, lon: m.lon, category: m.category,
      events: [{ title: title, description: desc, is_bet: false }],
    }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.status === 'ok') {
      kShowToast('✅ 事件已添加');
      kLoadAllMarkers();
    } else {
      kShowToast('❌ ' + (d.error || '失败'));
    }
  });
}

function kPopulateCityDropdown() {
  var sel = document.getElementById('k-weather-city-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">── 请选择城市 ──</option>';
  kWeatherCities
    .filter(function(c) { return c.enabled !== false; })
    .sort(function(a, b) { return a.name.localeCompare(b.name); })
    .forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c.slug || c.name;
      opt.textContent = c.name;
      opt.dataset.name = c.name;
      opt.dataset.lat = c.lat;
      opt.dataset.lon = c.lon;
      sel.appendChild(opt);
    });
}

var _kToastTimer = null;
function kShowToast(msg, type) {
  var el = document.getElementById('k-toast');
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = type === 'error' ? '#7f1d1d' : type === 'warn' ? '#78350f' : '#2a2d3a';
  el.classList.add('show');
  if (_kToastTimer) clearTimeout(_kToastTimer);
  _kToastTimer = setTimeout(function() { el.classList.remove('show'); }, 3000);
}

function kEscHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
