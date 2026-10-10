
var _wcEditSlug = null;

function buildWeatherCitiesEditor(data) {
  var editor = document.getElementById('cfg-editor');
  var cities = (data && data.cities) || [];
  var enabledCount = cities.filter(function(c) { return c.enabled !== false; }).length;

  var html = '';
  html += '<div class="flex items-center justify-between mb-4">';
  html += '<div>';
  html += '<span class="text-sm font-semibold text-gray-300">🌍 天气城市管理</span>';
  html += '<span class="text-xs text-gray-500 ml-3">共 ' + cities.length + ' 城市 · 启用 ' + enabledCount + '</span>';
  html += '</div>';
  html += '<button onclick="showCityForm(null)" class="px-3 py-1.5 bg-accent text-white text-xs font-semibold rounded-lg hover:bg-blue-600 transition">+ 添加城市</button>';
  html += '</div>';

  html += '<div id="wc-form" style="display:none" class="card mb-4" style="border:1px solid #3b82f6">';
  html += '<h4 class="text-sm font-semibold text-gray-300 mb-3" id="wc-form-title">添加城市</h4>';
  html += '<div class="grid grid-cols-2 md:grid-cols-4 gap-3">';
  html += _wcInput('wc-name', '城市名称', 'text', 'e.g. New York City');
  html += _wcInput('wc-slug', 'Slug', 'text', 'e.g. nyc');
  html += _wcInput('wc-lat', '纬度', 'number', '-90 to 90');
  html += _wcInput('wc-lon', '经度', 'number', '-180 to 180');
  html += _wcInput('wc-icao', 'ICAO代码', 'text', 'e.g. KLGA');
  html += _wcInput('wc-tz', '时区', 'text', 'e.g. America/New_York');
  html += _wcInput('wc-offset', 'UTC偏移', 'number', 'e.g. -4');
  html += '<div><label for="wc-enabled" class="block text-xs text-gray-400 mb-1">启用</label>';
  html += '<select id="wc-enabled" class="w-full px-2 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200">';
  html += '<option value="true">是</option><option value="false">否</option></select></div>';
  html += '</div>';
  html += '<div class="mt-3 border-t border-gray-700 pt-3">';
  html += '<p class="text-xs text-gray-500 mb-2">📊 结算数据源（影响最终结算需求 YES/NO 判断）</p>';
  html += '<div class="grid grid-cols-2 md:grid-cols-3 gap-3">';
  html += '<div><label for="wc-src" class="block text-xs text-gray-400 mb-1">结算数据源</label>';
  html += '<select id="wc-src" class="w-full px-2 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200" onchange="_wcToggleNoaaSite()">';
  html += '<option value="wunderground">Wunderground (默认)</option>';
  html += '<option value="noaa">NOAA (靓风局)</option>';
  html += '<option value="hko">HKO (香港天文台)</option>';
  html += '</select></div>';
  html += _wcInput('wc-url', '结算URL', 'text', 'e.g. https://www.wunderground.com/...');
  html += '<div id="wc-noaa-wrap"><label for="wc-noaa" class="block text-xs text-gray-400 mb-1">NOAA站点代码</label>';
  html += '<input id="wc-noaa" type="text" placeholder="e.g. LTFM" ';
  html += 'class="w-full px-2 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200"></div>';
  html += '</div>';
  html += '</div>';
  html += '<div class="flex gap-2 mt-3">';
  html += '<button onclick="saveCityForm()" class="px-4 py-1.5 bg-accent text-white text-xs font-semibold rounded-lg hover:bg-blue-600 transition">💾 保存</button>';
  html += '<button onclick="hideCityForm()" class="px-3 py-1.5 text-gray-400 text-xs hover:text-white transition">取消</button>';
  html += '<span id="wc-form-err" class="text-xs text-red ml-2" style="line-height:30px"></span>';
  html += '</div>';
  html += '</div>';

  html += '<div style="overflow-x:auto">';
  html += '<table class="w-full text-xs">';
  html += '<thead><tr class="text-gray-400 border-b border-gray-700">';
  html += '<th class="py-2 px-2 text-left">状态</th>';
  html += '<th class="py-2 px-2 text-left">城市</th>';
  html += '<th class="py-2 px-2 text-left">Slug</th>';
  html += '<th class="py-2 px-2 text-right">纬度</th>';
  html += '<th class="py-2 px-2 text-right">经度</th>';
  html += '<th class="py-2 px-2 text-left">ICAO</th>';
  html += '<th class="py-2 px-2 text-left">时区</th>';
  html += '<th class="py-2 px-2 text-right">UTC</th>';
  html += '<th class="py-2 px-2 text-left">结算源</th>';
  html += '<th class="py-2 px-2 text-left">NOAA站点</th>';
  html += '<th class="py-2 px-2 text-center">操作</th>';
  html += '</tr></thead><tbody>';

  cities.forEach(function(c) {
    var enabled = c.enabled !== false;
    var rowClass = enabled ? '' : ' style="opacity:0.5"';
    html += '<tr class="border-b border-gray-800 hover:bg-gray-800/30"' + rowClass + '>';
    html += '<td class="py-1.5 px-2">';
    html += '<button onclick="toggleCity(\'' + escHtml(c.slug) + '\')" class="text-base cursor-pointer" title="' + (enabled ? '点击禁用' : '点击启用') + '">';
    html += enabled ? '🟢' : '🔴';
    html += '</button></td>';
    html += '<td class="py-1.5 px-2 text-gray-200 font-medium">' + escHtml(c.name || '') + '</td>';
    html += '<td class="py-1.5 px-2 text-gray-400 font-mono">' + escHtml(c.slug || '') + '</td>';
    html += '<td class="py-1.5 px-2 text-right text-gray-300">' + (c.lat != null ? c.lat.toFixed(4) : '-') + '</td>';
    html += '<td class="py-1.5 px-2 text-right text-gray-300">' + (c.lon != null ? c.lon.toFixed(4) : '-') + '</td>';
    html += '<td class="py-1.5 px-2 text-gray-400 font-mono">' + escHtml(c.icao || '-') + '</td>';
    html += '<td class="py-1.5 px-2 text-gray-400">' + escHtml(c.timezone || '-') + '</td>';
    html += '<td class="py-1.5 px-2 text-right text-gray-300">' + (c.utc_offset != null ? (c.utc_offset >= 0 ? '+' : '') + c.utc_offset : '-') + '</td>';
    var src = c.settlement_source || 'wunderground';
    var srcColor = src === 'noaa' ? '#3b82f6' : src === 'hko' ? '#10b981' : '#6b7280';
    var srcLabel = src === 'noaa' ? 'NOAA' : src === 'hko' ? 'HKO' : 'WU';
    html += '<td class="py-1.5 px-2"><span style="background:' + srcColor + ';color:#fff;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600">' + srcLabel + '</span></td>';
    html += '<td class="py-1.5 px-2 text-gray-400 font-mono">' + escHtml(c.noaa_site || '-') + '</td>';
    html += '<td class="py-1.5 px-2 text-center">';
    html += '<button onclick="editCity(\'' + escHtml(c.slug) + '\')" class="text-accent hover:text-blue-400 mr-2" title="编辑">✏️</button>';
    html += '<button onclick="deleteCity(\'' + escHtml(c.slug) + '\', \'' + escHtml(c.name || '') + '\')" class="text-red hover:text-red-400" title="删除">🗑️</button>';
    html += '</td>';
    html += '</tr>';
  });

  html += '</tbody></table></div>';

  document.getElementById('cfg-save-bar').style.display = 'none';

  editor.innerHTML = html;
}

