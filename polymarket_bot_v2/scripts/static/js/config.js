
var cfgData = {};
var cfgDocs = {};
var cfgOriginal = {};
var cfgEdits = {};
var cfgCurrentSection = 'pt_basic';
var _isDryRun = true;
var cfgCrossSection = {};

var DRY_RUN_ONLY_KEYS = [
  'initial_balance',
  'slippage_factors',
  'min_volume_24h_reject', 'max_spread_reject', 'low_price_skip_threshold',
  'recent_sl_alarm_ratio',
  'weather_stop_loss_cooldown_hours', 'exit_cooldown_hours',
  'reentry_min_price_change', 'reentry_stop_loss_history_hours',
  'min_win_probability', 'min_win_probability_tiers',
  'min_model_count', 'ai_uncertainty_zone_width',
  'momentum_guard', 'arbitrage', 'high_conviction', 'dynamic_sizing'
];

var CFG_SECTION_ALIAS = {
  'pt_basic':    {realKey: 'paper_trading', group: 'basic'},
  'pt_entry':    {realKey: 'paper_trading', group: 'entry'},
  'pt_sizing':   {realKey: 'paper_trading', group: 'sizing'},
  'pt_category': {realKey: 'paper_trading', group: 'category'},
  'pt_weather':  {realKey: 'paper_trading', group: 'weather'},
  'pt_advanced': {realKey: 'paper_trading', group: 'advanced'},
};

var CFG_CATEGORIES = {
  'betting': {
    label: '📝 投注策略',
    sections: {
      'pt_basic':           '📋 基础设置',
      'pt_entry':           '🎯 入场条件',
      'pt_sizing':          '💰 仓位与金额',
      'pt_category':        '📊 赛道配置',
      'pt_weather':         '🌤️ 天气策略',
      'local_copy_trading': '🤝 跟投-执行 (V1 旧版)',
      'copytrader_v2':      '🚀 跟投跟卖 V2 (官方API)'
    }
  },
  'data': {
    label: '📡 数据源与扫描',
    sections: {
      'scanner': '🔍 扫描',
      'weather_ensemble': '🌤️ 天气引擎',
      'weather_strategy': '🌤️ 天气信号策略',
      'price_monitor': '📡 价格监控',
      'sports_live': '⚽ 体育实时',
      'sports_fetcher': '🏟️ 体育数据',
      'odds_fetcher': '🎲 赔率源',
      'crypto_engine': '₿ Crypto引擎'
    }
  },
  'signal': {
    label: '🤖 信号与AI',
    sections: {
      'signal': '🤖 信号',
      'smart_money': '💰 聪明钱',
      'evolution': '🧬 自进化分析'
    }
  },
  'risk': {
    label: '🛡️ 风控与退出',
    sections: {
      'risk': '🛡️ 风控',
      'smart_capital': '💰 智能资金',
      'profit_taking': '📈 止盈',
      'stop_loss': '📉 止损',
      'weather_confidence_hold': '🌡️高置信锁'
    }
  },
  'execution': {
    label: '📊 仓位与执行',
    sections: {
      'position_management': '📊 仓位管理',
      'order_execution': '🏷️ Maker下单',
      'executor': '⚙️ 执行器',
      'sync': '🔗 同步',
      'reconcile': '🔄 对账'
    }
  },
  'general': {
    label: '🌍 通用配置',
    sections: {
      'general': '🔧 通用',
      'telegram': '📱 Telegram',
      'telegram_bot': '🤖 TG机器人',
      'weather_cities': '🌍 天气城市',
    }
  }
};
var cfgCurrentCategory = 'betting';

function selectCategory(cat) {
  cfgCurrentCategory = cat;
  document.querySelectorAll('.cfg-cat-btn').forEach(function(el) {
    el.classList.toggle('active', el.dataset.cat === cat);
  });
  buildCategorySections(cat);
  var sections = CFG_CATEGORIES[cat].sections;
  var firstSec = Object.keys(sections)[0];
  selectConfigSection(firstSec);
}

function buildCategorySections(cat) {
  var container = document.getElementById('cfg-sections');
  var sections = CFG_CATEGORIES[cat].sections;
  var sectionKeys = Object.keys(sections);
  if (sectionKeys.length <= 1) {
    container.innerHTML = '';
    container.style.display = 'none';
    return;
  }
  container.style.display = 'flex';
  var html = '';
  sectionKeys.forEach(function(sec) {
    var isActive = sec === cfgCurrentSection ? ' active' : '';
    html += '<button class="cfg-sec-btn' + isActive + '" data-sec="' + sec + '" onclick="selectConfigSection(\'' + sec + '\')">' + sections[sec] + '</button>';
  });
  container.innerHTML = html;
}

var CFG_CATEGORY_LABELS = {
  'weather':'天气','sports':'体育','crypto':'加密货币','geopolitics':'地缘政治',
  'finance':'金融','tech':'科技','economy':'经济','pop_culture':'流行文化',
  'default':'默认值','esports':'电竞','weather_edge':'天气Edge桶'
};
function autoDocForDictSubKey(subKey, parentKey) {
  var catLabel = CFG_CATEGORY_LABELS[subKey];
  if (catLabel) {
    var parentShort = parentKey || '';
    return catLabel + ' 赛道的配置值（' + parentShort + '）';
  }
  if (parentKey && parentKey.indexOf('soccer_league') >= 0) {
    return '足球联赛「' + subKey + '」的质量系数';
  }
  if (parentKey && parentKey.indexOf('slippage') >= 0) {
    return '价格区间「' + subKey + '」的模拟滑点因子';
  }
  if (parentKey && parentKey.indexOf('per_category') >= 0 && catLabel) {
    return catLabel + ' 赛道的模式设置';
  }
  return '';
}

var CFG_BLOCKED = ['dry_run','executor','api_key','private_key','secret','proxy_address','clob_api_url','base_url','token','password'];

var CFG_HIDDEN = ['db_path', 'dry_run'];

function selectConfigSection(sec) {
  cfgCurrentSection = sec;
  document.querySelectorAll('.cfg-sec-btn').forEach(function(el) {
    el.classList.toggle('active', el.dataset.sec === sec);
  });
  var catForSec = findCategoryForSection(sec);
  if (catForSec && catForSec !== cfgCurrentCategory) {
    cfgCurrentCategory = catForSec;
    document.querySelectorAll('.cfg-cat-btn').forEach(function(el) {
      el.classList.toggle('active', el.dataset.cat === catForSec);
    });
    buildCategorySections(catForSec);
    document.querySelectorAll('.cfg-sec-btn').forEach(function(el) {
      el.classList.toggle('active', el.dataset.sec === sec);
    });
  }
  loadConfig();
}

function findCategoryForSection(sec) {
  for (var cat in CFG_CATEGORIES) {
    if (CFG_CATEGORIES[cat].sections[sec]) return cat;
  }
  return null;
}

