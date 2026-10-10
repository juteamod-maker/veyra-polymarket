function tradesApp() {
  return {
    trades: [],
    stats: {},
    loading: true,
    page: 1,
    pageSize: 25,
    totalCount: 0,
    totalPages: 1,
    filters: {
      status: "",
      category: "",
      outcome: "",
      search: ""
    },
    detailBet: null,
    async init() {
      await this.loadTrades();
      this.$nextTick(() => {
        if (window.lucide) lucide.createIcons();
      });
    },
    async loadTrades() {
      this.loading = true;
      const params = new URLSearchParams;
      params.set("per_page", this.pageSize);
      params.set("page", this.page);
      const statusMap = {
        OPEN: "open",
        WON: "win",
        LOST: "loss",
        SOLD: "manual_exit",
        RESOLVED: "resolved"
      };
      if (this.filters.status) params.set("status", statusMap[this.filters.status] || this.filters.status.toLowerCase());
      if (this.filters.category) params.set("category", this.filters.category);
      if (this.filters.outcome) params.set("outcome", this.filters.outcome);
      if (this.filters.search) params.set("market_id", this.filters.search);
      const d = await vJson(`/api/user/pm/bets?${params}`);
      this.loading = false;
      if (d) {
        const bets = d.bets || d || [];
        this.trades = bets.map(b => ({
          id: b.id || b.market_id || b.condition_id,
          question: b.question || b.market_slug || "",
          market_slug: b.market_slug || "",
          category: b.category || "",
          outcome: b.outcome || "",
          status: (b.status || "").toUpperCase(),
          avg_price: b.avg_price || b.actual_avg_price || 0,
          exit_price: b.exit_price || b.sell_price || null,
          shares: b.shares || 0,
          cost: b.cost || b.amount || 0,
          pnl: b.realized_pnl || b.pnl || 0,
          source: b.source || b.signal_source || "bot",
          exit_reason: b.exit_reason || "",
          confidence: b.confidence || 0,
          created_at: b.created_at || "",
          resolved_at: b.resolved_at || ""
        }));
        this.totalCount = d.total || d.count || bets.length;
        this.totalPages = Math.max(1, Math.ceil(this.totalCount / this.pageSize));
        this.computeStats(bets);
      }
    },
    computeStats(bets) {
      const resolved = bets.filter(b => b.status !== "OPEN");
      const wins = resolved.filter(b => parseFloat(b.realized_pnl || b.pnl || 0) > 0);
      const totalPnl = resolved.reduce((s, b) => s + parseFloat(b.realized_pnl || b.pnl || 0), 0);
      this.stats = {
        total: this.totalCount,
        realized_pnl: totalPnl,
        win_rate: resolved.length > 0 ? wins.length / resolved.length * 100 : 0,
        avg_hold_time: "-"
      };
    },
    showDetail(t) {
      this.detailBet = t;
      this.$nextTick(() => {
        if (window.lucide) lucide.createIcons();
      });
    },
    pageNumbers() {
      const pages = [];
      const start = Math.max(1, this.page - 2);
      const end = Math.min(this.totalPages, this.page + 2);
      for (let i = start; i <= end; i++) pages.push(i);
      return pages;
    }
  };
}