function _wcInput(id, label, type, ph) {
  return '<div><label for="' + id + '" class="block text-xs text-gray-400 mb-1">' + label + '</label>' +
    '<input id="' + id + '" type="' + type + '" placeholder="' + ph + '" ' +
    'class="w-full px-2 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200" ' +
    (type === 'number' ? 'step="any"' : '') + '></div>';
}

function showCityForm(slug) {
  _wcEditSlug = slug;
  var form = document.getElementById('wc-form');
  var title = document.getElementById('wc-form-title');
  document.getElementById('wc-form-err').textContent = '';
  form.style.display = 'block';

  if (slug) {
    title.textContent = '编辑城市: ' + slug;
    var cities = (cfgData && cfgData.cities) || [];
    var c = cities.find(function(x) { return x.slug === slug; });
    if (c) {
      document.getElementById('wc-name').value = c.name || '';
      document.getElementById('wc-slug').value = c.slug || '';
      document.getElementById('wc-lat').value = c.lat != null ? c.lat : '';
      document.getElementById('wc-lon').value = c.lon != null ? c.lon : '';
      document.getElementById('wc-icao').value = c.icao || '';
      document.getElementById('wc-tz').value = c.timezone || '';
      document.getElementById('wc-offset').value = c.utc_offset != null ? c.utc_offset : '';
      document.getElementById('wc-enabled').value = c.enabled !== false ? 'true' : 'false';
      document.getElementById('wc-src').value = c.settlement_source || 'wunderground';
      document.getElementById('wc-url').value = c.settlement_url || '';
      document.getElementById('wc-noaa').value = c.noaa_site || '';
      _wcToggleNoaaSite();
    }
  } else {
    title.textContent = '添加新城市';
    ['wc-name','wc-slug','wc-lat','wc-lon','wc-icao','wc-tz','wc-offset','wc-url','wc-noaa'].forEach(function(id) {
      document.getElementById(id).value = '';
    });
    document.getElementById('wc-enabled').value = 'true';
    document.getElementById('wc-src').value = 'wunderground';
    _wcToggleNoaaSite();
  }
}

