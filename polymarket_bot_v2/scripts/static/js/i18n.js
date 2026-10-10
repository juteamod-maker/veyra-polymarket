
(function (global) {
  'use strict';

  var STORAGE_KEY = 'vp_lang';
  var DEFAULT_LANG = 'zh-CN';

  var _packs = {

    'zh-CN': {
      'nav.overview':     '概览',
      'nav.betslist':     '持仓',
      'nav.weathermap':   '天气',
      'nav.polymarket':   '机会',
      'nav.copytraders':  '跟投',
      'nav.analysis':     '分析',
      'nav.ordermonitor': '追踪',
      'nav.strategy':     '配置',
      'nav.manage':       '管理',

      'brand.name':       'Veyra.trade',

      'status.checking':  '检查中...',
      'status.running':   '运行中',
      'status.stopped':   '已停止',
      'status.error':     '连接失败',
      'status.dry_run':   'DRY RUN',
      'status.live':      'LIVE',
      'status.frozen':    '冻结',
      'status.editing':   '编辑中·刷新已暂停',

      'common.loading':   '加载中...',
      'common.error':     '加载失败',
      'common.refresh':   '刷新',
      'common.save':      '保存',
      'common.cancel':    '取消',
      'common.confirm':   '确认',
      'common.search':    '搜索...',
      'common.all':       '全部',
      'common.yes':       'YES',
      'common.no':        'NO',

      'page.overview':    '总览',
      'page.betslist':    '投注记录',
      'page.copytraders': '跟投管理',
      'page.analysis':    '数据分析',
      'page.strategy':    '策略配置',
      'page.weathermap':  '天气看板',
      'page.system':      '系统设置',
      'page.security':    '安全管理',
      'page.logs':        '系统日志',
    },

    'en-US': {
      'nav.overview':     'Overview',
      'nav.betslist':     'Positions',
      'nav.weathermap':   'Weather',
      'nav.polymarket':   'Opportunities',
      'nav.copytraders':  'Copytraders',
      'nav.analysis':     'Analysis',
      'nav.ordermonitor': 'Tracking',
      'nav.strategy':     'Config',
      'nav.manage':       'Admin',

      'brand.name':       'Veyra.trade',

      'status.checking':  'Checking...',
      'status.running':   'Running',
      'status.stopped':   'Stopped',
      'status.error':     'Conn Failed',
      'status.dry_run':   'DRY RUN',
      'status.live':      'LIVE',
      'status.frozen':    'Frozen',
      'status.editing':   'Editing · Refresh Paused',

      'common.loading':   'Loading...',
      'common.error':     'Load failed',
      'common.refresh':   'Refresh',
      'common.save':      'Save',
      'common.cancel':    'Cancel',
      'common.confirm':   'Confirm',
      'common.search':    'Search...',
      'common.all':       'All',
      'common.yes':       'YES',
      'common.no':        'NO',

      'page.overview':    'Overview',
      'page.betslist':    'Bet List',
      'page.copytraders': 'Copy Traders',
      'page.analysis':    'Analysis',
      'page.strategy':    'Strategy Config',
      'page.weathermap':  'Weather Map',
      'page.system':      'System Settings',
      'page.security':    'Security',
      'page.logs':        'System Logs',
    }
  };

  var _currentLang = (function () {
    try {
      return localStorage.getItem(STORAGE_KEY) || DEFAULT_LANG;
    } catch (e) {
      return DEFAULT_LANG;
    }
  })();

  var _callbacks = [];

  var i18n = {
    t: function (key, fallback) {
      var pack = _packs[_currentLang] || _packs[DEFAULT_LANG];
      if (pack && pack[key] !== undefined) return pack[key];
      var defPack = _packs[DEFAULT_LANG];
      if (defPack && defPack[key] !== undefined) return defPack[key];
      return fallback !== undefined ? fallback : key;
    },

    setLang: function (lang) {
      if (!_packs[lang]) {
        console.warn('[i18n] Unknown language:', lang);
        return false;
      }
      _currentLang = lang;
      try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
      _callbacks.forEach(function (fn) { try { fn(lang); } catch (e) {} });
      return true;
    },

    getLang: function () { return _currentLang; },

    getAvailableLangs: function () {
      return Object.keys(_packs).map(function (code) {
        var names = { 'zh-CN': '中文', 'en-US': 'English' };
        return { code: code, name: names[code] || code };
      });
    },

    onLangChange: function (fn) {
      if (typeof fn === 'function') _callbacks.push(fn);
    },

    extend: function (lang, entries) {
      if (!_packs[lang]) _packs[lang] = {};
      Object.keys(entries).forEach(function (k) { _packs[lang][k] = entries[k]; });
    }
  };

  global.i18n = i18n;
})(window);