function loadConfig() {
  var statusEl = document.getElementById('cfg-status');
  statusEl.textContent = '加载中...';
  statusEl.className = 'text-xs text-gray-500';
  var _alias = CFG_SECTION_ALIAS[cfgCurrentSection];
  var _apiSection = _alias ? _alias.realKey : cfgCurrentSection;
  var _filterGroup = _alias ? _alias.group : null;
  Promise.all([
    api('/api/config?section=' + _apiSection),
    api('/api/config-docs?section=' + _apiSection),
    api('/api/status').catch(function() { return {}; }),
    _apiSection === 'weather_strategy' ? api('/api/config?section=weather_city_scanner').catch(function(){return{};}) : Promise.resolve({}),
    _apiSection === 'weather_strategy' ? api('/api/config-docs?section=weather_city_scanner').catch(function(){return{};}) : Promise.resolve({})
  ]).then(function(results) {
    var d = results[0];
    var docsResp = results[1];
    var statusResp = results[2] || {};
    var cityDataResp = results[3] || {};
    var cityDocsResp = results[4] || {};
    if (statusResp.hasOwnProperty('dry_run')) {
      _isDryRun = !!statusResp.dry_run;
    }
    var data = d.data || {};
    cfgDocs = (docsResp && docsResp.docs) ? docsResp.docs : {};
    cfgData = {};
    Object.keys(data).forEach(function(k) { if (!k.startsWith('_')) cfgData[k] = data[k]; });
    cfgCrossSection = {};
    if (_apiSection === 'weather_strategy') {
      var cityData = cityDataResp.data || {};
      var cityDocs = (cityDocsResp && cityDocsResp.docs) ? cityDocsResp.docs : {};
      Object.keys(cityData).forEach(function(k) {
        if (k.startsWith('_')) return;
        var nsKey = 'wcs_' + k;
        cfgData[nsKey] = cityData[k];
        cfgCrossSection[nsKey] = 'weather_city_scanner.' + k;
      });
      Object.keys(cityDocs).forEach(function(dk) {
        cfgDocs['wcs_' + dk] = cityDocs[dk];
      });
    }
    cfgOriginal = {};
    cfgEdits = {};
    updateSaveBar();
    buildConfigEditor(cfgData, _apiSection, _filterGroup);
    if (cfgCurrentSection === 'smart_money') {
      setTimeout(function() { loadWalletPanel(); }, 50);
    }
    if (cfgCurrentSection === 'smart_capital') {
      setTimeout(function() { refreshSmartCapitalPreview(); }, 100);
    }
    statusEl.textContent = '已加载 ' + cfgCurrentSection;
    statusEl.className = 'text-xs text-green';
    setTimeout(function() { statusEl.className = 'text-xs text-gray-500'; }, 2000);
  }).catch(function(e) {
    statusEl.textContent = '加载失败';
    statusEl.className = 'text-xs text-red';
    document.getElementById('cfg-editor').innerHTML = '<div class="text-red text-xs py-4">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function buildConfigEditor(data, prefix, filterGroup) {
  var editor = document.getElementById('cfg-editor');
  var html = '';

  if (prefix === 'weather_cities') {
    buildWeatherCitiesEditor(data);
    return;
  }

  var PT_GROUPS = {
    'basic': {label:'📋 基础设置',subgroups:[
      {label:'⏱️ 运行周期',keys:['cycle_interval_minutes','position_check_interval_minutes','outcome_check_interval_minutes','signal_cache_hours','weather_only','recent_sl_alarm_ratio']},
      {label:'📊 持仓与冷却',keys:['initial_balance','max_open_positions','max_per_market_positions','reentry_min_price_change','reentry_stop_loss_history_hours','stop_loss_cooldown_hours','exit_cooldown_hours']}
    ]},
    'entry': {label:'🎯 入场条件',subgroups:[
      {label:'📐 EV与置信度',keys:['min_model_count','ai_uncertainty_zone_width','min_win_probability','min_win_probability_tiers']},
      {label:'💹 价格与流动性',keys:['min_volume_24h_reject','max_spread_reject','divergence_limits','low_price_skip_threshold','max_entry_price']},
      {label:'🚫 费用与过滤',keys:['min_net_edge_after_fees','blocked_categories']}
    ]},
    'sizing': {label:'💰 仓位与金额',subgroups:[
      {label:'💵 金额限制',keys:['hard_max_bet_usdc','fixed_bet_size_usdc','balance_reserve_min_usdc']},
      {label:'📊 费率与动态仓位',keys:['taker_fee_rates','taker_fee_rate_default','dynamic_sizing','slippage_factors']}
    ]},
    'category': {label:'📊 赛道配置',subgroups:[
      {label:'📋 通用赛道',keys:['category_min_ev','category_min_confidence','category_max_positions','category_max_per_event','category_max_per_asset','settlement_max_hours','signal_category_priority']},
      {label:'⚽ 体育赛道',keys:['sports'],flatten:true},
      {label:'₿ Crypto赛道',keys:['crypto'],flatten:true},
      {label:'💰 聪明钱对赛道影响',keys:['smart_money'],flatten:true}
    ]},
    'weather': {label:'🌤️ 天气策略 (运行约束)',subgroups:[
      {label:'🔵 右尾做空优先城市',keys:['weather_priority_cities_right_tail']},
      {label:'🔷 左尾做空优先城市',keys:['weather_priority_cities_left_tail']},
      {label:'🌡️ 时间卫士 (time_guard)',keys:['weather_entry_window_hours','weather_time_guard']},
      {label:'🔮 YesBest融合 (yesbest_fusion)',keys:['yesbest_fusion'],flatten:true}
    ]},
    'advanced': {label:'⚡ 高级策略',keys:['momentum_guard','circuit_breaker','arbitrage','high_conviction']}
  };
  var PT_GROUP_DOC = {
    'basic':'系统基础参数（运行周期·持仓与冷却 2个子分组）',
    'entry':'入场过滤条件（EV与置信度·价格与流动性·费用与过滤 3个子分组）',
    'sizing':'仓位金额控制（金额限制·费率与动态仓位 2个子分组）— 赛道级仓位上限统一由 💰智能资金 SCE 管控',
    'category':'各赛道独立EV/置信度/持仓数/结算时限配置（通用赛道·体育·Crypto·聪明钱影响 4个子分组）',
    'weather':'天气赛道运行约束（右尾做空城市·左尾做空城市·时间卫士·YesBest融合） — 仓位上限由 💰智能资金 SCE 管控',
    'advanced':'动量守卫、熔断器、套利、高确定性等进阶策略'
  };

  var SECTION_SUBGROUPS = {
    'signal': [
      {label:'🧠 核心参数',keys:['min_ev_threshold','min_confidence','ensemble_models','arbiter_model','arbiter_weight','fallback_model','temperature','max_retries','second_model_min_ev','skip_on_insufficient_data','kelly_fraction']},
      {label:'📊 额度与轮换',keys:['pre_filter','max_tier1_markets','max_tier2_markets','max_tier3_markets','category_min_markets','rotation_pool_multiplier','rotation_memory_seconds']},
      {label:'⚽ 体育预筛',keys:['sports_max_enriched_per_cycle','sports_max_odds_verify_per_cycle','sports_odds_verify_min_ev','sports_prescreen_min_data_len','sports_prescreen_min_liquidity_usd','sports_prescreen_price_weight','sports_prescreen_urgency_weight','sports_prescreen_data_weight','sports_prescreen_liquidity_weight','sports_prescreen_data_norm_len','sports_prescreen_strong_price_min_dev']},
      {label:'📐 价格偏差与校准',keys:['price_deviation_hard_reject','price_deviation_soft_threshold','price_deviation_ref_max_age_seconds','price_deviation_ref_auto_update_threshold','crypto_narrow_range_threshold','probability_calibration','bayesian_update']}
    ],
    'profit_taking': [
      {label:'📈 基础止盈',keys:['enabled','certainty_sell_threshold','certainty_hold_threshold','certainty_hold_max_hours','certainty_hold_categories','near_ceiling_threshold','profit_take_pct','min_hold_minutes']},
      {label:'⏰ 到期止盈',keys:['expiry_hours_threshold','expiry_profit_pct','hold_to_settle_threshold','hold_to_settle_hours']},
      {label:'📉 Trailing止盈',keys:['trailing_enabled','trailing_drop_pct','trailing_min_profit_pct','trailing_tiers','weather_trailing_min_drop','trailing_floor_min_profit_pct','trailing_floor_dynamic_enabled','trailing_floor_retreat_pct','sports_trailing_floor_retreat_pct','legacy_cleanup_enabled','legacy_mismatch_threshold']},
      {label:'₿ Crypto止盈',keys:['crypto'],flatten:true},
      {label:'🌡️ 天气专属止盈',keys:['weather_quick_exit_price','weather_quick_entry_max','quick_exit_entry_max','weather_exit_gate_enabled','weather_exit_gate_profit_hold_pct','weather_exit_gate_safe_margin_f','weather_exit_gate_safe_margin_c']}
    ],
    'stop_loss': [
      {label:'🛑 基础止损',keys:['enabled','floor_price','weather_edge_floor_price','standard_stop_pct','sports_standard_stop_pct','crypto_standard_stop_pct','weather_standard_stop_pct']},
      {label:'⏰ 时间与到期',keys:['time_risk_stop_pct','reversal_exit_price','expiry_stop_hours','expiry_stop_loss_pct','early_stop']},
      {label:'🌤️ 天气与体育专属',keys:['weather_time_weighted_stop','weather_rotation_exit','sports_expired_min_loss_pct']},
      {label:'🛡️ 流动性守卫（防闪跌误抛）',keys:['liquidity_guard'],flatten:true},
      {label:'🌡️ 天气止损否决（阻止过早止损）',keys:['weather_veto'],flatten:true}
    ],
    'weather_ensemble': [
      {label:'🌤️ GFS核心',keys:['enabled','model','max_forecast_days','min_members_for_confidence','api_timeout','min_edge','gfs_cache_ttl_seconds','skill_score_curve']},
      {label:'🌍 ECMWF双模型',keys:['ecmwf_enabled','ecmwf_model','ecmwf_members','ecmwf_weight_high','ecmwf_weight_medium','ecmwf_divergence_threshold','ecmwf_divergence_high_threshold','ecmwf_cache_ttl_seconds','ecmwf_edge_threshold_high_confidence','fallback_on_divergence']},
      {label:'✈️ METAR校准',keys:['metar_enabled','metar_cache_ttl_seconds','gate_metar_cache_ttl_seconds','metar_api_timeout','metar_same_day_weight','metar_bias_correction_max','metar_bias_trigger_c','metar_bias_rate_per_c','metar_bias_max_shift']},
      {label:'📊 市场概率融入',keys:['market_prob_weight']},
      {label:'⚙️ 缓存与高级',keys:['api_request_interval_sec','cache_stale_ttl_seconds','cache_stale_fresh_seconds','cache_stale_recent_seconds','circuit_breaker_cooldown_sec','batch_chunk_size','batch_chunk_delay_seconds','deterministic_fallback_enabled','fallback_spread_celsius','fallback_synthetic_members','fallback_consecutive_warn_threshold','source_confidence_multiplier','consensus_spread_threshold','temp_divergence_warning_threshold','gaussian_smoothing','gfs_daily_sample_hours','gfs_grid_resolution_degrees','throttle_backoff_multiplier','throttle_max_interval_sec']}
    ],
    'executor': [
      {label:'🏷️ 下单参数',keys:['slippage_threshold','offset_pct','probe_size_usdc','probe_enabled','probe_min_order_usdc','min_bet_usdc']},
      {label:'📊 VWAP',keys:['vwap_enabled','vwap_max_deviation','vwap_min_depth_usdc','vwap_small_bet_threshold','vwap_bet_depth_multiplier']},
      {label:'🔄 卖出与结算',keys:['sell_offset_pct','sell_progressive_step','fill_timeout_sec','fok_timeout_sec','fill_poll_interval_sec','sweep_min_shares','settlement_delay_sec','sell_min_warn_interval_sec','bid_sanity_min_ratio','oracle_terminal_thresh']},
      {label:'💰 余额与验证',keys:['balance_retry_count','balance_retry_interval_sec','chain_verify_initial_delay_sec']},
      {label:'📦 订单与补买',keys:['min_order_size_shares','expired_sell_fail_threshold','post_buy_topup_enabled','post_buy_topup_buffer_shares','post_buy_topup_max_retries','post_buy_topup_safety_floor_shares','max_topup_amplification','max_topup_usdc','max_clob_amplification']},
      {label:'🔒 流动性门控（M2）',keys:['min_orderbook_depth_usdc','sell_fail_count_for_aggressive','sell_fail_count_for_alert']}
    ],
    'scanner': [
      {label:'🔍 扫描与排序',keys:['top_n','gamma_events_limit','min_score','categories_bonus','category_top_n','dimension_sorts']},
      {label:'⚖️ 套利检测',keys:['arbitrage_detection_enabled','arbitrage_threshold','arbitrage_min_liquidity']},
      {label:'🏷️ 赛道过滤',keys:['blacklist_keywords','sports_tag_slugs','crypto_daily_tag_filter','crypto_tag_slugs','blocked_categories']},
      {label:'🌤️ 天气优化',keys:['weather_tomorrow_bonus','weather_strike_optimizer_enabled','strike_optimizer_total_timeout_sec','strike_optimizer_event_timeout_sec','max_weather_strike_opt_events']}
    ],
    'price_monitor': [
      {label:'📡 监控设置',keys:['enabled','sample_interval_seconds','db_flush_interval_seconds','memory_buffer_size','retention_days','cleanup_interval_hours']},
      {label:'🔔 告警与自动卖出',keys:['thresholds_down_pct','thresholds_up_pct','telegram_alert_on_down','telegram_alert_on_up','alert_cooldown_seconds','auto_sell_enabled','auto_sell_threshold_pct']},
      {label:'⚡ 闪崩检测',keys:['flash_crash'],flatten:true}
    ],
    'local_copy_trading': [
      {label:'⚙️ 基础',keys:['enabled','max_concurrent_follows','default_base_usdc','max_error_count','max_per_event','trade_lookback_minutes','scan_interval_minutes']},
      {label:'🔍 过滤',keys:['category_size_multiplier','tag_filter_mode','min_ev_filter','max_price_filter']},
      {label:'💰 动态投注缩放',keys:['dynamic_usdc_enabled','dynamic_usdc_scale_anchor','dynamic_usdc_min_shares']},
      {label:'🔄 智能跟卖',keys:['follow_sell']}
    ],
    'copytrader_v2': [
      {label:'⚙️ Daemon 基础',keys:['enabled','scan_interval_seconds','pool_refresh_interval_seconds','data_source']},
      {label:'💵 仓位与并发',keys:['default_base_usdc','max_concurrent_follows_per_user']},
      {label:'🔍 信号过滤',keys:['min_score','max_price_filter','min_ev_filter','trade_lookback_minutes','exclude_bots']},
      {label:'🔄 跟卖',keys:['follow_sell_enabled']},
      {label:'📊 赛道倍率',keys:['category_size_multiplier']}
    ],
    'telegram': [
      {label:'📱 通知开关',keys:['enabled','notify_new_bet','notify_position_closed','notify_risk_alert','notify_cycle_summary']},
      {label:'⏱️ 限流',keys:['summary_interval_cycles','rate_limit_per_minute','risk_alert_cooldown_sec','close_batch_window_sec']}
    ],
    'risk': [
      {label:'📊 仓位限制',keys:['max_single_bet_pct','max_single_market_pct']},
      {label:'🛑 停机触发',keys:['daily_loss_pause_pct','cumulative_loss_pause_pct','min_absolute_daily_loss_usdc','min_absolute_cumulative_loss_usdc','balance_floor_usdc','category_pause_on_loss_streak']},
      {label:'🔄 智能自动恢复',keys:['auto_recovery_enabled','auto_recovery_buffer_pct']},
      {label:'📤 提现告警（不停机）',keys:['withdraw_trigger_pct','deposit_detection_threshold_usdc']},
      {label:'🚨 紧急冻结',keys:['emergency_freeze']}
    ],
    'sports_live': [
      {label:'⏰ 时间窗口',keys:['game_window_hours','post_expiry_window_hours','fast_monitor_interval_seconds']},
      {label:'📈 止盈止损',keys:['floor_price','standard_stop_pct','reversal_exit_price','near_ceiling_threshold','profit_take_pct']}
    ],
    'odds_fetcher': [
      {label:'⏱️ 缓存TTL',keys:['main_cache_ttl_seconds','today_cache_ttl_seconds','live_cache_ttl_seconds','clv_cache_ttl_seconds','settlement_cache_ttl_seconds','metadata_cache_ttl_seconds']},
      {label:'⚙️ API配置',keys:['max_daily_calls','preferred_bookmakers','clv_bookmakers','target_sport_ids']}
    ],
    'general': [
      {label:'⚙️ 运行控制',keys:['scan_interval_minutes','position_monitor_interval_minutes','main_loop_sleep_seconds','log_level']},
      {label:'💾 数据',keys:['live_initial_deposit','api_max_workers']}
    ],
    'smart_money': [
      {label:'⚙️ 基础',keys:['enabled','refresh_interval_hours','top_n_wallets','min_pnl_usd']},
      {label:'📡 数据源',keys:['categories','fallback_build_id','manual_wallets']}
    ],
    'position_management': [
      {label:'⚙️ 基础',keys:['enabled','reconcile_writeoff_reopen_value','reconcile_exit_reopen_value']},
      {label:'🗑️ 死仓处理',keys:['dead_position_max_sell_retries','dead_position_price_threshold','dead_position_no_reopen_price']},
      {label:'📈 加仓配置 (scale_in)',keys:['scale_in'],flatten:true},
      {label:'🔪 整笔卖出 (sell_one_bet)',keys:['sell_one_bet'],flatten:true}
    ],
    'order_execution': [
      {label:'📤 买入模式',keys:['buy_mode'],flatten:true},
      {label:'📥 卖出模式',keys:['sell_mode'],flatten:true},
      {label:'🏷️ Maker参数',keys:['maker'],flatten:true},
      {label:'🎯 Taker参数',keys:['taker'],flatten:true},
      {label:'🧠 智能模式',keys:['smart_mode'],flatten:true}
    ],
    'smart_capital': [
      {label:'🏆 余额分层 Tiers',keys:['tiers'],flatten:true}
    ],
    'crypto_engine': [
      {label:'📊 置信度',keys:['confidence_base','confidence_tiers'],flatten:true},
      {label:'📈 市场参数',keys:['min_annual_volatility','fng_extreme_fear_threshold','fng_extreme_greed_threshold']}
    ],
    'evolution': [
      {label:'🧬 进化参数',keys:['enabled','min_samples','interval_bets','interval_hours','persist_overrides']}
    ],

    'reconcile': [
      {label:'🔄 对账参数',keys:['min_age_seconds','platform_check_interval_cycles','chain_verify_retries','chain_verify_retry_delay_sec']}
    ],
    'sync': [
      {label:'🔗 同步参数',keys:['enabled','min_position_shares','min_position_usdc','dust_threshold_shares']}
    ],
    'weather_confidence_hold': [
      {label:'🌡️ 高置信锁参数',keys:['enabled','max_ai_prob','hours_before_expiry']}
    ],
    'weather_strategy': [
      {label:'🔵 右尾做空核心参数',keys:['right_tail_max_signals','right_tail_left_enabled','no_max_entry_price','right_tail_confidence_cap','right_tail_single_model_confidence_cap','right_tail_single_model_position_multiplier','block_rightmost_bucket','right_tail_max_gfs_probability','right_tail_min_sigma_from_mean']},
      {label:'🎯 右尾做空偏移控制',keys:['right_tail_offset_min','right_tail_offset_max','right_tail_offset_preference','right_tail_high_risk_cities']},
      {label:'💎 右尾做空加买',keys:['pure_coverage'],flatten:true,extra_keys:['addon_max_signals']},
      {label:'🔷 左尾做空核心参数',keys:['left_tail_enabled']},
      {label:'🟦 左尾做空偏移控制',keys:[]},
      {label:'🌙 夜间左偏做空',keys:[]},
      {label:'🌡️ 暖夜高温做空',keys:[]},
      {label:'🔮 高赢尾桶策略',keys:['extreme_lock'],flatten:true},
      {label:'🟡 YES埋伏信号',keys:['ambush_enabled','ambush_shadow_mode','ambush_independent_entry_enabled','ambush_min_hours_to_expiry','ambush_max_ask','ambush_max_distance','ambush_size_usdc','ambush_max_signals','ambush_empirical_curve']},
      {label:'🚪 YES埋伏退出',keys:['ambush_target_exit_price','ambush_force_exit_hours_after_peak','ambush_max_hold_hours','ambush_floor_price','ambush_stop_loss_pct']},
      {label:'🔍 扫描与过滤',keys:['min_group_strikes','min_market_volume_24h','max_cities_per_cycle','gamma_tag_slug_right_tail','gamma_tag_slug_left_tail']},
      {label:'📊 WCS质量评分',keys:['wcs_enabled','wcs_min_score','wcs_corr_decay_tau','wcs_corr_penalty_weight','wcs_taker_fee_rate','wcs_divergence_pos_mult_floor','wcs_divergence_blend_ecmwf_weight','wcs_right_tail_min_ev','right_tail_single_model_score_bonus','right_tail_high_risk_score_bonus','right_tail_divergence_score_bonus']},
      {label:'🔒 NO仓位控制',keys:['wcs_weather_max_daily_exposure_pct','wcs_weather_min_shares_buffer','wcs_weather_no_compress_sizing']},
        {label:'🏙️ 城市历史准确率（M5）',keys:['wcs_city_accuracy_enabled','wcs_city_accuracy_window_days','wcs_city_accuracy_low_threshold','wcs_city_accuracy_min_sample','wcs_city_accuracy_score_penalty','wcs_city_accuracy_pos_multiplier']},
      {label:'🧠 智能入场窗口',keys:['smart_entry_enabled','peak_window_buy_offset_hours','max_days_until_event','signal_cache_hours','min_history_rounds_for_buy']},
      {label:'📈 趋势与稳定性',keys:['enable_price_trend_scoring','trend_improving_bonus','trend_worsening_penalty','signal_stability_min_pct']},
      {label:'🌧️ 降雨修正因子',keys:['precipitation_boost_factor','precipitation_boost_threshold']},
      {label:'🛡️ 评分持有保护',keys:['wcs_score_hold'],flatten:true}
    ]
  };

  if (prefix === 'smart_money') {
    html += '<div id="wallet-panel" class="mb-4 rounded-lg border border-indigo-700 bg-gray-900 p-4">';
    html += '<div class="flex items-center justify-between mb-3">';
    html += '<h4 class="text-sm font-semibold text-indigo-300">💼 聪明钱 manual_wallets 管理</h4>';
    html += '<button onclick="loadWalletPanel()" class="text-xs text-accent hover:underline">🔄 刷新</button>';
    html += '</div>';
    html += '<p class="text-xs text-gray-400 mb-3">追踪这些钱包地址的链上持仓，识别大户入场信号。格式: 0x开头的以太坊地址。</p>';
    html += '<div id="wallet-list" class="space-y-1 mb-3"><div class="text-gray-500 text-xs">加载中...</div></div>';
    html += '<div class="flex gap-2">';
    html += '<input id="wallet-new-input" type="text" placeholder="0x... 钱包地址 (42位)" class="flex-1 px-3 py-2 text-xs font-mono rounded-lg border border-gray-600 bg-gray-800 text-white" autocomplete="off" maxlength="42" />';
    html += '<button onclick="addSmartMoneyWallet()" class="px-3 py-2 bg-indigo-600 text-white text-xs font-semibold rounded-lg hover:bg-indigo-700 transition whitespace-nowrap">+ 添加</button>';
    html += '</div>';
    html += '<div id="wallet-result" class="mt-2" style="display:none"></div>';
    html += '</div>';
  }

  if (prefix === 'smart_capital') {
    html += '<div id="sc-preview-panel" class="mb-4 rounded-lg border border-yellow-700 bg-gray-900 p-4">';
    html += '<div class="flex items-center justify-between mb-3">';
    html += '<h4 class="text-sm font-semibold text-yellow-300">💰 智能资金实时预览</h4>';
    html += '<button onclick="refreshSmartCapitalPreview()" class="text-xs text-accent hover:underline">🔄 刷新</button>';
    html += '</div>';
    html += '<p class="text-xs text-gray-400 mb-3">输入模拟余额，实时查看当前 Tier 和各赛道最大下注额（修改参数保存后生效）。</p>';
    html += '<div class="flex gap-2 items-center mb-3">';
    html += '<label for="sc-preview-balance" class="text-xs text-gray-400 whitespace-nowrap">模拟余额 USDC:</label>';
    html += '<input id="sc-preview-balance" type="number" value="20" min="0.01" max="99999" step="0.01" ';
    html += 'class="w-28 px-2 py-1 text-xs rounded border border-gray-600 bg-gray-800 text-white" ';
    html += 'onchange="refreshSmartCapitalPreview()" />';
    html += '<button onclick="refreshSmartCapitalPreview()" class="px-3 py-1 bg-yellow-700 text-white text-xs font-semibold rounded hover:bg-yellow-600 transition">计算</button>';
    html += '</div>';
    html += '<div id="sc-preview-result" class="text-xs text-gray-400">点击「计算」查看预估下注金额</div>';
    html += '</div>';
    setTimeout(function() { refreshSmartCapitalPreview(); }, 150);
  }

  var _ptSubgroups = null;

  if (prefix === 'paper_trading') {
    if (filterGroup) {
      var gdoc = PT_GROUP_DOC[filterGroup] || '';
      if (gdoc) html += '<div class="cfg-group-doc">💡 ' + escHtml(gdoc) + '</div>';
      var grp = PT_GROUPS[filterGroup];
      if (grp && grp.subgroups) {
        _ptSubgroups = grp.subgroups;
        var keys = [];
      } else {
        var allowedKeys = grp ? grp.keys : [];
        var keys = allowedKeys.filter(function(k) { return data.hasOwnProperty(k); });
      }
    } else {
      if (!window._ptSubTab) window._ptSubTab = 'basic';
      html += '<div class="cfg-sub-tabs" id="cfg-sub-tabs">';
      Object.keys(PT_GROUPS).forEach(function(gk) {
        var g = PT_GROUPS[gk];
        var cls = gk === window._ptSubTab ? ' active' : '';
        html += '<button class="cfg-sub-btn' + cls + '" onclick="window._ptSubTab=\'' + gk + '\';buildConfigEditor(cfgData,\'paper_trading\')">' + g.label + '</button>';
      });
      html += '</div>';
      var gdoc = PT_GROUP_DOC[window._ptSubTab] || '';
      if (gdoc) html += '<div class="cfg-group-doc">💡 ' + escHtml(gdoc) + '</div>';
      var grpLegacy = PT_GROUPS[window._ptSubTab];
      if (grpLegacy && grpLegacy.subgroups) {
        _ptSubgroups = grpLegacy.subgroups;
        var keys = [];
      } else {
        var allowedKeys = grpLegacy ? grpLegacy.keys : [];
        var keys = allowedKeys.filter(function(k) { return data.hasOwnProperty(k); });
      }
    }
  } else {
    var secSg = SECTION_SUBGROUPS[prefix];
    if (secSg) {
      _ptSubgroups = secSg;
      var keys = [];
    } else {
      var keys = Object.keys(data).sort(function(a, b) {
        var aObj = typeof data[a] === 'object' && data[a] !== null && !Array.isArray(data[a]);
        var bObj = typeof data[b] === 'object' && data[b] !== null && !Array.isArray(data[b]);
        if (aObj !== bObj) return aObj ? 1 : -1;
        return a.localeCompare(b);
      });
    }
  }

  if (_ptSubgroups) {
    _ptSubgroups.forEach(function(sg, idx) {
      var sgKeys = sg.keys.filter(function(k) { return data.hasOwnProperty(k); });
      if (sgKeys.length === 0) return;

      var flatObj = null;
      var flatKey = null;
      if (sg.flatten && sgKeys.length === 1) {
        flatKey = sgKeys[0];
        var fv = data[flatKey];
        if (typeof fv === 'object' && fv !== null && !Array.isArray(fv)) {
          flatObj = fv;
        }
      }

      var _hideFilter = function(k) { return !k.startsWith('_') && !(!_isDryRun && DRY_RUN_ONLY_KEYS.indexOf(k) >= 0); };
      var itemCount = flatObj ? Object.keys(flatObj).filter(_hideFilter).length : sgKeys.filter(_hideFilter).length;
      if (itemCount === 0) return;
      var openClass = '';
      html += '<div class="accordion cfg-subgroup' + openClass + '">';
      html += '<div class="accordion-header" onclick="this.parentElement.classList.toggle(\'open\')">';
      html += '<h3>' + sg.label + ' <span style="font-size:0.7rem;color:#64748b;font-weight:400">(' + itemCount + ' 项)</span></h3>';
      html += '<span class="accordion-chevron">▼</span>';
      html += '</div>';
      html += '<div class="accordion-body"><div class="accordion-inner">';

      if (flatObj) {
        Object.keys(flatObj).sort(function(a, b) {
          var aObj = typeof flatObj[a] === 'object' && flatObj[a] !== null && !Array.isArray(flatObj[a]);
          var bObj = typeof flatObj[b] === 'object' && flatObj[b] !== null && !Array.isArray(flatObj[b]);
          if (aObj !== bObj) return aObj ? 1 : -1;
          return a.localeCompare(b);
        }).forEach(function(sk) {
          if (sk.startsWith('_')) return;
          var sv = flatObj[sk];
          var _flatBase = (cfgCrossSection && cfgCrossSection[flatKey]) ? cfgCrossSection[flatKey] : prefix + '.' + flatKey;
          var sp = _flatBase + '.' + sk;
          var sd = cfgDocs[flatKey + '.' + sk] || '';
          if (!sd) sd = autoDocForDictSubKey(sk, flatKey.replace(/^wcs_/, ''));
          var sb = CFG_BLOCKED.some(function(bk) { return sk.toLowerCase().indexOf(bk) >= 0; });
          html += renderConfigRow(sk, sv, sp, sd, sb);
        });
      } else {
        sgKeys.forEach(function(key) {
          var val = data[key];
          var fullPath = (cfgCrossSection && cfgCrossSection[key]) ? cfgCrossSection[key] : prefix + '.' + key;
          var displayKey = key.replace(/^wcs_/, '');
          var doc = cfgDocs[key] || '';
          var isBlocked = CFG_BLOCKED.some(function(bk) { return displayKey.toLowerCase().indexOf(bk) >= 0; });
          html += renderConfigRow(displayKey, val, fullPath, doc, isBlocked);
        });
      }

      html += '</div></div></div>';
    });
    if (prefix !== 'paper_trading') {
      var allSgKeys = {};
      _ptSubgroups.forEach(function(sg){ sg.keys.forEach(function(k){ allSgKeys[k]=true; }); });
      Object.keys(data).sort().forEach(function(key) {
        if (allSgKeys[key]) return;
        if (key.startsWith('_')) return;
        if (prefix === 'general' && CFG_HIDDEN.indexOf(key) >= 0) return;
        if (prefix === 'smart_money' && key === 'manual_wallets') return;
        var val = data[key];
        var fullPath = prefix + '.' + key;
        var doc = cfgDocs[key] || '';
        var isBlocked = CFG_BLOCKED.some(function(bk) { return key.toLowerCase().indexOf(bk) >= 0; });
        html += renderConfigRow(key, val, fullPath, doc, isBlocked);
      });
    }
  }

  keys.forEach(function(key) {
    var val = data[key];
    var fullPath = prefix + '.' + key;
    var doc = cfgDocs[key] || '';
    if (prefix === 'general' && CFG_HIDDEN.indexOf(key) >= 0) return;
    if (prefix === 'smart_money' && key === 'manual_wallets') return;
    var isBlocked = CFG_BLOCKED.some(function(bk) { return key.toLowerCase().indexOf(bk) >= 0; });
    html += renderConfigRow(key, val, fullPath, doc, isBlocked);
  });

  editor.innerHTML = html || '<div class="text-gray-500 text-xs py-4">此 section 为空</div>';
}

function renderConfigRow(key, val, path, doc, blocked) {
  if (!_isDryRun && DRY_RUN_ONLY_KEYS.indexOf(key) >= 0) return '';
  if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
    var parentDoc = doc || '';
    var relBase = path.indexOf('.') >= 0 ? path.slice(path.indexOf('.') + 1) : path;
    var subCount = Object.keys(val).filter(function(k){return !k.startsWith('_');}).length;
    var headerLabel = (doc && typeof doc === 'string') ? doc : key;
    var subHtml = '<div class="cfg-nested-panel">';
    subHtml += '<div class="cfg-nested-header" onclick="this.parentElement.classList.toggle(\'open\')">';
    subHtml += '<h3>' + escHtml(headerLabel) + ' <span style="font-size:0.7rem;color:#64748b;font-weight:400">(' + subCount + ' 项)</span></h3>';
    if (doc && headerLabel !== key) subHtml += '<span class="cfg-key-secondary" style="display:block;margin-top:2px">' + escHtml(key) + '</span>';
    subHtml += '<span class="accordion-chevron">▼</span>';
    subHtml += '</div>';
    subHtml += '<div class="cfg-nested-body">';
    if (parentDoc && typeof parentDoc === 'string') subHtml += '<div class="cfg-group-doc">' + escHtml(parentDoc) + '</div>';
    Object.keys(val).sort(function(a, b) {
      var aObj = typeof val[a] === 'object' && val[a] !== null && !Array.isArray(val[a]);
      var bObj = typeof val[b] === 'object' && val[b] !== null && !Array.isArray(val[b]);
      if (aObj !== bObj) return aObj ? 1 : -1;
      return a.localeCompare(b);
    }).forEach(function(sk) {
      if (sk.startsWith('_')) return;
      var sv = val[sk];
      var sp = path + '.' + sk;
      var sd = cfgDocs[relBase + '.' + sk] || '';
      if (!sd) sd = autoDocForDictSubKey(sk, key);
      var sb = blocked || CFG_BLOCKED.some(function(bk) { return sk.toLowerCase().indexOf(bk) >= 0; });
      subHtml += renderConfigRow(sk, sv, sp, sd, sb);
    });
    subHtml += '</div></div>';
    return subHtml;
  }

  cfgOriginal[path] = (Array.isArray(val) || (typeof val === 'object' && val !== null))
    ? JSON.parse(JSON.stringify(val)) : val;
  var inputHtml = '';

  if (key === 'dry_run' && typeof val === 'boolean') {
    var dryClass = val ? 'dry' : 'live';
    var dryLabel = val ? '🟢 DRY RUN（模拟模式）' : '🔴 LIVE（实盘模式）';
    inputHtml = '<button class="cfg-dryrun-toggle ' + dryClass + '" onclick="toggleDryRun()">' + dryLabel + '</button>';
    var drDocId = 'doc-' + path.replace(/\./g, '-');
    var drInfoBtn = doc ? ' <button class="cfg-info-btn" onclick="toggleParamDoc(\'' + drDocId + '\')" title="' + escHtml(doc.slice(0, 80)) + '">ⓘ</button>' : '';
    var drDocHtml = doc ? '<div id="' + drDocId + '" class="cfg-key-doc">' + escHtml(doc) + '</div>' : '';
    return '<div class="cfg-row" style="background:#111422;border:1px solid ' + (val ? '#22c55e30' : '#ef444430') + ';border-radius:8px;margin:4px 0">' +
      '<div class="cfg-key"><div class="cfg-key-name">' + escHtml(key) + drInfoBtn + '</div>' + drDocHtml + '</div>' + inputHtml + '</div>';
  }

  if (key === 'emergency_freeze' && typeof val === 'boolean') {
    var efFrozen = val;
    var efBg = efFrozen ? '#3f0a0a' : '#0a1f0a';
    var efBorder = efFrozen ? '#ef4444' : '#22c55e';
    var efLabel = efFrozen ? '⚡ 紧急冻结已启动！（点击解除）' : '✅ 正常运行中（点击启动紧急冻结）';
    var efBtnCls = efFrozen ? 'cfg-dryrun-toggle live' : 'cfg-dryrun-toggle dry';
    var efDocId = 'doc-' + path.replace(/\./g, '-');
    var efInfoBtn = doc ? ' <button class="cfg-info-btn" onclick="toggleParamDoc(\'' + efDocId + '\')" title="' + escHtml(doc.slice(0, 80)) + '">ⓘ</button>' : '';
    var efDocHtml = doc ? '<div id="' + efDocId + '" class="cfg-key-doc">' + escHtml(doc) + '</div>' : '';
    return '<div class="cfg-row" style="background:' + efBg + ';border:2px solid ' + efBorder + ';border-radius:8px;margin:4px 0;padding:6px">' +
      '<div class="cfg-key"><div class="cfg-key-name" style="font-weight:700;font-size:13px">' + escHtml(key) + efInfoBtn + '</div>' + efDocHtml + '</div>' +
      '<button class="' + efBtnCls + '" style="font-size:13px;padding:6px 14px" onclick="toggleEmergencyFreeze()">' + efLabel + '</button></div>';
  }

  if (typeof val === 'boolean') {
    var effectiveBool = (cfgEdits[path] !== undefined) ? cfgEdits[path] : val;
    var cls = 'cfg-toggle' + (effectiveBool ? ' on' : '') + (blocked ? ' locked' : '') + (cfgEdits[path] !== undefined ? ' changed' : '');
    inputHtml = '<div class="' + cls + '" data-path="' + escHtml(path) + '" ' +
      (blocked ? '' : 'onclick="toggleConfigBool(this)"') + '></div>';
  } else if (typeof val === 'number') {
    var step = (val % 1 !== 0) ? '0.001' : '1';
    var dispVal = (cfgEdits[path] !== undefined) ? cfgEdits[path] : val;
    inputHtml = '<input type="number" step="' + step + '" class="cfg-input' + (blocked ? ' locked' : '') + (cfgEdits[path] !== undefined ? ' changed' : '') + '" ' +
      'data-path="' + escHtml(path) + '" value="' + dispVal + '" ' +
      (blocked ? 'disabled' : 'onchange="onConfigChange(this)" onkeydown="if(event.key===\'Enter\'){saveConfig();}"') + '>';
  } else if (typeof val === 'string') {
    var dispStrVal = (cfgEdits[path] !== undefined) ? cfgEdits[path] : val;
    inputHtml = '<input type="text" class="cfg-input' + (blocked ? ' locked' : '') + (cfgEdits[path] !== undefined ? ' changed' : '') + '" ' +
      'data-path="' + escHtml(path) + '" value="' + escHtml(dispStrVal) + '" ' +
      'style="width:200px;text-align:left" ' +
      (blocked ? 'disabled' : 'onchange="onConfigChange(this)" onkeydown="if(event.key===\'Enter\'){saveConfig();}"') + '>';
  } else if (Array.isArray(val)) {
    var arrPanelId = 'arr-panel-' + path.replace(/\./g, '-');
    var arrListId = 'arr-list-' + path.replace(/\./g, '-');
    var arrInputId = 'cfg-add-' + path.replace(/\./g, '-');
    var isChanged = cfgEdits[path] !== undefined;
    var countBadge = '<span class="cfg-arr-count">' + val.length + '</span>';
    var changedBadge = isChanged ? ' <span style="color:#eab308;font-size:0.65rem">●已修改</span>' : '';

    var hasObjectItems = val.some(function(item) { return item !== null && typeof item === 'object' && !Array.isArray(item); });

    var listHtml = '';
    if (val.length === 0) {
      listHtml = '<div class="cfg-arr-empty">（空列表）</div>';
    } else if (hasObjectItems) {
      listHtml = '<div class="cfg-arr-cards">';
      val.forEach(function(item, idx) {
        if (item === null || typeof item !== 'object') {
          listHtml += '<div class="cfg-arr-card">' +
            '<div class="cfg-arr-card-header">' +
              '<span class="cfg-arr-card-title">#' + (idx + 1) + '</span>' +
              (blocked ? '' : '<button class="cfg-arr-item-del" onclick="removeArrayItem(\'' + escHtml(path) + '\',' + idx + ')" title="删除此项">×</button>') +
            '</div>' +
            '<div class="cfg-arr-card-body"><span style="color:#94a3b8">' + escHtml(String(item)) + '</span></div>' +
          '</div>';
          return;
        }
        var cardTitle = item.name || item.label || item.id || ('#' + (idx + 1));
        var cardSubtitle = '';
        if (item.max_balance !== undefined) cardSubtitle = '≤ $' + item.max_balance;
        else if (item.max_return !== undefined) cardSubtitle = '≤ ' + (item.max_return * 100) + '%';

        listHtml += '<div class="cfg-arr-card">' +
          '<div class="cfg-arr-card-header" onclick="toggleArrCard(this)">' +
            '<div>' +
              '<span class="cfg-arr-card-title">' + escHtml(String(cardTitle)) + '</span>' +
              (cardSubtitle ? '<span class="cfg-arr-card-sub">' + escHtml(cardSubtitle) + '</span>' : '') +
            '</div>' +
            '<div class="cfg-arr-card-actions">' +
              (blocked ? '' : '<button class="cfg-arr-item-del" onclick="event.stopPropagation();removeArrayItem(\'' + escHtml(path) + '\',' + idx + ')" title="删除此项">×</button>') +
              '<span class="cfg-arr-card-chevron">▸</span>' +
            '</div>' +
          '</div>' +
          '<div class="cfg-arr-card-body" style="display:none">';

        var fieldKeys = Object.keys(item);
        fieldKeys.forEach(function(fk) {
          var fv = item[fk];
          var fieldPath = path + '.' + idx + '.' + fk;
          if (fv !== null && typeof fv === 'object' && !Array.isArray(fv)) {
            listHtml += '<div class="cfg-arr-card-field">' +
              '<span class="cfg-arr-card-fk">' + escHtml(fk) + '</span>' +
              '<div class="cfg-arr-card-subobj">';
            Object.keys(fv).forEach(function(sk) {
              var sv = fv[sk];
              var subPath = fieldPath + '.' + sk;
              var subInputType = typeof sv === 'number' ? 'number' : 'text';
              var subStep = (typeof sv === 'number' && sv % 1 !== 0) ? '0.001' : '1';
              listHtml += '<div class="cfg-arr-card-subrow">' +
                '<span class="cfg-arr-card-sk">' + escHtml(sk) + '</span>' +
                '<input type="' + subInputType + '"' + (subInputType === 'number' ? ' step="' + subStep + '"' : '') +
                  ' class="cfg-arr-field-input' + (blocked ? ' locked' : '') + '" ' +
                  'data-arr-path="' + escHtml(path) + '" data-arr-idx="' + idx + '" data-field="' + escHtml(fk + '.' + sk) + '" ' +
                  'value="' + escHtml(String(sv)) + '" ' +
                  (blocked ? 'disabled' : 'onchange="onArrFieldChange(this)"') + '>' +
              '</div>';
            });
            listHtml += '</div></div>';
          } else {
            var inputType = typeof fv === 'number' ? 'number' : (typeof fv === 'boolean' ? 'checkbox' : 'text');
            var step = (typeof fv === 'number' && fv % 1 !== 0) ? '0.001' : '1';
            var inputVal = typeof fv === 'boolean' ? '' : escHtml(String(fv));
            if (inputType === 'checkbox') {
              listHtml += '<div class="cfg-arr-card-field">' +
                '<span class="cfg-arr-card-fk">' + escHtml(fk) + '</span>' +
                '<div class="cfg-toggle' + (fv ? ' on' : '') + '" ' +
                  'data-arr-path="' + escHtml(path) + '" data-arr-idx="' + idx + '" data-field="' + escHtml(fk) + '" ' +
                  (blocked ? '' : 'onclick="onArrBoolToggle(this)"') + '></div>' +
              '</div>';
            } else {
              listHtml += '<div class="cfg-arr-card-field">' +
                '<span class="cfg-arr-card-fk">' + escHtml(fk) + '</span>' +
                '<input type="' + inputType + '"' + (inputType === 'number' ? ' step="' + step + '"' : '') +
                  ' class="cfg-arr-field-input' + (blocked ? ' locked' : '') + '" ' +
                  'data-arr-path="' + escHtml(path) + '" data-arr-idx="' + idx + '" data-field="' + escHtml(fk) + '" ' +
                  'value="' + inputVal + '" ' +
                  (blocked ? 'disabled' : 'onchange="onArrFieldChange(this)"') + '>' +
              '</div>';
            }
          }
        });
        listHtml += '</div></div>';
      });
      listHtml += '</div>';
    } else {
      listHtml = '<div class="cfg-arr-items">';
      val.forEach(function(item, idx) {
        var display = String(item);
        listHtml += '<div class="cfg-arr-item">' +
          '<span class="cfg-arr-item-text" title="' + escHtml(display) + '">' + escHtml(display) + '</span>' +
          (blocked ? '' : '<button class="cfg-arr-item-del" onclick="removeArrayItem(\'' + escHtml(path) + '\',' + idx + ')" title="删除">×</button>') +
          '</div>';
      });
      listHtml += '</div>';
    }

    var addRowHtml = '';
    if (!blocked) {
      if (hasObjectItems) {
        addRowHtml = '<div class="cfg-arr-add">' +
          '<button class="cfg-arr-add-btn" onclick="addObjectArrayItem(\'' + escHtml(path) + '\')" title="基于第一项的结构添加新项">+ 添加新项</button>' +
        '</div>';
      } else {
        addRowHtml = '<div class="cfg-arr-add">' +
          '<input type="text" class="cfg-arr-input" id="' + arrInputId + '" placeholder="输入后回车添加，逗号分隔可批量…" ' +
          'onkeydown="if(event.key===\'Enter\'||event.key===\',\'){event.preventDefault();addArrayItem(\'' + escHtml(path) + '\')}" ' +
          'onpaste="var p=\'' + escHtml(path) + '\';var el=this;setTimeout(function(){addArrayItem(p)},50)">' +
          '<button class="cfg-arr-add-btn" onclick="addArrayItem(\'' + escHtml(path) + '\')">+ 添加</button>' +
        '</div>';
      }
    }

    var panelBorder = isChanged ? '#eab308' : '#2a4a8a';
    return '<div class="cfg-arr-panel" id="' + arrPanelId + '" style="border-color:' + panelBorder + '">' +
      '<div class="cfg-arr-header">' +
        '<div class="cfg-arr-label">' +
          '<span class="cfg-arr-title">' + (doc ? escHtml(doc) : escHtml(key)) + '</span>' +
          countBadge + changedBadge +
          (blocked ? ' <span style="color:#64748b;font-size:0.7rem">🔒</span>' : '') +
        '</div>' +
        '<span class="cfg-key-secondary">' + escHtml(key) + '</span>' +
      '</div>' +
      '<div class="cfg-arr-body" id="' + arrListId + '">' + listHtml + '</div>' +
      addRowHtml +
    '</div>';
  }

  var lockIcon = blocked ? ' <span title="受保护参数" style="color:#64748b;font-size:0.7rem">🔒</span>' : '';
  var primaryLabel = doc ? escHtml(doc) : escHtml(key);
  var secondaryLabel = doc ? '<span class="cfg-key-secondary">' + escHtml(key) + lockIcon + '</span>' : lockIcon;

  return '<div class="cfg-row">' +
    '<div class="cfg-key">' +
      '<div class="cfg-key-name">' + primaryLabel + secondaryLabel + '</div>' +
    '</div>' +
    inputHtml +
  '</div>';
}

function toggleArrCard(headerEl) {
  var body = headerEl.nextElementSibling;
  var chevron = headerEl.querySelector('.cfg-arr-card-chevron');
  if (body.style.display === 'none') {
    body.style.display = '';
    if (chevron) chevron.textContent = '▾';
    headerEl.parentElement.classList.add('expanded');
  } else {
    body.style.display = 'none';
    if (chevron) chevron.textContent = '▸';
    headerEl.parentElement.classList.remove('expanded');
  }
}

function onArrFieldChange(el) {
  var arrPath = el.dataset.arrPath;
  var idx = parseInt(el.dataset.arrIdx, 10);
  var field = el.dataset.field;
  var current = JSON.parse(JSON.stringify(cfgEdits[arrPath] || cfgOriginal[arrPath]));
  if (!Array.isArray(current) || idx < 0 || idx >= current.length) return;

  var rawVal = el.value;
  var parts = field.split('.');
  var target = current[idx];
  for (var i = 0; i < parts.length - 1; i++) {
    if (target[parts[i]] === undefined) target[parts[i]] = {};
    target = target[parts[i]];
  }
  var lastKey = parts[parts.length - 1];
  var origFieldVal = target[lastKey];
  if (typeof origFieldVal === 'number') {
    var num = Number(rawVal);
    if (!isNaN(num)) target[lastKey] = num;
    else { el.style.borderColor = '#ef4444'; return; }
  } else {
    target[lastKey] = rawVal;
  }

  if (JSON.stringify(current) === JSON.stringify(cfgOriginal[arrPath])) {
    delete cfgEdits[arrPath];
  } else {
    cfgEdits[arrPath] = current;
  }
  el.classList.toggle('changed', cfgEdits[arrPath] !== undefined);
  var subPath = arrPath.slice(cfgCurrentSection.length + 1).split('.');
  var cfgTarget = cfgData;
  for (var i = 0; i < subPath.length - 1; i++) {
    if (cfgTarget[subPath[i]] !== undefined) cfgTarget = cfgTarget[subPath[i]];
  }
  cfgTarget[subPath[subPath.length - 1]] = current;
  updateSaveBar();
}

function onArrBoolToggle(el) {
  var arrPath = el.dataset.arrPath;
  var idx = parseInt(el.dataset.arrIdx, 10);
  var field = el.dataset.field;
  var current = JSON.parse(JSON.stringify(cfgEdits[arrPath] || cfgOriginal[arrPath]));
  if (!Array.isArray(current) || idx < 0 || idx >= current.length) return;

  var parts = field.split('.');
  var target = current[idx];
  for (var i = 0; i < parts.length - 1; i++) {
    if (target[parts[i]] === undefined) target[parts[i]] = {};
    target = target[parts[i]];
  }
  target[parts[parts.length - 1]] = !target[parts[parts.length - 1]];
  el.classList.toggle('on');

  if (JSON.stringify(current) === JSON.stringify(cfgOriginal[arrPath])) {
    delete cfgEdits[arrPath];
  } else {
    cfgEdits[arrPath] = current;
  }
  var subPath = arrPath.slice(cfgCurrentSection.length + 1).split('.');
  var cfgTarget = cfgData;
  for (var i = 0; i < subPath.length - 1; i++) {
    if (cfgTarget[subPath[i]] !== undefined) cfgTarget = cfgTarget[subPath[i]];
  }
  cfgTarget[subPath[subPath.length - 1]] = current;
  updateSaveBar();
}

function addObjectArrayItem(path) {
  var current = JSON.parse(JSON.stringify(cfgEdits[path] || cfgOriginal[path]));
  if (!Array.isArray(current)) return;
  var template;
  if (current.length > 0 && typeof current[0] === 'object') {
    template = JSON.parse(JSON.stringify(current[0]));
    function clearValues(obj) {
      for (var k in obj) {
        if (typeof obj[k] === 'number') obj[k] = 0;
        else if (typeof obj[k] === 'string') obj[k] = '';
        else if (typeof obj[k] === 'boolean') obj[k] = false;
        else if (obj[k] !== null && typeof obj[k] === 'object') clearValues(obj[k]);
      }
    }
    clearValues(template);
  } else {
    template = {};
  }
  current.push(template);
  cfgEdits[path] = current;
  var subPath = path.slice(cfgCurrentSection.length + 1).split('.');
  var target = cfgData;
  for (var i = 0; i < subPath.length - 1; i++) {
    if (target[subPath[i]] !== undefined) target = target[subPath[i]];
  }
  target[subPath[subPath.length - 1]] = current;
  updateSaveBar();
  buildConfigEditor(cfgData, cfgCurrentSection);
}

function onConfigChange(el) {
  var path = el.dataset.path;
  var origVal = cfgOriginal[path];
  var newVal;

  if (typeof origVal === 'number') {
    newVal = Number(el.value);
    if (isNaN(newVal)) { el.classList.add('cfg-input'); return; }
  } else if (Array.isArray(origVal)) {
    try { newVal = JSON.parse(el.value); } catch(e) { el.style.borderColor = '#ef4444'; return; }
  } else {
    newVal = el.value;
  }

  if (JSON.stringify(newVal) === JSON.stringify(origVal)) {
    delete cfgEdits[path];
    el.classList.remove('changed');
  } else {
    cfgEdits[path] = newVal;
    el.classList.add('changed');
  }
  updateSaveBar();
}

function toggleConfigBool(el) {
  var path = el.dataset.path;
  var isOn = el.classList.contains('on');
  var newVal = !isOn;
  el.classList.toggle('on');
  if (newVal === cfgOriginal[path]) {
    delete cfgEdits[path];
  } else {
    cfgEdits[path] = newVal;
  }
  updateSaveBar();
}

function removeArrayItem(path, idx) {
  var current = JSON.parse(JSON.stringify(cfgEdits[path] || cfgOriginal[path]));
  if (!Array.isArray(current) || idx < 0 || idx >= current.length) return;
  current.splice(idx, 1);
  if (JSON.stringify(current) === JSON.stringify(cfgOriginal[path])) {
    delete cfgEdits[path];
  } else {
    cfgEdits[path] = current;
  }
  updateSaveBar();
  var subPath = path.slice(cfgCurrentSection.length + 1).split('.');
  var target = cfgData;
  for (var i = 0; i < subPath.length - 1; i++) {
    if (target[subPath[i]] !== undefined) target = target[subPath[i]];
  }
  target[subPath[subPath.length - 1]] = current;
  buildConfigEditor(cfgData, cfgCurrentSection);
}

function addArrayItem(path) {
  var inputId = 'cfg-add-' + path.replace(/\./g, '-');
  var input = document.getElementById(inputId);
  if (!input || !input.value.trim()) return;
  var raw = input.value.trim();
  var items = raw.split(/[,\n]+/).map(function(s) { return s.trim(); }).filter(Boolean);
  if (items.length === 0) return;
  var current = JSON.parse(JSON.stringify(cfgEdits[path] || cfgOriginal[path]));
  if (!Array.isArray(current)) return;
  var added = 0;
  items.forEach(function(newItem) {
    if (/^-?\d+(\.\d+)?$/.test(newItem)) newItem = Number(newItem);
    if (typeof newItem === 'string' && current.indexOf(newItem) >= 0) return;
    if (typeof newItem === 'number' && current.indexOf(newItem) >= 0) return;
    current.push(newItem);
    added++;
  });
  if (added === 0) {
    input.style.borderColor = '#eab308';
    setTimeout(function() { input.style.borderColor = ''; }, 1500);
    return;
  }
  cfgEdits[path] = current;
  updateSaveBar();
  var subPath = path.slice(cfgCurrentSection.length + 1).split('.');
  var target = cfgData;
  for (var i = 0; i < subPath.length - 1; i++) {
    if (target[subPath[i]] !== undefined) target = target[subPath[i]];
  }
  target[subPath[subPath.length - 1]] = current;
  buildConfigEditor(cfgData, cfgCurrentSection);
}

function updateSaveBar() {
  var count = Object.keys(cfgEdits).length;
  var bar = document.getElementById('cfg-save-bar');
  var countEl = document.getElementById('cfg-change-count');
  bar.style.display = count > 0 ? 'flex' : 'none';
  countEl.textContent = count + ' 项修改待保存';
}

function resetConfigEdits() {
  cfgEdits = {};
  loadConfig();
}

function toggleEmergencyFreeze() {
  var currentFreeze = !!(cfgData['emergency_freeze']);
  var newVal = !currentFreeze;
  var actionText = newVal ? '⚡ 启动紧急冻结？' : '✅ 解除紧急冻结？';
  var warnText = newVal
    ? '启动后将立即停止所有新下单和补仓操作。\n现有持仓的止损/止盈继续运行。\n\n确认启动紧急冻结？'
    : '解除后系统将恢复正常下单。\n\n确认解除紧急冻结？';
  if (!confirm(actionText + '\n\n' + warnText)) return;
  fetch(BASE + '/api/emergency-freeze?key=' + KEY, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({freeze: newVal})
  }).then(function(r) { return r.json(); })
   .then(function(d) {
    if (d.status === 'ok') {
      var msg = newVal ? '⚡ 紧急冻结已启动！所有新下单已停止。' : '✅ 紧急冻结已解除，恢复正常运行。';
      alert(msg);
      loadConfig();
    } else {
      alert('操作失败: ' + (d.error || JSON.stringify(d)));
    }
  }).catch(function(e) { alert('请求失败: ' + e); });
}