function hideCityForm() {
  document.getElementById('wc-form').style.display = 'none';
  _wcEditSlug = null;
}

function editCity(slug) {
  showCityForm(slug);
}

function _wcToggleNoaaSite() {
  var src = document.getElementById('wc-src');
  var wrap = document.getElementById('wc-noaa-wrap');
  if (src && wrap) {
    wrap.style.display = src.value === 'noaa' ? '' : 'none';
  }
}

function _getCityFormData() {
  return {
    name: document.getElementById('wc-name').value.trim(),
    slug: document.getElementById('wc-slug').value.trim().toLowerCase(),
    lat: parseFloat(document.getElementById('wc-lat').value),
    lon: parseFloat(document.getElementById('wc-lon').value),
    icao: document.getElementById('wc-icao').value.trim().toUpperCase(),
    timezone: document.getElementById('wc-tz').value.trim(),
    utc_offset: parseFloat(document.getElementById('wc-offset').value) || 0,
    enabled: document.getElementById('wc-enabled').value === 'true',
    settlement_source: document.getElementById('wc-src').value || 'wunderground',
    settlement_url: document.getElementById('wc-url').value.trim(),
    noaa_site: document.getElementById('wc-noaa').value.trim().toUpperCase()
  };
}

function saveCityForm() {
  var data = _getCityFormData();
  var errEl = document.getElementById('wc-form-err');

  if (!data.name) { errEl.textContent = '城市名称不能为空'; return; }
  if (!data.slug) { errEl.textContent = 'Slug不能为空'; return; }
  if (isNaN(data.lat) || data.lat < -90 || data.lat > 90) { errEl.textContent = '纬度无效（-90到90）'; return; }
  if (isNaN(data.lon) || data.lon < -180 || data.lon > 180) { errEl.textContent = '经度无效（-180到180）'; return; }

  errEl.textContent = '保存中...';
  errEl.className = 'text-xs text-gray-400 ml-2';

  var url, method;
  if (_wcEditSlug) {
    url = '/api/weather-cities/' + encodeURIComponent(_wcEditSlug);
    method = 'PUT';
  } else {
    url = '/api/weather-cities';
    method = 'POST';
  }

  api(url, { method: method, body: JSON.stringify(data), headers: {'Content-Type': 'application/json'} })
  .then(function(resp) {
    if (resp.error) {
      errEl.textContent = resp.error + (resp.details ? ': ' + resp.details.join(', ') : '');
      errEl.className = 'text-xs text-red ml-2';
    } else {
      hideCityForm();
      loadConfig();
    }
  })
  .catch(function(e) {
    errEl.textContent = '请求失败: ' + String(e);
    errEl.className = 'text-xs text-red ml-2';
  });
}

function deleteCity(slug, name) {
  if (!confirm('确定删除城市 "' + name + '" (' + slug + ') ？此操作不可撤销。')) return;
  api('/api/weather-cities/' + encodeURIComponent(slug), { method: 'DELETE' })
  .then(function(resp) {
    if (resp.error) { alert('删除失败: ' + resp.error); }
    else { loadConfig(); }
  })
  .catch(function(e) { alert('删除失败: ' + e); });
}

function toggleCity(slug) {
  api('/api/weather-cities/' + encodeURIComponent(slug) + '/toggle', { method: 'PATCH' })
  .then(function(resp) {
    if (resp.error) { alert('切换失败: ' + resp.error); }
    else { loadConfig(); }
  })
  .catch(function(e) { alert('切换失败: ' + e); });
}
