function dashboardApp() {
  return {
    summary: {},
    risk: {
      paused: false,
      reason: ""
    },
    trades: [],
    loading: false,
    page: 1,
    pageSize: 20,
    totalCount: 0,
    totalPages: 1,
    filters: {
      status: "",
      category: "",
      search: ""
    },
    async init() {
      await Promise.all([ this.loadSummary(), this.loadRisk(), this.loadTrades() ]);
      this.$nextTick(() => {
        if (window.lucide) lucide.createIcons();
      });
      this.checkPmWalletBanner();
      setInterval(() => this.refresh(), 3e4);
    },
    async checkPmWalletBanner() {
      try {
        const me = await vJson("/api/user/me");
        if (me && me.authenticated && !me.is_admin && !me.polymarket_wallet && !me.pm_wallet_address) {
          var banner = document.getElementById("pm-wallet-banner");
          if (banner) banner.style.display = "";
        }
      } catch (e) {}
    },
    async refresh() {
      await Promise.all([ this.loadSummary(), this.loadRisk(), this.loadTrades() ]);
    },
    async loadSummary() {
      const d = await vJson("/api/summary");
      if (!d) return;
      this.summary = {
        nlv: d.nlv ?? d.balance ?? 0,
        balance: d.balance ?? 0,
        deployed: d.deployed ?? 0,
        total_pnl: d.total_pnl ?? 0,
        realized_pnl: d.realized_pnl ?? 0,
        unrealized_pnl: d.unrealized_pnl ?? 0,
        win_rate: d.win_rate ?? 0,
        roi: d.roi ?? 0,
        open_positions: d.open_positions ?? d.open_bets ?? 0
      };
    },
    async loadRisk() {
      const d = await vJson("/api/risk/status");
      if (!d) return;
      this.risk = {
        paused: d.paused ?? false,
        reason: d.reason ?? ""
      };
    },
    async loadTrades() {
      this.loading = true;
      try {
        const params = new URLSearchParams({
          page: this.page,
          page_size: this.pageSize
        });
        if (this.filters.status) params.set("status", this.filters.status);
        if (this.filters.category) params.set("category", this.filters.category);
        if (this.filters.search) params.set("search", this.filters.search);
        const d = await vJson(`/api/user/pm/bets?${params.toString()}`);
        const items = d?.items || [];
        const total = d?.total_count ?? items.length;
        this.trades = items.map(t => ({
          id: t.id,
          question: t.question,
          market_slug: t.market_slug,
          category: t.category,
          outcome: t.outcome,
          status: this.normalizeStatus(t.status),
          avg_price: this.asNum(t.avg_price),
          exit_price: t.exit_price,
          shares: this.asNum(t.shares),
          cost: this.asNum(t.cost),
          pnl: this.asNum(t.pnl),
          created_at: t.created_at
        }));
        this.totalCount = total;
        this.totalPages = Math.max(1, Math.ceil(total / this.pageSize));
        if (this.page > this.totalPages) {
          this.page = this.totalPages;
          await this.loadTrades();
        }
      } catch (e) {
        this.trades = [];
        this.totalCount = 0;
        this.totalPages = 1;
      } finally {
        this.loading = false;
      }
    },
    normalizeStatus(status) {
      const s = String(status || "").toUpperCase();
      if (s === "CLOSED_WIN") return "WON";
      if (s === "CLOSED_LOSS") return "LOST";
      if (s === "CASHED_OUT") return "SOLD";
      return s || "-";
    },
    asNum(v) {
      const n = Number(v);
      return Number.isFinite(n) ? n : 0;
    },
    pageNumbers() {
      const pages = [];
      const start = Math.max(1, this.page - 2);
      const end = Math.min(this.totalPages, this.page + 2);
      for (let p = start; p <= end; p++) pages.push(p);
      return pages;
    },
    async resumeRisk() {
      const resp = await vFetch("/api/risk/resume", {
        method: "POST"
      });
      if (resp && resp.ok) {
        showToast(_t("dashboard.toast.riskResumed", "Risk control resumed"), "success");
        this.risk.paused = false;
      }
    },
    fmt$(v, digits = 2) {
      const n = Number(v || 0);
      return `$${n.toFixed(digits)}`;
    },
    fmtNum(v, digits = 1) {
      const n = Number(v || 0);
      return n.toFixed(digits);
    },
    fmtPnL(v) {
      const n = Number(v || 0);
      return `${n >= 0 ? "+" : ""}$${n.toFixed(2)}`;
    },
    fmtPct(v) {
      const n = Number(v || 0);
      return `${(n * 100).toFixed(1)}%`;
    },
    pnlClass(v) {
      return Number(v || 0) >= 0 ? "text-success" : "text-danger";
    },
    fmtDate(dt) {
      if (!dt) return "-";
      try {
        return new Date(dt).toLocaleString("zh-CN", {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit"
        });
      } catch {
        return dt;
      }
    }
  };
}