function toggleDryRun() {
  var currentDry = cfgData['dry_run'];
  var newVal = !currentDry;
  var actionText = newVal ? '切换到 DRY RUN（模拟模式）' : '⚠️ 切换到 LIVE（实盘真实下单！）';
  var warnText = newVal ? '将停止真实下单，回到模拟模式。' : '⚠️ 警告：实盘模式将使用真实资金下单！\n\n请确认：\n1. .env 中私钥已正确配置\n2. 已充分测试 DRY RUN\n3. 余额充足\n\n此操作需要 TOTP 验证。';
  if (!confirm(actionText + '\n\n' + warnText)) return;
  if (!newVal && !totpSession) {
    alert('切换实盘需要先完成 TOTP 二次验证。请刷新页面并完成验证后重试。');
    return;
  }
  var headers = {'Content-Type':'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/toggle-dry-run?key=' + KEY, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify({dry_run: newVal})
  }).then(function(r) { return r.json(); })
   .then(function(d) {
    if (d.status === 'ok') {
      alert((newVal ? '✅ 已切换到模拟模式' : '🔴 已切换到实盘模式') + '\n下一轮生效。');
      loadConfig();
      refresh();
    } else {
      alert('切换失败: ' + (d.error || JSON.stringify(d)));
    }
  }).catch(function(e) { alert('请求失败: ' + e); });
}

