
var _dpChartInstances = {};

var _bloombergCrosshairPlugin = {
  id: 'bloombergCrosshair',
  afterDraw: function(chart) {
    var meta = chart._bloombergMeta;
    if (!meta || !meta.hoverActive) return;
    var ctx = chart.ctx;
    var area = chart.chartArea;
    var mx = meta.mouseX;
    var my = meta.mouseY;
    if (mx < area.left || mx > area.right || my < area.top || my > area.bottom) return;
    ctx.save();
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 0.8;
    ctx.strokeStyle = 'rgba(148,163,184,0.5)';
    ctx.beginPath();
    ctx.moveTo(mx, area.top);
    ctx.lineTo(mx, area.bottom);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(area.left, my);
    ctx.lineTo(area.right, my);
    ctx.stroke();
    ctx.restore();
    var yScale = chart.scales.y;
    if (yScale) {
      var yVal = yScale.getValueForPixel(my);
      if (yVal != null) {
        var labelText = yVal.toFixed(1) + '¢';
        ctx.save();
        ctx.fillStyle = '#1e293b';
        ctx.strokeStyle = '#334155';
        ctx.lineWidth = 1;
        var tw = ctx.measureText(labelText).width + 8;
        ctx.fillRect(area.right + 1, my - 9, tw, 18);
        ctx.strokeRect(area.right + 1, my - 9, tw, 18);
        ctx.fillStyle = '#e2e8f0';
        ctx.font = '10px monospace';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(labelText, area.right + 5, my);
        ctx.restore();
      }
    }
  }
};

function _updateOverlay(betId, entryPrice, currentPrice, isUp) {
  var curEl = document.getElementById('dp-pchart-cur-' + betId);
  var chgEl = document.getElementById('dp-pchart-chg-' + betId);
  if (curEl) {
    curEl.textContent = currentPrice.toFixed(1) + '¢';
    curEl.style.color = isUp ? '#22c55e' : '#ef4444';
  }
  if (chgEl && entryPrice) {
    var pct = ((currentPrice - entryPrice) / entryPrice * 100).toFixed(1);
    var arrow = isUp ? '▲' : '▼';
    var sign = isUp ? '+' : '';
    chgEl.innerHTML = '<span style="font-weight:700;color:' + (isUp ? '#22c55e' : '#ef4444') + '">' + arrow + ' ' + sign + pct + '%</span>';
  }
}

function loadDetailPriceChart(betId, hours) {
  hours = parseInt(hours) || 6;
  document.querySelectorAll('.dp-pchart-range[data-bet="' + betId + '"]').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.hours) === hours);
  });

  var canvas = document.getElementById('dp-pchart-' + betId);
  var loading = document.getElementById('dp-pchart-loading-' + betId);
  if (!canvas) return;

  var seriesUrl = '/api/price_monitor/series/' + betId + '?hours=' + hours;

  function _tryRealtimeFallback(seriesData) {
    api('/api/price_monitor/realtime/' + betId).then(function(rt) {
      if (loading) loading.style.display = 'none';
      if (rt.data && rt.data.length > 0) {
        _renderDetailPriceChart(betId, canvas, {
          series: rt.data.map(function(p) { return { ts: p.ts, mid: p.mid || p.price, bid: p.bid, ask: p.ask }; }),
          entry_price: seriesData ? seriesData.entry_price : null,
          outcome: (seriesData && seriesData.outcome) || rt.outcome || null,
          events: (seriesData && seriesData.events) || [],
          market_end_date: seriesData ? seriesData.market_end_date : null
        });
      } else {
        _showWaitingState(betId, loading, seriesData);
      }
    }).catch(function() {
      _showWaitingState(betId, loading, seriesData);
    });
  }

  api(seriesUrl).then(function(d) {
    if (d.series && d.series.length > 0) {
      if (loading) loading.style.display = 'none';
      _renderDetailPriceChart(betId, canvas, d);
    } else {
      _tryRealtimeFallback(d);
    }
  }).catch(function() {
    _tryRealtimeFallback(null);
  });
}

function _showWaitingState(betId, loading, seriesData) {
  if (!loading) return;
  loading.style.display = 'flex';
  if (seriesData && seriesData.entry_price) {
    var ep = (seriesData.entry_price * 100).toFixed(1);
    loading.innerHTML = '<span style="color:#475569;font-size:0.72rem">⏳ 等待价格采集中… 入场 ' + ep + '¢</span>';
    _updateOverlay(betId, parseFloat(ep), parseFloat(ep), true);
  } else {
    loading.innerHTML = '<span style="color:#475569;font-size:0.72rem">⏳ 等待价格采集中…</span>';
  }
}

