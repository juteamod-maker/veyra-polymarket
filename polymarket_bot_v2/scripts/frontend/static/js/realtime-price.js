(function() {
  if (window.VyRealtimePrice) return;
  const POLL_INTERVAL_MS = 1500;
  const STALE_AFTER_MS = 5e3;
  const PRICE_BATCH_SIZE = 40;
  const PRICE_BATCH_CONCURRENCY = 2;
  const state = {
    subscriptions: new Map,
    counter: 0,
    timer: null,
    lastSnapshot: {
      ts: 0,
      stale: false,
      prices: {}
    },
    inflight: false,
    paused: false
  };
  function _activeTokens() {
    const set = new Set;
    state.subscriptions.forEach(sub => sub.tokens.forEach(t => set.add(t)));
    return Array.from(set);
  }
  function _chunkTokens(tokens, size) {
    const out = [];
    for (let i = 0; i < tokens.length; i += size) {
      out.push(tokens.slice(i, i + size));
    }
    return out;
  }
  async function _fetchPriceBatch(tokens) {
    const url = "/api/pm/prices/live?token_ids=" + encodeURIComponent(tokens.join(","));
    const resp = await fetch(url, {
      credentials: "same-origin"
    });
    const json = await resp.json();
    return {
      ts: json && json.ts ? json.ts * 1e3 : Date.now(),
      stale: !!(json && json.stale),
      prices: json && json.prices || {}
    };
  }
  async function _pollOnce() {
    if (state.inflight) return;
    const tokens = _activeTokens();
    if (!tokens.length) return;
    state.inflight = true;
    try {
      const chunks = _chunkTokens(tokens, PRICE_BATCH_SIZE);
      const prices = {};
      let ts = 0;
      let stale = false;
      for (let i = 0; i < chunks.length; i += PRICE_BATCH_CONCURRENCY) {
        const slice = chunks.slice(i, i + PRICE_BATCH_CONCURRENCY);
        const results = await Promise.allSettled(slice.map(_fetchPriceBatch));
        results.forEach(res => {
          if (res.status !== "fulfilled") {
            stale = true;
            return;
          }
          const part = res.value;
          ts = Math.max(ts, part.ts || 0);
          stale = stale || !!part.stale;
          Object.assign(prices, part.prices || {});
        });
      }
      if (!ts) ts = Date.now();
      Object.keys(prices).forEach(k => {
        prices[k] = Object.assign({}, prices[k], {
          stale: stale
        });
      });
      state.lastSnapshot = {
        ts: ts,
        stale: stale,
        prices: prices
      };
    } catch (e) {
      const old = state.lastSnapshot;
      const stalePrices = {};
      Object.keys(old.prices || {}).forEach(k => {
        stalePrices[k] = Object.assign({}, old.prices[k], {
          stale: true
        });
      });
      state.lastSnapshot = {
        ts: old.ts,
        stale: true,
        prices: stalePrices,
        error: String(e)
      };
    } finally {
      state.inflight = false;
    }
    _broadcast();
  }
  function _broadcast() {
    const snap = state.lastSnapshot;
    state.subscriptions.forEach(sub => {
      try {
        const subPrices = {};
        sub.tokens.forEach(t => {
          if (snap.prices[t]) subPrices[t] = snap.prices[t];
        });
        sub.callback({
          ts: snap.ts,
          stale: snap.stale || snap.ts > 0 && Date.now() - snap.ts > STALE_AFTER_MS,
          prices: subPrices
        });
      } catch (e) {
        console.warn("[VyRealtimePrice] subscriber callback error:", e);
      }
    });
  }
  function _ensureTimer() {
    if (state.timer || state.paused) return;
    if (!state.subscriptions.size) return;
    _pollOnce();
    state.timer = setInterval(_pollOnce, POLL_INTERVAL_MS);
  }
  function _stopTimer() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }
  function subscribe(tokens, callback) {
    if (!Array.isArray(tokens) || typeof callback !== "function") {
      throw new Error("VyRealtimePrice.subscribe(tokens[], callback)");
    }
    state.counter += 1;
    const id = state.counter;
    state.subscriptions.set(id, {
      tokens: new Set(tokens.filter(Boolean).map(String)),
      callback: callback
    });
    _ensureTimer();
    if (state.lastSnapshot.ts > 0) _broadcast();
    return function unsubscribe() {
      state.subscriptions.delete(id);
      if (!state.subscriptions.size) _stopTimer();
    };
  }
  function updateTokens(unsubFn, newTokens) {
    return null;
  }
  function getSnapshot() {
    return state.lastSnapshot;
  }
  document.addEventListener("visibilitychange", function() {
    if (document.hidden) {
      state.paused = true;
      _stopTimer();
    } else {
      state.paused = false;
      _ensureTimer();
    }
  });
  window.VyRealtimePrice = {
    subscribe: subscribe,
    getSnapshot: getSnapshot,
    _state: state
  };
})();