function saveConfig() {
  var count = Object.keys(cfgEdits).length;
  if (count === 0) return;

  var btn = document.getElementById('cfg-save-btn');
  btn.disabled = true;
  btn.textContent = '保存中...';

  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;

  fetch(BASE + '/api/config?key=' + KEY, {
    method: 'PATCH',
    headers: headers,
    body: JSON.stringify({updates: cfgEdits})
  }).then(function(r) { return r.json().then(function(d) { return {status: r.status, data: d}; }); })
   .then(function(res) {
    var resultEl = document.getElementById('cfg-result');
    resultEl.style.display = 'block';

    if (res.status === 200 && res.data.status === 'ok') {
      var applied = res.data.applied || {};
      var appliedKeys = Object.keys(applied);
      var html = '<div class="text-xs p-3 rounded-lg" style="background:#22c55e15;border:1px solid #22c55e30">' +
        '<div class="text-green font-semibold mb-2">✅ 保存成功（' + appliedKeys.length + ' 项已更新）</div>';
      appliedKeys.forEach(function(k) {
        html += '<div class="text-gray-400"><code>' + escHtml(k) + '</code>: ' +
          '<span class="text-red">' + JSON.stringify(applied[k].old) + '</span> → ' +
          '<span class="text-green">' + JSON.stringify(applied[k].new) + '</span></div>';
      });
      if (res.data.errors && res.data.errors.length) {
        html += '<div class="text-yellow mt-2">⚠️ 部分失败: ' + res.data.errors.join('; ') + '</div>';
      }
      html += '<div class="text-gray-500 mt-1">备份: ' + escHtml(res.data.backup || '') + '</div>';
      html += '<div class="text-gray-500">ℹ️ 配置热加载将在下一轮自动生效</div></div>';
      resultEl.innerHTML = html;
      cfgEdits = {};
      setTimeout(function() { loadConfig(); }, 1000);
    } else {
      resultEl.innerHTML = '<div class="text-xs p-3 rounded-lg" style="background:#ef444415;border:1px solid #ef444430">' +
        '<div class="text-red font-semibold">❌ 保存失败</div>' +
        '<div class="text-gray-400">' + escHtml(JSON.stringify(res.data)) + '</div></div>';
    }

    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    setTimeout(function() { resultEl.style.display = 'none'; }, 8000);
  }).catch(function(e) {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    var resultEl = document.getElementById('cfg-result');
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div class="text-xs p-3 text-red">网络错误: ' + escHtml(String(e)) + '</div>';
  });
}