function _renderDetailPriceChart(betId, canvas, data) {
  var series = data.series || [];
  var events = data.events || [];
  var entryRaw = data.entry_price;
  var endDate = data.market_end_date || null;
  var outcome = (data.outcome || 'YES').toUpperCase();
  var isNO = (outcome === 'NO');

  if (series.length === 0) {
    _showWaitingState(betId, document.getElementById('dp-pchart-loading-' + betId), data);
    return;
  }

  var entryPrice = entryRaw ? entryRaw * 100 : null;
  var mids = series.map(function(p) {
    var v;
    if (isNO) {
      v = (p.ask != null && p.ask > 0) ? (1.0 - p.ask) : (1.0 - p.mid);
    } else {
      v = (p.bid != null && p.bid > 0) ? p.bid : p.mid;
    }
    return v * 100;
  });

  var labels = series.map(function(p) {
    return p.ts ? p.ts.replace('T', ' ').slice(11, 19) : '';
  });

  var lastPrice = mids[mids.length - 1];
  var isUp = entryPrice ? (lastPrice >= entryPrice) : true;
  var lineColor = isUp ? '#22c55e' : '#ef4444';
  var gradientId = '_bbg_' + betId;

  _updateOverlay(betId, entryPrice, lastPrice, isUp);

  var ctx2d = canvas.getContext('2d');
  var gradFill = ctx2d.createLinearGradient(0, 0, 0, canvas.parentElement.clientHeight || 180);
  if (isUp) {
    gradFill.addColorStop(0, 'rgba(34,197,94,0.25)');
    gradFill.addColorStop(0.5, 'rgba(34,197,94,0.08)');
    gradFill.addColorStop(1, 'rgba(34,197,94,0)');
  } else {
    gradFill.addColorStop(0, 'rgba(239,68,68,0.25)');
    gradFill.addColorStop(0.5, 'rgba(239,68,68,0.08)');
    gradFill.addColorStop(1, 'rgba(239,68,68,0)');
  }

  var datasets = [{
    label: outcome + ' 价格',
    data: mids,
    borderColor: lineColor,
    backgroundColor: gradFill,
    fill: true,
    tension: 0.3,
    pointRadius: 0,
    pointHoverRadius: 5,
    pointHoverBackgroundColor: lineColor,
    pointHoverBorderColor: '#fff',
    pointHoverBorderWidth: 2,
    borderWidth: 2
  }];

  if (entryPrice && entryPrice > 0) {
    datasets.push({
      label: '入场 ' + entryPrice.toFixed(1) + '¢',
      data: new Array(labels.length).fill(entryPrice),
      borderColor: 'rgba(234,179,8,0.6)',
      borderDash: [6, 3],
      borderWidth: 1,
      pointRadius: 0,
      pointHoverRadius: 0,
      fill: false
    });
  }

  if (endDate && series.length > 1) {
    var endTs = endDate.replace('Z', '+00:00');
    var settleIdx = -1;
    for (var si = 0; si < series.length; si++) {
      if (series[si].ts >= endTs) { settleIdx = si; break; }
    }
    if (settleIdx >= 0) {
      var settleData = new Array(labels.length).fill(null);
      settleData[settleIdx] = mids[settleIdx];
      datasets.push({
        label: '⏱ 结算',
        data: settleData,
        borderColor: 'rgba(168,85,247,0.4)',
        borderDash: [3, 3],
        borderWidth: 1,
        pointRadius: 5,
        pointBackgroundColor: '#a855f7',
        pointBorderColor: '#a855f7',
        pointStyle: 'rectRot',
        fill: false,
        spanGaps: false
      });
    }
  }

  var eventPoints = [];
  events.forEach(function(e) {
    var eTs = e.ts || '';
    for (var i = 0; i < series.length; i++) {
      if (series[i].ts >= eTs) {
        var evPrice = isNO ? (1 - e.price_at_cross) * 100 : e.price_at_cross * 100;
        eventPoints.push({ x: i, y: evPrice, direction: e.direction, threshold: e.threshold_pct });
        break;
      }
    }
  });
  var downPts = eventPoints.filter(function(p) { return p.direction === 'down'; });
  var upPts   = eventPoints.filter(function(p) { return p.direction === 'up'; });
  if (downPts.length) {
    datasets.push({
      label: '↓ 下跌',
      data: downPts.map(function(p) { return { x: p.x, y: p.y }; }),
      type: 'scatter', pointBackgroundColor: '#ef4444', pointBorderColor: '#fff',
      pointBorderWidth: 1, pointRadius: 4, pointStyle: 'triangle', showLine: false
    });
  }
  if (upPts.length) {
    datasets.push({
      label: '↑ 上涨',
      data: upPts.map(function(p) { return { x: p.x, y: p.y }; }),
      type: 'scatter', pointBackgroundColor: '#22c55e', pointBorderColor: '#fff',
      pointBorderWidth: 1, pointRadius: 4, pointStyle: 'triangle', showLine: false
    });
  }

  if (_dpChartInstances[betId]) { _dpChartInstances[betId].destroy(); }

  var hudId = 'dp-pchart-hud-' + betId;
  var existingHud = document.getElementById(hudId);
  if (!existingHud) {
    var wrap = canvas.parentElement;
    var hud = document.createElement('div');
    hud.id = hudId;
    hud.style.cssText = 'position:absolute;top:6px;left:8px;font-family:monospace;font-size:0.68rem;' +
      'color:#94a3b8;pointer-events:none;z-index:10;display:none;' +
      'background:rgba(15,17,23,0.85);padding:3px 8px;border-radius:4px;border:1px solid #1e293b;';
    wrap.style.position = 'relative';
    wrap.appendChild(hud);
  }

  var _entryPriceRef = entryPrice;
  var _downPtsRef = downPts;
  var _upPtsRef = upPts;

  var chartInstance = new Chart(canvas, {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    plugins: [_bloombergCrosshairPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      layout: {
        padding: { right: 45 }
      },
      interaction: {
        mode: 'index',
        intersect: false
      },
      hover: {
        mode: 'index',
        intersect: false
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: true,
          mode: 'index',
          intersect: false,
          position: 'nearest',
          backgroundColor: 'rgba(15,17,23,0.95)',
          borderColor: '#334155',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#cbd5e1',
          titleFont: { family: 'monospace', size: 11, weight: 'bold' },
          bodyFont: { family: 'monospace', size: 11 },
          padding: { top: 8, bottom: 8, left: 10, right: 10 },
          cornerRadius: 4,
          displayColors: false,
          caretSize: 0,
          callbacks: {
            title: function(items) {
              if (!items[0]) return '';
              return '🕐 ' + items[0].label;
            },
            label: function(ctx) {
              if (ctx.dataset.type === 'scatter') {
                var idx = ctx.dataIndex;
                var pts = ctx.dataset.label.indexOf('↓') >= 0 ? _downPtsRef : _upPtsRef;
                var pt = pts[idx] || {};
                return (pt.direction === 'down' ? '📉 跌破' : '📈 突破') + ' ' + pt.threshold + '%';
              }
              var val = ctx.parsed.y;
              if (val == null) return '';
              if (ctx.datasetIndex === 0) {
                var lines = [];
                lines.push('💰 价格: ' + val.toFixed(1) + '¢');
                if (_entryPriceRef) {
                  var chg = ((val - _entryPriceRef) / _entryPriceRef * 100).toFixed(1);
                  var sign = parseFloat(chg) >= 0 ? '+' : '';
                  lines.push('📊 变动: ' + sign + chg + '%');
                  var diff = (val - _entryPriceRef).toFixed(1);
                  var diffSign = parseFloat(diff) >= 0 ? '+' : '';
                  lines.push('📐 差值: ' + diffSign + diff + '¢');
                }
                return lines;
              }
              if (ctx.datasetIndex === 1 && _entryPriceRef) {
                return '🎯 入场: ' + _entryPriceRef.toFixed(1) + '¢';
              }
              return '';
            },
            afterBody: function() { return ''; }
          }
        }
      },
      scales: {
        x: {
          display: true,
          ticks: { color: '#475569', maxTicksLimit: 6, font: { family: 'monospace', size: 9 } },
          grid: { color: 'rgba(30,33,48,0.4)', drawBorder: false }
        },
        y: {
          display: true,
          position: 'left',
          ticks: {
            color: '#475569',
            font: { family: 'monospace', size: 9 },
            callback: function(v) { return v.toFixed(0) + '¢'; }
          },
          grid: { color: 'rgba(30,33,48,0.3)', drawBorder: false }
        }
      },
      onHover: function(event, elements, chart) {
        if (!chart._bloombergMeta) chart._bloombergMeta = {};
        var area = chart.chartArea;
        var x = event.x || (event.native && event.native.offsetX);
        var y = event.y || (event.native && event.native.offsetY);
        if (x >= area.left && x <= area.right && y >= area.top && y <= area.bottom) {
          chart._bloombergMeta.hoverActive = true;
          chart._bloombergMeta.mouseX = x;
          chart._bloombergMeta.mouseY = y;
        } else {
          chart._bloombergMeta.hoverActive = false;
        }
        chart.draw();
      }
    }
  });

  canvas.addEventListener('mouseleave', function() {
    if (chartInstance._bloombergMeta) {
      chartInstance._bloombergMeta.hoverActive = false;
      chartInstance.draw();
    }
  });

  _dpChartInstances[betId] = chartInstance;
}