function toggleAccordion(id) {
  var el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

var ssCfgData = {}, ssCfgOriginal = {}, ssCfgEdits = {}, ssCfgDocs = {};

function loadGeneralConfig() {
  var statusEl = document.getElementById('ss-cfg-status');
  statusEl.textContent = '加载中...';
  api('/api/config?section=general').then(function(d) {
    var data = d.data || {};
    ssCfgDocs = data._doc_ || {};
    ssCfgData = {};
    Object.keys(data).forEach(function(k) { if (!k.startsWith('_')) ssCfgData[k] = data[k]; });
    ssCfgOriginal = {};
    ssCfgEdits = {};
    updateGeneralSaveBar();
    buildGeneralConfigEditor(ssCfgData);
    statusEl.textContent = '已加载 general';
    statusEl.className = 'text-xs text-green';
    setTimeout(function() { statusEl.className = 'text-xs text-gray-500'; }, 2000);
  }).catch(function(e) {
    statusEl.textContent = '加载失败';
    statusEl.className = 'text-xs text-red';
    document.getElementById('ss-cfg-editor').innerHTML = '<div class="text-red text-xs py-4">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

var GENERAL_HIDDEN_KEYS = ['db_path', 'dry_run'];
function buildGeneralConfigEditor(data) {
  var editor = document.getElementById('ss-cfg-editor');
  var html = '';
  var keys = Object.keys(data).sort().filter(function(k) { return GENERAL_HIDDEN_KEYS.indexOf(k) === -1; });
  keys.forEach(function(key) {
    var val = data[key];
    var fk = 'general.' + key;
    ssCfgOriginal[fk] = val;
    var blocked = CFG_BLOCKED.some(function(b) { return key.toLowerCase().indexOf(b) !== -1; });
    var doc = ssCfgDocs[key] || '';
    html += '<div class="cfg-row flex items-center gap-3 py-1 border-b border-gray-800">';
    html += '<div class="w-1/3 min-w-0">';
    html += '<span class="text-xs text-gray-400 truncate block" title="' + escHtml(fk) + '">' + escHtml(key) + '</span>';
    if (doc) html += '<span class="text-xs text-gray-600 block mt-0.5 leading-snug" style="font-size:0.65rem">' + escHtml(doc) + '</span>';
    html += '</div>';
    if (blocked) {
      html += '<span class="text-xs text-gray-600 w-2/3 font-mono">🔒 (受保护)</span>';
    } else if (typeof val === 'boolean') {
      html += '<label class="relative inline-flex items-center cursor-pointer">';
      html += '<input type="checkbox" class="sr-only peer" data-fk="' + escHtml(fk) + '" ' + (val ? 'checked' : '') + ' onchange="onGeneralCfgChange(this)">';
      html += '<div class="w-9 h-5 bg-gray-700 peer-checked:bg-accent rounded-full after:content-[\'\'] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full"></div>';
      html += '</label>';
    } else {
      html += '<input type="text" class="text-xs font-mono bg-gray-800 border border-gray-700 rounded px-2 py-1 text-white w-2/3" value="' + escHtml(String(val)) + '" data-fk="' + escHtml(fk) + '" oninput="onGeneralCfgChange(this)">';
    }
    html += '</div>';
  });
  if (keys.length === 0) html = '<div class="text-gray-500 text-xs py-4">无配置项</div>';
  editor.innerHTML = html;
}

function onGeneralCfgChange(el) {
  var fk = el.dataset.fk;
  var origVal = ssCfgOriginal[fk];
  var newVal;
  if (el.type === 'checkbox') {
    newVal = el.checked;
  } else {
    var raw = el.value.trim();
    if (raw === 'true') newVal = true;
    else if (raw === 'false') newVal = false;
    else if (raw !== '' && !isNaN(Number(raw))) newVal = Number(raw);
    else newVal = raw;
  }
  if (newVal === origVal || String(newVal) === String(origVal)) {
    delete ssCfgEdits[fk];
  } else {
    ssCfgEdits[fk] = newVal;
  }
  updateGeneralSaveBar();
}

function updateGeneralSaveBar() {
  var count = Object.keys(ssCfgEdits).length;
  var bar = document.getElementById('ss-cfg-save-bar');
  bar.style.display = count > 0 ? 'flex' : 'none';
  document.getElementById('ss-cfg-change-count').textContent = count + ' 项已修改';
}

function resetGeneralConfigEdits() {
  ssCfgEdits = {};
  updateGeneralSaveBar();
  buildGeneralConfigEditor(ssCfgData);
}

function saveGeneralConfig() {
  var count = Object.keys(ssCfgEdits).length;
  if (count === 0) return;
  var btn = document.getElementById('ss-cfg-save-btn');
  btn.disabled = true;
  btn.textContent = '保存中...';
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/config?key=' + KEY, {
    method: 'PATCH', headers: headers,
    body: JSON.stringify({updates: ssCfgEdits})
  }).then(function(r) { return r.json().then(function(d) { return {status: r.status, data: d}; }); })
   .then(function(res) {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    var resultEl = document.getElementById('ss-cfg-result');
    resultEl.style.display = 'block';
    if (res.status === 200 && res.data.status === 'ok') {
      resultEl.innerHTML = '<div class="text-xs p-3 text-green">✅ 已保存 ' + (res.data.applied_count || count) + ' 项</div>';
      ssCfgEdits = {};
      updateGeneralSaveBar();
      loadGeneralConfig();
    } else {
      resultEl.innerHTML = '<div class="text-xs p-3 text-red">❌ ' + escHtml(res.data.error || '保存失败') + '</div>';
    }
    setTimeout(function() { resultEl.style.display = 'none'; }, 4000);
  }).catch(function(e) {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    var resultEl = document.getElementById('ss-cfg-result');
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div class="text-xs p-3 text-red">网络错误: ' + escHtml(String(e)) + '</div>';
  });
}

var tgCfgData = {}, tgCfgOriginal = {}, tgCfgEdits = {}, tgCfgDocs = {};

function loadTelegramConfig() {
  var statusEl = document.getElementById('tg-cfg-status');
  statusEl.textContent = '加载中...';
  api('/api/config?section=telegram').then(function(d) {
    var data = d.data || {};
    tgCfgDocs = data._doc_ || {};
    tgCfgData = {};
    Object.keys(data).forEach(function(k) { if (!k.startsWith('_')) tgCfgData[k] = data[k]; });
    tgCfgOriginal = {};
    tgCfgEdits = {};
    updateTelegramSaveBar();
    buildTelegramConfigEditor(tgCfgData);
    statusEl.textContent = '已加载 telegram';
    statusEl.className = 'text-xs text-green';
    setTimeout(function() { statusEl.className = 'text-xs text-gray-500'; }, 2000);
  }).catch(function(e) {
    statusEl.textContent = '加载失败';
    statusEl.className = 'text-xs text-red';
    document.getElementById('tg-cfg-editor').innerHTML = '<div class="text-red text-xs py-4">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function buildTelegramConfigEditor(data) {
  var editor = document.getElementById('tg-cfg-editor');
  var html = '';
  var keys = Object.keys(data).sort();
  keys.forEach(function(key) {
    var val = data[key];
    var fk = 'telegram.' + key;
    tgCfgOriginal[fk] = val;
    var doc = tgCfgDocs[key] || '';
    html += '<div class="cfg-row flex items-center gap-3 py-1 border-b border-gray-800">';
    html += '<div class="w-1/3 min-w-0">';
    html += '<span class="text-xs text-gray-400 truncate block" title="' + escHtml(fk) + '">' + escHtml(key) + '</span>';
    if (doc) html += '<span class="text-xs text-gray-600 block mt-0.5 leading-snug" style="font-size:0.65rem">' + escHtml(doc) + '</span>';
    html += '</div>';
    if (typeof val === 'boolean') {
      html += '<label class="relative inline-flex items-center cursor-pointer">';
      html += '<input type="checkbox" class="sr-only peer" data-fk="' + escHtml(fk) + '" ' + (val ? 'checked' : '') + ' onchange="onTelegramCfgChange(this)">';
      html += '<div class="w-9 h-5 bg-gray-700 peer-checked:bg-accent rounded-full after:content-[\'\'] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full"></div>';
      html += '</label>';
    } else {
      html += '<input type="text" class="text-xs font-mono bg-gray-800 border border-gray-700 rounded px-2 py-1 text-white w-2/3" value="' + escHtml(String(val)) + '" data-fk="' + escHtml(fk) + '" oninput="onTelegramCfgChange(this)">';
    }
    html += '</div>';
  });
  if (keys.length === 0) html = '<div class="text-gray-500 text-xs py-4">无配置项</div>';
  editor.innerHTML = html;
}

function onTelegramCfgChange(el) {
  var fk = el.dataset.fk;
  var origVal = tgCfgOriginal[fk];
  var newVal;
  if (el.type === 'checkbox') {
    newVal = el.checked;
  } else {
    var raw = el.value.trim();
    if (raw === 'true') newVal = true;
    else if (raw === 'false') newVal = false;
    else if (raw !== '' && !isNaN(Number(raw))) newVal = Number(raw);
    else newVal = raw;
  }
  if (newVal === origVal || String(newVal) === String(origVal)) {
    delete tgCfgEdits[fk];
  } else {
    tgCfgEdits[fk] = newVal;
  }
  updateTelegramSaveBar();
}

function updateTelegramSaveBar() {
  var count = Object.keys(tgCfgEdits).length;
  var bar = document.getElementById('tg-cfg-save-bar');
  bar.style.display = count > 0 ? 'flex' : 'none';
  document.getElementById('tg-cfg-change-count').textContent = count + ' 项已修改';
}

function resetTelegramConfigEdits() {
  tgCfgEdits = {};
  updateTelegramSaveBar();
  buildTelegramConfigEditor(tgCfgData);
}

function saveTelegramConfig() {
  var count = Object.keys(tgCfgEdits).length;
  if (count === 0) return;
  var btn = document.getElementById('tg-cfg-save-btn');
  btn.disabled = true;
  btn.textContent = '保存中...';
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/config?key=' + KEY, {
    method: 'PATCH', headers: headers,
    body: JSON.stringify({updates: tgCfgEdits})
  }).then(function(r) { return r.json().then(function(d) { return {status: r.status, data: d}; }); })
   .then(function(res) {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    var resultEl = document.getElementById('tg-cfg-result');
    resultEl.style.display = 'block';
    if (res.status === 200 && res.data.status === 'ok') {
      resultEl.innerHTML = '<div class="text-xs p-3 text-green">✅ 已保存 ' + (res.data.applied_count || count) + ' 项</div>';
      tgCfgEdits = {};
      updateTelegramSaveBar();
      loadTelegramConfig();
    } else {
      resultEl.innerHTML = '<div class="text-xs p-3 text-red">❌ ' + escHtml(res.data.error || '保存失败') + '</div>';
    }
    setTimeout(function() { resultEl.style.display = 'none'; }, 4000);
  }).catch(function(e) {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
    var resultEl = document.getElementById('tg-cfg-result');
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div class="text-xs p-3 text-red">网络错误: ' + escHtml(String(e)) + '</div>';
  });
}

function loadWalletPanel() {
  var listEl = document.getElementById('wallet-list');
  if (!listEl) return;
  listEl.innerHTML = '<div class="text-gray-500 text-xs">加载中...</div>';
  api('/api/config?section=smart_money').then(function(d) {
    var wallets = (d.data && Array.isArray(d.data.manual_wallets)) ? d.data.manual_wallets : [];
    renderWalletPanel(wallets);
  }).catch(function(e) {
    if (listEl) listEl.innerHTML = '<div class="text-red text-xs">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function renderWalletPanel(wallets) {
  var listEl = document.getElementById('wallet-list');
  if (!listEl) return;
  if (!wallets || wallets.length === 0) {
    listEl.innerHTML = '<div class="text-yellow text-xs py-2">⚠️ 暂无追踪钱包。添加地址以启用聪明钱信号。</div>';
    return;
  }
  var html = '';
  wallets.forEach(function(addr, i) {
    html += '<div class="flex items-center gap-2 py-0.5">';
    html += '<span class="text-xs font-mono text-indigo-300 bg-gray-800 px-2 py-1 rounded flex-1 min-w-0 truncate" title="' + escHtml(addr) + '">' + escHtml(addr) + '</span>';
    html += '<button onclick="removeSmartMoneyWallet(' + i + ')" class="text-xs text-red-400 hover:text-red-300 transition flex-shrink-0" title="移除此地址">🗑️ 移除</button>';
    html += '</div>';
  });
  listEl.innerHTML = html;
}

function addSmartMoneyWallet() {
  var input = document.getElementById('wallet-new-input');
  if (!input) return;
  var addr = input.value.trim();
  if (!addr || !/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    showWalletResult('⚠️ 请输入有效的以太坊地址（0x + 40位十六进制）', 'text-yellow');
    return;
  }
  api('/api/config?section=smart_money').then(function(d) {
    var cfg = d.data || {};
    var wallets = (Array.isArray(cfg.manual_wallets) ? cfg.manual_wallets : []).slice();
    var addrLower = addr.toLowerCase();
    if (wallets.some(function(w) { return w.toLowerCase() === addrLower; })) {
      showWalletResult('⚠️ 该地址已在追踪列表中', 'text-yellow');
      return;
    }
    wallets.push(addr);
    var updates = {'smart_money.manual_wallets': wallets};
    var headers = {'Content-Type': 'application/json'};
    if (totpSession) headers['X-TOTP-Session'] = totpSession;
    return fetch(BASE + '/api/config?key=' + KEY, {
      method: 'PATCH', headers: headers,
      body: JSON.stringify({updates: updates})
    }).then(function(r) { return r.json(); }).then(function(res) {
      if (res.status === 'ok') {
        input.value = '';
        showWalletResult('✅ 已添加: ' + addr, 'text-green');
        loadWalletPanel();
      } else {
        showWalletResult('❌ ' + (res.error || '添加失败'), 'text-red');
      }
    });
  }).catch(function(e) {
    showWalletResult('❌ 网络错误: ' + String(e), 'text-red');
  });
}

function removeSmartMoneyWallet(index) {
  if (!confirm('确定移除该聪明钱地址？')) return;
  api('/api/config?section=smart_money').then(function(d) {
    var cfg = d.data || {};
    var wallets = (Array.isArray(cfg.manual_wallets) ? cfg.manual_wallets : []).slice();
    if (index < 0 || index >= wallets.length) return;
    var removed = wallets.splice(index, 1);
    var updates = {'smart_money.manual_wallets': wallets};
    var headers = {'Content-Type': 'application/json'};
    if (totpSession) headers['X-TOTP-Session'] = totpSession;
    return fetch(BASE + '/api/config?key=' + KEY, {
      method: 'PATCH', headers: headers,
      body: JSON.stringify({updates: updates})
    }).then(function(r) { return r.json(); }).then(function(res) {
      if (res.status === 'ok') {
        showWalletResult('✅ 已移除: ' + (removed[0] || ''), 'text-green');
        loadWalletPanel();
      } else {
        showWalletResult('❌ ' + (res.error || '移除失败'), 'text-red');
      }
    });
  }).catch(function(e) {
    showWalletResult('❌ 网络错误: ' + String(e), 'text-red');
  });
}

function showWalletResult(msg, cls) {
  var el = document.getElementById('wallet-result');
  if (!el) return;
  el.style.display = 'block';
  el.innerHTML = '<div class="text-xs p-2 ' + cls + '">' + msg + '</div>';
  setTimeout(function() { if (el) el.style.display = 'none'; }, 4000);
}

function loadChatIdWhitelist() {
  api('/api/config?section=telegram_bot').then(function(d) {
    var ids = (d.data && d.data.allowed_chat_ids) || [];
    renderChatIdWhitelist(ids);
  }).catch(function(e) {
    document.getElementById('chatid-whitelist-editor').innerHTML =
      '<div class="text-red text-xs">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function renderChatIdWhitelist(ids) {
  var el = document.getElementById('chatid-whitelist-editor');
  if (!ids || ids.length === 0) {
    el.innerHTML = '<div class="text-yellow text-xs py-2">⚠️ 白名单为空 — Bot 将使用环境变量 CHAT_ID 作为默认授权。建议添加 Chat ID 以增强安全性。</div>';
    return;
  }
  var html = '';
  ids.forEach(function(id, i) {
    html += '<div class="flex items-center gap-2 py-1">';
    html += '<span class="text-xs font-mono text-white bg-gray-800 px-2 py-1 rounded">' + escHtml(String(id)) + '</span>';
    html += '<button onclick="removeChatIdWhitelist(' + i + ')" class="text-xs text-red-400 hover:text-red-300 transition">🗑️ 移除</button>';
    html += '</div>';
  });
  el.innerHTML = html;
}

function addChatIdWhitelist() {
  var input = document.getElementById('chatid-new-input');
  var val = input.value.trim();
  if (!val || !/^-?\d+$/.test(val)) {
    showChatIdResult('⚠️ 请输入有效的 Chat ID（纯数字）', 'text-yellow');
    return;
  }
  var chatId = parseInt(val, 10);
  api('/api/config?section=telegram_bot').then(function(d) {
    var cfg = d.data || {};
    var ids = (cfg.allowed_chat_ids || []).slice();
    if (ids.indexOf(chatId) !== -1) {
      showChatIdResult('⚠️ 该 Chat ID 已在白名单中', 'text-yellow');
      return;
    }
    ids.push(chatId);
    var updates = {'telegram_bot.allowed_chat_ids': ids};
    var headers = {'Content-Type': 'application/json'};
    if (totpSession) headers['X-TOTP-Session'] = totpSession;
    return fetch(BASE + '/api/config?key=' + KEY, {
      method: 'PATCH', headers: headers,
      body: JSON.stringify({updates: updates})
    }).then(function(r) { return r.json(); }).then(function(res) {
      if (res.status === 'ok') {
        input.value = '';
        showChatIdResult('✅ 已添加 Chat ID: ' + chatId, 'text-green');
        loadChatIdWhitelist();
      } else {
        showChatIdResult('❌ ' + (res.error || '添加失败'), 'text-red');
      }
    });
  }).catch(function(e) {
    showChatIdResult('❌ 网络错误: ' + String(e), 'text-red');
  });
}

function removeChatIdWhitelist(index) {
  if (!confirm('确定移除此 Chat ID？')) return;
  api('/api/config?section=telegram_bot').then(function(d) {
    var cfg = d.data || {};
    var ids = (cfg.allowed_chat_ids || []).slice();
    if (index < 0 || index >= ids.length) return;
    ids.splice(index, 1);
    var updates = {'telegram_bot.allowed_chat_ids': ids};
    var headers = {'Content-Type': 'application/json'};
    if (totpSession) headers['X-TOTP-Session'] = totpSession;
    return fetch(BASE + '/api/config?key=' + KEY, {
      method: 'PATCH', headers: headers,
      body: JSON.stringify({updates: updates})
    }).then(function(r) { return r.json(); }).then(function(res) {
      if (res.status === 'ok') {
        showChatIdResult('✅ 已移除', 'text-green');
        loadChatIdWhitelist();
      } else {
        showChatIdResult('❌ ' + (res.error || '移除失败'), 'text-red');
      }
    });
  }).catch(function(e) {
    showChatIdResult('❌ 网络错误: ' + String(e), 'text-red');
  });
}

function showChatIdResult(msg, cls) {
  var el = document.getElementById('chatid-wl-result');
  el.style.display = 'block';
  el.innerHTML = '<div class="text-xs p-2 ' + cls + '">' + msg + '</div>';
  setTimeout(function() { el.style.display = 'none'; }, 4000);
}

function refreshSmartCapitalPreview() {
  var balanceEl = document.getElementById('sc-preview-balance');
  var resultEl = document.getElementById('sc-preview-result');
  if (!balanceEl || !resultEl) return;
  var balance = parseFloat(balanceEl.value) || 10;
  if (balance <= 0) { resultEl.textContent = '余额必须大于 0'; return; }
  resultEl.innerHTML = '<span class="text-gray-500">计算中...</span>';
  api('/api/smart_capital/preview?balance=' + encodeURIComponent(balance))
    .then(function(d) {
      var cats = d.categories || {};
      var CAT_LABELS = {weather:'天气 ⛅',sports:'体育 ⚽',crypto:'Crypto ₿','default':'默认'};
      var html = '<div class="mb-2 flex flex-wrap gap-3 items-center text-xs">';
      html += '<span class="text-yellow-300 font-semibold">当前 Tier: ' + escHtml(d.tier_name || '-') + '</span>';
      html += '<span class="text-gray-400">最大占比: ' + escHtml(d.max_bet_pct_pct || '-') + '</span>';
      html += '<span class="text-gray-400">硬顶: $' + (d.hard_max_usdc != null ? d.hard_max_usdc.toFixed(2) : '-') + '</span>';
      html += '</div>';
      html += '<div class="grid grid-cols-2 gap-2">';
      ['weather', 'sports', 'crypto', 'default'].forEach(function(cat) {
        var c = cats[cat] || {};
        var label = CAT_LABELS[cat] || cat;
        html += '<div class="bg-gray-800 rounded p-2 text-xs">';
        html += '<div class="text-gray-200 font-semibold mb-1">' + escHtml(label) + '</div>';
        html += '<div class="text-gray-400">乘数: ' + (c.multiplier != null ? c.multiplier.toFixed(2) : '-') + '</div>';
        html += '<div class="text-green-400 font-semibold">上限: $' + (c.max_usdc != null ? c.max_usdc.toFixed(4) : '-') + '</div>';
        html += '</div>';
      });
      html += '</div>';
      var pause = d.pause || {};
      if (pause.enabled) {
        var floorUsdc = pause.balance_floor_usdc || 0;
        var above = balance >= floorUsdc;
        html += '<div class="mt-2 text-xs ' + (above ? 'text-green-400' : 'text-red-400') + '">';
        html += above
          ? '✅ 余额 $' + balance.toFixed(2) + ' ≥ 地板 $' + floorUsdc.toFixed(2) + '，暂停保护未触发'
          : '⚠️ 余额 $' + balance.toFixed(2) + ' < 地板 $' + floorUsdc.toFixed(2) + '，将暂停所有新投注！';
        html += '</div>';
      }
      resultEl.innerHTML = html;
    })
    .catch(function(e) {
      resultEl.innerHTML = '<div class="text-red-400 text-xs">预览加载失败: ' + escHtml(String(e)) + '</div>';
    });
}

var envKeysList = [];

function loadEnvKeys() {
  var container = document.getElementById('env-keys-container');
  container.innerHTML = '<div class="text-gray-500 text-xs py-4">加载中...</div>';
  api('/api/env/list').then(function(d) {
    envKeysList = d.keys || [];
    renderEnvKeys();
  }).catch(function(e) {
    container.innerHTML = '<div class="text-red text-xs py-4">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function renderEnvKeys() {
  var container = document.getElementById('env-keys-container');
  if (envKeysList.length === 0) {
    container.innerHTML = '<div class="text-gray-500 text-xs py-4">无密钥配置</div>';
    return;
  }
  var html = '<table class="w-full text-xs"><thead><tr class="text-gray-500 border-b border-gray-700">';
  html += '<th class="text-left py-2 px-2">Key</th><th class="text-left py-2 px-2">值 (掩码)</th><th class="text-left py-2 px-2">状态</th><th class="py-2 px-2">操作</th>';
  html += '</tr></thead><tbody>';
  envKeysList.forEach(function(item) {
    var keyName = item.key;
    var masked = item.masked_value || '(未设置)';
    var hasVal = item.has_value;
    var ro = item.readonly;
    html += '<tr class="border-b border-gray-800 hover:bg-gray-800/50">';
    html += '<td class="py-2 px-2 font-mono text-accent">' + escHtml(keyName) + '</td>';
    html += '<td class="py-2 px-2 font-mono text-gray-400">' + escHtml(masked) + '</td>';
    html += '<td class="py-2 px-2">';
    if (ro) html += '<span class="text-yellow">🔒 只读</span>';
    else if (hasVal) html += '<span class="text-green">✅ 已配置</span>';
    else html += '<span class="text-gray-500">⬜ 未设置</span>';
    html += '</td>';
    html += '<td class="py-2 px-2 text-center">';
    if (!ro) {
      html += '<button onclick="openEnvModal(\'' + escHtml(keyName) + '\',\'' + escHtml(masked) + '\')" class="text-accent hover:underline">✏️ 编辑</button>';
    } else {
      html += '<span class="text-gray-600">-</span>';
    }
    html += '</td></tr>';
  });
  html += '</tbody></table>';
  container.innerHTML = html;
}

function openEnvModal(key, masked) {
  document.getElementById('env-edit-key').textContent = key;
  document.getElementById('env-edit-masked').textContent = masked;
  document.getElementById('env-edit-value').value = '';
  document.getElementById('env-edit-status').textContent = '';
  var modal = document.getElementById('env-edit-modal');
  modal.style.display = 'flex';
  document.getElementById('env-edit-value').focus();
}

function closeEnvModal() {
  document.getElementById('env-edit-modal').style.display = 'none';
}

function saveEnvKey() {
  var key = document.getElementById('env-edit-key').textContent;
  var value = document.getElementById('env-edit-value').value;
  if (!value) {
    document.getElementById('env-edit-status').textContent = '请输入新值';
    document.getElementById('env-edit-status').style.color = '#ef4444';
    return;
  }
  var btn = document.getElementById('env-save-btn');
  var statusEl = document.getElementById('env-edit-status');
  btn.disabled = true;
  btn.textContent = '保存中...';
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/env/update?key=' + KEY, {
    method: 'POST', headers: headers,
    body: JSON.stringify({key: key, value: value})
  }).then(function(r) { return r.json().then(function(d) { return {status: r.status, data: d}; }); })
   .then(function(res) {
    btn.disabled = false;
    btn.textContent = '💾 保存';
    if (res.status === 200 && res.data.status === 'ok') {
      statusEl.textContent = '✅ 已保存';
      statusEl.style.color = '#22c55e';
      setTimeout(function() { closeEnvModal(); loadEnvKeys(); }, 1000);
    } else {
      statusEl.textContent = '❌ ' + (res.data.error || '保存失败');
      statusEl.style.color = '#ef4444';
    }
  }).catch(function(e) {
    btn.disabled = false;
    btn.textContent = '💾 保存';
    statusEl.textContent = '❌ 网络错误';
    statusEl.style.color = '#ef4444';
  });
}

var _pmCityData = [];

function loadPMCityMonitor() {
  var container = document.getElementById('pm-city-monitor-container');
  var summary = document.getElementById('pm-city-monitor-summary');
  var syncBtn = document.getElementById('pm-sync-all-btn');
  var syncResult = document.getElementById('pm-sync-result');
  container.innerHTML = '<div class="text-gray-500 text-xs py-4">正在从 Polymarket 拉取数据...</div>';
  summary.innerHTML = '';
  if (syncResult) syncResult.style.display = 'none';
  if (syncBtn) syncBtn.style.display = 'none';
  api('/api/pm-city-monitor').then(function(d) {
    if (d.error) { container.innerHTML = '<div class="text-red text-xs py-4">❌ ' + escHtml(d.error) + '</div>'; return; }
    var cities = d.cities || [];
    _pmCityData = cities;
    var mc = d.mismatch_count || 0;
    var hasNotInSystem = cities.some(function(c) { return c.status === 'not_in_system'; });
    if (syncBtn) syncBtn.style.display = (mc > 0 || hasNotInSystem) ? 'inline' : 'none';
    var sHtml = '<div class="flex flex-wrap gap-4 text-xs">';
    sHtml += '<span class="text-gray-400">PM 城市: <span class="text-white font-semibold">' + (d.pm_count || 0) + '</span></span>';
    sHtml += '<span class="text-gray-400">系统城市: <span class="text-white font-semibold">' + (d.sys_count || 0) + '</span></span>';
    if (mc > 0) {
      sHtml += '<span class="text-red-400 font-semibold">⚠️ ' + mc + ' 项不一致</span>';
    } else {
      sHtml += '<span class="text-green-400 font-semibold">✅ 全部一致</span>';
    }
    sHtml += '<span class="text-gray-600">' + (d.ts || '').substring(0, 19) + ' UTC</span>';
    sHtml += '</div>';
    summary.innerHTML = sHtml;
    var SRC_LABEL = {wunderground: 'WU', noaa: 'NOAA', hko: 'HKO'};
    var html = '<table class="w-full text-xs"><thead><tr class="text-gray-500 border-b border-gray-700">';
    html += '<th class="text-left py-2 px-2">城市</th>';
    html += '<th class="text-left py-2 px-2">PM 结算源</th>';
    html += '<th class="text-left py-2 px-2">PM 站点</th>';
    html += '<th class="text-left py-2 px-2">系统站点</th>';
    html += '<th class="text-left py-2 px-2">状态</th>';
    html += '<th class="text-left py-2 px-2">链接</th>';
    html += '<th class="text-left py-2 px-2">操作</th>';
    html += '</tr></thead><tbody>';
    cities.sort(function(a, b) {
      var aM = a.status !== 'match' ? 0 : 1;
      var bM = b.status !== 'match' ? 0 : 1;
      if (aM !== bM) return aM - bM;
      return a.slug.localeCompare(b.slug);
    });
    cities.forEach(function(c, idx) {
      var statusLabel = '', statusClass = '';
      if (c.status === 'match') { statusLabel = '✅ 一致'; statusClass = 'text-green-400'; }
      else if (c.status === 'mismatch') { statusLabel = '⚠️ 不一致'; statusClass = 'text-red-400 font-semibold'; }
      else if (c.status === 'not_in_pm') { statusLabel = '🚫 PM无数据'; statusClass = 'text-yellow-400'; }
      else if (c.status === 'not_in_system') { statusLabel = '🆕 系统缺失'; statusClass = 'text-blue-400'; }
      var isMismatch = c.status === 'mismatch';
      var rowClass = c.status !== 'match' ? ' style="background:#ef444410"' : '';
      var pmSrc = SRC_LABEL[c.pm_source] || c.pm_source || '-';
      html += '<tr class="border-b border-gray-800 hover:bg-gray-800/50"' + rowClass + '>';
      html += '<td class="py-2 px-2 text-white">' + escHtml(c.name) + '</td>';
      html += '<td class="py-2 px-2 font-mono text-gray-400">' + escHtml(pmSrc) + '</td>';
      html += '<td class="py-2 px-2 font-mono ' + (isMismatch ? 'text-red-400 font-semibold' : 'text-gray-400') + '">' + escHtml(c.pm_station || '-') + '</td>';
      html += '<td class="py-2 px-2 font-mono ' + (isMismatch ? 'text-yellow-400' : 'text-gray-400') + '">' + escHtml(c.sys_station || '-') + '</td>';
      html += '<td class="py-2 px-2 ' + statusClass + '">' + statusLabel;
      if (c.diff_fields && c.diff_fields.length) {
        html += '<span class="text-gray-500 ml-1">(' + c.diff_fields.join(',') + ')</span>';
      }
      html += '</td>';
      html += '<td class="py-2 px-2">';
      if (c.pm_url) html += '<a href="' + escHtml(c.pm_url) + '" target="_blank" class="text-accent hover:underline">' + escHtml(pmSrc) + '↗</a>';
      html += '</td>';
      html += '<td class="py-2 px-2">';
      if (isMismatch) {
        html += '<button onclick="syncPMCity(\'' + escHtml(c.slug) + '\')" class="text-xs text-yellow-300 hover:text-yellow-100 font-semibold">🔄 同步</button>';
      }
      html += '</td></tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  }).catch(function(e) {
    container.innerHTML = '<div class="text-red text-xs py-4">请求失败: ' + escHtml(String(e)) + '</div>';
  });
}

function syncPMCity(slug) {
  var city = _pmCityData.find(function(c) { return c.slug === slug; });
  if (!city) return;
  var action = city.status === 'not_in_system' ? 'add' : 'update';
  _doSyncPMCities([{slug: city.slug, name: city.name, pm_source: city.pm_source, pm_station: city.pm_station, pm_url: city.pm_url, action: action}]);
}

function syncAllPMCities() {
  var toUpdate = _pmCityData.filter(function(c) { return c.status === 'mismatch'; });
  var toAdd = _pmCityData.filter(function(c) { return c.status === 'not_in_system'; });
  if (toUpdate.length === 0 && toAdd.length === 0) { return; }
  var msg = '将同步 ' + toUpdate.length + ' 个城市更新';
  if (toAdd.length > 0) msg += ' + ' + toAdd.length + ' 个新城市添加';
  if (!confirm(msg + '，是否继续？')) return;
  var payload = toUpdate.map(function(c) { return {slug: c.slug, pm_source: c.pm_source, pm_station: c.pm_station, pm_url: c.pm_url, action: 'update'}; });
  toAdd.forEach(function(c) { payload.push({slug: c.slug, name: c.name, pm_source: c.pm_source, pm_station: c.pm_station, pm_url: c.pm_url, action: 'add'}); });
  _doSyncPMCities(payload);
}

function _doSyncPMCities(cities) {
  var resultEl = document.getElementById('pm-sync-result');
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="text-xs p-2 text-gray-400">同步中...</div>';
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/pm-city-sync?key=' + KEY, {
    method: 'POST', headers: headers,
    body: JSON.stringify({cities: cities})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === 'ok') {
      var html = '<div class="text-xs p-3 rounded-lg" style="background:#22c55e15;border:1px solid #22c55e30">';
      html += '<div class="text-green-400 font-semibold mb-1">✅ 同步成功 — ' + (d.updated || 0) + ' 个更新';
      if (d.added) html += ' + ' + d.added + ' 个新增';
      html += '</div>';
      if (d.details) {
        d.details.forEach(function(u) {
          html += '<div class="text-gray-400"><code>' + escHtml(u.slug) + '</code>: ';
          var parts = [];
          for (var k in u.changes) { parts.push(k + ': ' + u.changes[k][0] + ' → ' + u.changes[k][1]); }
          html += escHtml(parts.join(', '));
          html += '</div>';
        });
      }
      if (d.added_details) {
        d.added_details.forEach(function(a) {
          html += '<div class="text-blue-400">🆕 <code>' + escHtml(a.slug) + '</code>: ' + escHtml(a.name) + ' @ ' + a.geo.lat + ',' + a.geo.lon + '</div>';
        });
      }
      if (d.backup) html += '<div class="text-gray-500 mt-1">备份: ' + escHtml(d.backup) + '</div>';
      html += '</div>';
      resultEl.innerHTML = html;
      setTimeout(function() { loadPMCityMonitor(); }, 2000);
    } else {
      resultEl.innerHTML = '<div class="text-xs p-3 text-red-400">❌ ' + escHtml(d.error || d.message || '同步失败') + '</div>';
    }
  }).catch(function(e) {
    resultEl.innerHTML = '<div class="text-xs p-3 text-red-400">请求失败: ' + escHtml(String(e)) + '</div>';
  });
}

window._sysUsersPage = 1;
window._sysUsersTotalPages = 1;

function _systemPost(urlPath, payload) {
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  return fetch(BASE + urlPath + '?key=' + KEY, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify(payload || {})
  }).then(function(r) {
    return r.json().then(function(d) {
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      return d;
    });
  });
}

function loadSystemUsers(page) {
  page = Number(page || 1);
  if (page < 1) page = 1;
  window._sysUsersPage = page;

  var qEl = document.getElementById('sys-users-q');
  var roleEl = document.getElementById('sys-users-role');
  var perEl = document.getElementById('sys-users-per-page');
  var container = document.getElementById('sys-users-container');
  var summary = document.getElementById('sys-users-summary');
  if (!container || !summary) return;

  var q = qEl ? qEl.value.trim() : '';
  var role = roleEl ? roleEl.value : '';
  var perPage = perEl ? Number(perEl.value || 50) : 50;

  container.innerHTML = '<div class="text-gray-500 py-3">加载中...</div>';
  var url = '/api/system/users/list?page=' + page + '&per_page=' + perPage;
  if (q) url += '&q=' + encodeURIComponent(q);
  if (role) url += '&role=' + encodeURIComponent(role);

  api(url).then(function(d) {
    var users = d.users || [];
    window._sysUsersTotalPages = Number(d.total_pages || 1);
    summary.textContent = '共 ' + (d.total || 0) + ' 个用户';

    var html = '<div style="overflow-x:auto"><table class="w-full text-xs">';
    html += '<thead><tr class="text-gray-500 border-b border-gray-700">';
    html += '<th class="text-left py-2 px-2">钱包</th>';
    html += '<th class="text-left py-2 px-2">角色</th>';
    html += '<th class="text-left py-2 px-2">PM钱包</th>';
    html += '<th class="text-left py-2 px-2">会话</th>';
    html += '<th class="text-left py-2 px-2">运行控制</th>';
    html += '<th class="text-left py-2 px-2">最后登录</th>';
    html += '<th class="text-left py-2 px-2">操作</th>';
    html += '</tr></thead><tbody>';

    if (!users.length) {
      html += '<tr><td colspan="7" class="py-4 text-gray-500 text-center">无匹配用户</td></tr>';
    } else {
      users.forEach(function(u) {
        var wallet = escHtml(u.wallet_address || '');
        var roleTxt = escHtml(u.role || 'manual_user');
        var pmWallet = escHtml(u.polymarket_wallet || '-');
        var sess = Number(u.active_sessions || 0);
        var freezeAuto = !!u.freeze_auto;
        var autoEnabled = !!u.auto_trading_enabled;
        var lastLogin = escHtml((u.last_login_at || '').replace('T', ' ').slice(0, 19) || '-');

        html += '<tr class="border-b border-gray-800 hover:bg-gray-800/50">';
        html += '<td class="py-2 px-2 font-mono text-accent">' + wallet + '</td>';
        html += '<td class="py-2 px-2">' + roleTxt + '</td>';
        html += '<td class="py-2 px-2 font-mono text-gray-300">' + pmWallet + '</td>';
        html += '<td class="py-2 px-2">' + sess + '</td>';
        html += '<td class="py-2 px-2">'
          + '<span class="' + (freezeAuto ? 'text-red-400' : 'text-green-400') + '">' + (freezeAuto ? '已冻结' : '未冻结') + '</span>'
          + ' / '
          + '<span class="' + (autoEnabled ? 'text-green-400' : 'text-yellow-400') + '">' + (autoEnabled ? '自动交易开' : '自动交易关') + '</span>'
          + '</td>';
        html += '<td class="py-2 px-2 text-gray-400">' + lastLogin + '</td>';
        html += '<td class="py-2 px-2 whitespace-nowrap">'
          + '<button class="text-xs text-blue-400 hover:underline mr-2" onclick="sysToggleFreeze(\'' + wallet + '\',' + (!freezeAuto) + ')">' + (freezeAuto ? '解冻' : '冻结') + '</button>'
          + '<button class="text-xs text-cyan-400 hover:underline mr-2" onclick="sysToggleAutoTrading(\'' + wallet + '\',' + (!autoEnabled) + ')">' + (autoEnabled ? '停自动' : '开自动') + '</button>'
          + '<button class="text-xs text-yellow-400 hover:underline mr-2" onclick="sysRevokeSessions(\'' + wallet + '\')">踢下线</button>'
          + '<button class="text-xs text-purple-400 hover:underline mr-2" onclick="sysUnbindPm(\'' + wallet + '\')">解绑PM</button>'
          + '<button class="text-xs text-red-400 hover:underline" onclick="sysDeleteUser(\'' + wallet + '\')">删除</button>'
          + '</td>';
        html += '</tr>';
      });
    }

    html += '</tbody></table></div>';
    container.innerHTML = html;

    var pageEl = document.getElementById('sys-users-page');
    var prev = document.getElementById('sys-users-prev');
    var next = document.getElementById('sys-users-next');
    if (pageEl) pageEl.textContent = window._sysUsersPage + ' / ' + window._sysUsersTotalPages;
    if (prev) prev.disabled = window._sysUsersPage <= 1;
    if (next) next.disabled = window._sysUsersPage >= window._sysUsersTotalPages;
  }).catch(function(e) {
    container.innerHTML = '<div class="text-red py-3">加载失败: ' + escHtml(String(e)) + '</div>';
  });
}

function sysToggleFreeze(wallet, freezeValue) {
  _systemPost('/api/system/users/runtime', {
    wallet_address: wallet,
    freeze_auto: !!freezeValue
  }).then(function() {
    loadSystemUsers(window._sysUsersPage || 1);
  }).catch(function(e) {
    alert('操作失败: ' + String(e));
  });
}

function sysToggleAutoTrading(wallet, enabled) {
  _systemPost('/api/system/users/runtime', {
    wallet_address: wallet,
    auto_trading_enabled: !!enabled
  }).then(function() {
    loadSystemUsers(window._sysUsersPage || 1);
  }).catch(function(e) {
    alert('操作失败: ' + String(e));
  });
}

function sysRevokeSessions(wallet) {
  if (!confirm('确认踢下线该用户的全部会话？')) return;
  _systemPost('/api/system/users/revoke-sessions', {
    wallet_address: wallet
  }).then(function(d) {
    alert('已撤销会话: ' + (d.revoked_sessions || 0));
    loadSystemUsers(window._sysUsersPage || 1);
  }).catch(function(e) {
    alert('操作失败: ' + String(e));
  });
}

function sysUnbindPm(wallet) {
  if (!confirm('确认解绑该用户 PM 钱包？')) return;
  _systemPost('/api/system/users/unbind-pm-wallet', {
    wallet_address: wallet
  }).then(function() {
    loadSystemUsers(window._sysUsersPage || 1);
  }).catch(function(e) {
    alert('操作失败: ' + String(e));
  });
}

function sysDeleteUser(wallet) {
  var ans = prompt('危险操作：输入 DELETE 确认删除用户 ' + wallet);
  if (ans !== 'DELETE') return;
  _systemPost('/api/system/users/delete', {
    wallet_address: wallet,
    confirm: 'DELETE'
  }).then(function() {
    loadSystemUsers(window._sysUsersPage || 1);
  }).catch(function(e) {
    alert('删除失败: ' + String(e));
  });
}

function syncPMCity(slug) {
  var city = _pmCityData.find(function(c) { return c.slug === slug; });
  if (!city) return;
  var action = city.status === 'not_in_system' ? 'add' : 'update';
  _doSyncPMCities([{slug: city.slug, name: city.name, pm_source: city.pm_source, pm_station: city.pm_station, pm_url: city.pm_url, action: action}]);
}

function syncAllPMCities() {
  var toUpdate = _pmCityData.filter(function(c) { return c.status === 'mismatch'; });
  var toAdd = _pmCityData.filter(function(c) { return c.status === 'not_in_system'; });
  if (toUpdate.length === 0 && toAdd.length === 0) { return; }
  var msg = '将同步 ' + toUpdate.length + ' 个城市更新';
  if (toAdd.length > 0) msg += ' + ' + toAdd.length + ' 个新城市添加';
  if (!confirm(msg + '，是否继续？')) return;
  var payload = toUpdate.map(function(c) { return {slug: c.slug, pm_source: c.pm_source, pm_station: c.pm_station, pm_url: c.pm_url, action: 'update'}; });
  toAdd.forEach(function(c) { payload.push({slug: c.slug, name: c.name, pm_source: c.pm_source, pm_station: c.pm_station, pm_url: c.pm_url, action: 'add'}); });
  _doSyncPMCities(payload);
}

function _doSyncPMCities(cities) {
  var resultEl = document.getElementById('pm-sync-result');
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="text-xs p-2 text-gray-400">同步中...</div>';
  var headers = {'Content-Type': 'application/json'};
  if (totpSession) headers['X-TOTP-Session'] = totpSession;
  fetch(BASE + '/api/pm-city-sync?key=' + KEY, {
    method: 'POST', headers: headers,
    body: JSON.stringify({cities: cities})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === 'ok') {
      var html = '<div class="text-xs p-3 rounded-lg" style="background:#22c55e15;border:1px solid #22c55e30">';
      html += '<div class="text-green-400 font-semibold mb-1">✅ 同步成功 — ' + (d.updated || 0) + ' 个更新';
      if (d.added) html += ' + ' + d.added + ' 个新增';
      html += '</div>';
      if (d.details) {
        d.details.forEach(function(u) {
          html += '<div class="text-gray-400"><code>' + escHtml(u.slug) + '</code>: ';
          var parts = [];
          for (var k in u.changes) { parts.push(k + ': ' + u.changes[k][0] + ' → ' + u.changes[k][1]); }
          html += escHtml(parts.join(', '));
          html += '</div>';
        });
      }
      if (d.added_details) {
        d.added_details.forEach(function(a) {
          html += '<div class="text-blue-400">🆕 <code>' + escHtml(a.slug) + '</code>: ' + escHtml(a.name) + ' @ ' + a.geo.lat + ',' + a.geo.lon + '</div>';
        });
      }
      if (d.backup) html += '<div class="text-gray-500 mt-1">备份: ' + escHtml(d.backup) + '</div>';
      html += '</div>';
      resultEl.innerHTML = html;
      setTimeout(function() { loadPMCityMonitor(); }, 2000);
    } else {
      resultEl.innerHTML = '<div class="text-xs p-3 text-red-400">❌ ' + escHtml(d.error || d.message || '同步失败') + '</div>';
    }
  }).catch(function(e) {
    resultEl.innerHTML = '<div class="text-xs p-3 text-red-400">请求失败: ' + escHtml(String(e)) + '</div>';
  });
}

var _origSwitchTab = switchTab;
switchTab = function(tabName) {
  _origSwitchTab(tabName);
  if (tabName === 'config') {
    buildCategorySections(cfgCurrentCategory);
    if (Object.keys(cfgData).length === 0) {
      loadConfig();
    }
  }
  if (tabName === 'system-settings') {
    if (envKeysList.length === 0) loadEnvKeys();
  }
};

function buildWeatherCitiesEditor(data) {
  var editor = document.getElementById('cfg-editor');
  if (!editor) return;

  var cities = [];
  if (Array.isArray(data)) {
    cities = data;
  } else if (data && Array.isArray(data.cities)) {
    cities = data.cities;
  }

  function _esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  function _renderTable() {
    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">';
    html += '<h3 style="font-size:0.9rem;color:#e2e8f0;font-weight:600">🌆 天气城市管理</h3>';
    html += '<button onclick="wcAddModal()" style="padding:5px 14px;font-size:0.78rem;background:#6366f1;color:#fff;border:none;border-radius:6px;cursor:pointer">+ 添加城市</button>';
    html += '</div>';

    if (cities.length === 0) {
      html += '<div style="color:#6b7280;font-size:0.8rem;padding:16px">暂无城市配置</div>';
    } else {
      html += '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:0.78rem">';
      html += '<thead><tr style="color:#9ca3af;border-bottom:1px solid #374151">';
      html += '<th style="text-align:left;padding:6px 8px">城市</th>';
      html += '<th style="text-align:left;padding:6px 8px">Slug</th>';
      html += '<th style="text-align:left;padding:6px 8px">ICAO</th>';
      html += '<th style="text-align:left;padding:6px 8px">时区</th>';
      html += '<th style="text-align:center;padding:6px 8px">启用</th>';
      html += '<th style="text-align:center;padding:6px 8px">操作</th>';
      html += '</tr></thead><tbody>';
      cities.forEach(function(c, idx) {
        var rowStyle = idx % 2 === 0 ? '' : 'background:rgba(255,255,255,0.02)';
        html += '<tr style="' + rowStyle + ';border-bottom:1px solid #1f2937">';
        html += '<td style="padding:6px 8px;color:#e2e8f0;font-weight:500">' + _esc(c.name) + '</td>';
        html += '<td style="padding:6px 8px;color:#9ca3af;font-family:monospace">' + _esc(c.slug) + '</td>';
        html += '<td style="padding:6px 8px;color:#9ca3af;font-family:monospace">' + _esc(c.icao||'-') + '</td>';
        html += '<td style="padding:6px 8px;color:#6b7280;font-size:0.7rem">' + _esc(c.timezone||'-') + '</td>';
        var toggleStyle = c.enabled ? 'background:#10b981' : 'background:#374151';
        html += '<td style="padding:6px 8px;text-align:center">';
        html += '<button onclick="wcToggle(' + JSON.stringify(_esc(c.slug)) + ',' + (!c.enabled) + ')" ';
        html += 'style="width:36px;height:18px;border-radius:9px;border:none;cursor:pointer;' + toggleStyle + ';transition:background 0.2s" ';
        html += 'title="' + (c.enabled ? '点击停用' : '点击启用') + '"></button></td>';
        html += '<td style="padding:6px 8px;text-align:center;white-space:nowrap">';
        html += '<button onclick="wcEditModal(' + idx + ')" style="padding:3px 8px;font-size:0.7rem;background:#1d4ed8;color:#fff;border:none;border-radius:4px;cursor:pointer;margin-right:4px">编辑</button>';
        html += '<button onclick="wcDelete(' + JSON.stringify(_esc(c.slug)) + ')" style="padding:3px 8px;font-size:0.7rem;background:#dc2626;color:#fff;border:none;border-radius:4px;cursor:pointer">删除</button>';
        html += '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
    editor.innerHTML = html;
  }

  _renderTable();

  window._wcCities = cities;
  window._wcRender = _renderTable;

  api('/api/weather-cities').then(function(d) {
    var list = (d && Array.isArray(d.cities)) ? d.cities : [];
    if (list.length) {
      cities = list;
      window._wcCities = cities;
      _renderTable();
    }
  }).catch(function() {});
}

function _wcReloadCities() {
  return api('/api/weather-cities').then(function(d) {
    window._wcCities = (d && Array.isArray(d.cities)) ? d.cities : [];
    if (window._wcRender) window._wcRender();
  });
}

function wcToggle(slug, newEnabled) {
  var headers = {'Content-Type':'application/json'};
  if (window.totpSession) headers['X-TOTP-Session'] = window.totpSession;
  fetch(BASE + '/api/weather-cities/' + encodeURIComponent(slug) + '/toggle?key=' + KEY, {
    method: 'PATCH', headers: headers,
    body: JSON.stringify({enabled: newEnabled})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === 'ok' || d.enabled !== undefined) {
      _wcReloadCities();
    } else { alert('切换失败: ' + (d.error||JSON.stringify(d))); }
  }).catch(function(e){ alert('网络错误: '+e); });
}

function wcDelete(slug) {
  if (!confirm('确认删除城市 ' + slug + '？此操作不可撤销。')) return;
  var headers = {};
  if (window.totpSession) headers['X-TOTP-Session'] = window.totpSession;
  fetch(BASE + '/api/weather-cities/' + encodeURIComponent(slug) + '?key=' + KEY, {
    method: 'DELETE', headers: headers
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === 'ok') {
      _wcReloadCities();
    } else { alert('删除失败: ' + (d.error||JSON.stringify(d))); }
  }).catch(function(e){ alert('网络错误: '+e); });
}

function _wcModalHtml(title, city, onSubmitFn) {
  var c = city || {name:'',slug:'',lat:'',lon:'',icao:'',timezone:'Asia/Shanghai',utc_offset:8,enabled:true,settlement_source:'wunderground',settlement_url:'',noaa_site:''};
  var html = '<div id="wc-modal-overlay" onclick="if(event.target===this)wcCloseModal()" style="position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;display:flex;align-items:center;justify-content:center">';
  html += '<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">';
  html += '<h3 style="font-size:0.95rem;color:#e2e8f0;font-weight:600;margin-bottom:16px">' + title + '</h3>';
  var fields = [
    {id:'wc-name',label:'城市名称',val:c.name,type:'text',required:true},
    {id:'wc-slug',label:'Slug (英文小写)',val:c.slug,type:'text',required:true,disabled:!!city},
    {id:'wc-lat', label:'纬度 (lat)',val:c.lat,type:'number'},
    {id:'wc-lon', label:'经度 (lon)',val:c.lon,type:'number'},
    {id:'wc-icao',label:'ICAO 代码',val:c.icao,type:'text'},
    {id:'wc-tz',  label:'时区',val:c.timezone,type:'text'},
    {id:'wc-utc', label:'UTC 偏移 (小时)',val:c.utc_offset,type:'number'},
    {id:'wc-src', label:'结算来源',val:c.settlement_source,type:'text'},
    {id:'wc-url', label:'结算 URL',val:c.settlement_url,type:'text'},
    {id:'wc-noaa',label:'NOAA 站点',val:c.noaa_site,type:'text'},
  ];
  fields.forEach(function(f) {
    html += '<div style="margin-bottom:10px">';
    html += '<label style="display:block;font-size:0.75rem;color:#9ca3af;margin-bottom:3px">' + f.label + (f.required?' *':'') + '</label>';
    html += '<input id="' + f.id + '" type="' + f.type + '" value="' + escHtml(String(f.val||'')) + '" ';
    if (f.disabled) html += 'readonly ';
    html += 'style="width:100%;box-sizing:border-box;padding:6px 10px;font-size:0.82rem;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0" />';
    html += '</div>';
  });
  html += '<div style="margin-bottom:10px"><label style="display:flex;align-items:center;gap:8px;font-size:0.78rem;color:#9ca3af;cursor:pointer">';
  html += '<input type="checkbox" id="wc-enabled" ' + (c.enabled?'checked':'') + ' style="width:14px;height:14px;accent-color:#6366f1"> 启用此城市</label></div>';
  html += '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">';
  html += '<button onclick="wcCloseModal()" style="padding:7px 16px;font-size:0.8rem;background:#374151;color:#9ca3af;border:none;border-radius:6px;cursor:pointer">取消</button>';
  html += '<button onclick="' + onSubmitFn + '()" style="padding:7px 16px;font-size:0.8rem;background:#6366f1;color:#fff;border:none;border-radius:6px;cursor:pointer">保存</button>';
  html += '</div></div></div>';
  return html;
}

function wcAddModal() {
  document.body.insertAdjacentHTML('beforeend', _wcModalHtml('+ 添加新城市', null, 'wcSubmitAdd'));
}

function wcEditModal(idx) {
  var c = (window._wcCities||[])[idx];
  if (!c) return;
  document.body.insertAdjacentHTML('beforeend', _wcModalHtml('✏️ 编辑城市: ' + c.name, c, 'wcSubmitEdit'));
  window._wcEditSlug = c.slug;
}

function wcCloseModal() {
  var el = document.getElementById('wc-modal-overlay');
  if (el) el.remove();
}

function _wcCollectForm() {
  return {
    name: document.getElementById('wc-name').value.trim(),
    slug: document.getElementById('wc-slug').value.trim(),
    lat: parseFloat(document.getElementById('wc-lat').value)||0,
    lon: parseFloat(document.getElementById('wc-lon').value)||0,
    icao: document.getElementById('wc-icao').value.trim(),
    timezone: document.getElementById('wc-tz').value.trim(),
    utc_offset: parseFloat(document.getElementById('wc-utc').value)||0,
    enabled: document.getElementById('wc-enabled').checked,
    settlement_source: document.getElementById('wc-src').value.trim() || 'wunderground',
    settlement_url: document.getElementById('wc-url').value.trim(),
    noaa_site: document.getElementById('wc-noaa').value.trim().toUpperCase(),
  };
}

function wcSubmitAdd() {
  var body = _wcCollectForm();
  if (!body.name || !body.slug) { alert('城市名称和 Slug 必填'); return; }
  var headers = {'Content-Type':'application/json'};
  if (window.totpSession) headers['X-TOTP-Session'] = window.totpSession;
  fetch(BASE + '/api/weather-cities?key=' + KEY, {
    method:'POST', headers:headers, body:JSON.stringify(body)
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d.status==='ok'||d.city) {
      wcCloseModal();
      _wcReloadCities();
    } else { alert('添加失败: '+(d.error||JSON.stringify(d))); }
  }).catch(function(e){ alert('网络错误: '+e); });
}

function wcSubmitEdit() {
  var body = _wcCollectForm();
  var slug = window._wcEditSlug;
  if (!slug) return;
  var headers = {'Content-Type':'application/json'};
  if (window.totpSession) headers['X-TOTP-Session'] = window.totpSession;
  fetch(BASE + '/api/weather-cities/' + encodeURIComponent(slug) + '?key=' + KEY, {
    method:'PUT', headers:headers, body:JSON.stringify(body)
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d.status==='ok'||d.city) {
      wcCloseModal();
      _wcReloadCities();
    } else { alert('保存失败: '+(d.error||JSON.stringify(d))); }
  }).catch(function(e){ alert('网络错误: '+e); });
}

if (window.STRATEGY_EMBED) {
} else {
  checkTotp();
  setInterval(refresh, REFRESH_INTERVAL);
}

function toggleParamDoc(docId) {
  var el = document.getElementById(docId);
  if (!el) return;
  el.classList.toggle('expanded');
}
