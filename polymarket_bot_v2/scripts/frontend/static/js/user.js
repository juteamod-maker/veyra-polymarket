function userApp() {
  return {
    tab: "wallet",
    user: {},
    wallet: {
      pm_bound: false,
      pm_address: "",
      balance: 0
    },
    metrics: {},
    points: {},
    referral: {},
    fees: {
      history: []
    },
    runtime: {
      auto_bet: false,
      copy_trade: false,
      stop_loss: true
    },
    settings: {
      telegram_notify: true,
      trade_notify: true,
      language: "zh-CN",
      timezone: "UTC"
    },
    bindAddress: "",
    async init() {
      await Promise.all([ this.loadUser(), this.loadWallet(), this.loadMetrics(), this.loadPoints(), this.loadReferral(), this.loadFees(), this.loadRuntime() ]);
      this.$nextTick(() => {
        if (window.lucide) lucide.createIcons();
      });
    },
    async loadUser() {
      const d = await vJson("/api/user/me");
      if (d && !d.error) {
        this.user = {
          display_name: d.display_name || d.username || "",
          email: d.email || "",
          wallet_address: d.wallet_address || "",
          wallet_short: d.wallet_address ? d.wallet_address.slice(0, 6) + "..." + d.wallet_address.slice(-4) : "",
          role: d.role || "user",
          is_active: d.is_active !== false,
          created_at: d.created_at || ""
        };
      }
    },
    async loadWallet() {
      const d = await vJson("/api/user/pm-wallet/status");
      if (d && !d.error) {
        this.wallet = {
          pm_bound: d.bound ?? d.pm_wallet_bound ?? false,
          pm_address: d.address ?? d.pm_wallet_address ?? "",
          balance: d.balance ?? 0
        };
      }
    },
    async loadMetrics() {
      const d = await vJson("/api/user/pm/metrics");
      if (d && !d.error) {
        this.metrics = {
          total_pnl: d.total_pnl ?? 0,
          roi: d.roi ?? 0,
          open_positions: d.open_positions ?? 0,
          deployed: d.deployed ?? 0
        };
      }
    },
    async loadPoints() {
      const d = await vJson("/api/user/points");
      if (d && !d.error) {
        this.points = {
          balance: d.balance ?? d.points ?? 0,
          tier: d.tier ?? d.level ?? "Bronze"
        };
      }
    },
    async loadReferral() {
      const d = await vJson("/api/user/referral");
      if (d && !d.error) {
        this.referral = {
          count: d.referral_count ?? d.count ?? 0,
          earnings: d.earnings ?? 0,
          code: d.referral_code ?? d.code ?? ""
        };
      }
    },
    async loadFees() {
      const d = await vJson("/api/user/fees");
      if (d && !d.error) {
        this.fees = {
          total: d.total_fees ?? d.total ?? 0,
          this_month: d.month_fees ?? d.this_month ?? 0,
          rate: d.fee_rate ?? d.rate ?? 0,
          history: []
        };
      }
      const h = await vJson("/api/user/fees/history?limit=20");
      if (h && Array.isArray(h.history || h)) {
        this.fees.history = (h.history || h).slice(0, 20);
      }
    },
    async loadRuntime() {
      const d = await vJson("/api/user/runtime-control");
      if (d && !d.error) {
        this.runtime = {
          auto_bet: d.auto_bet ?? false,
          copy_trade: d.copy_trade ?? false,
          stop_loss: d.stop_loss ?? true
        };
      }
    },
    async toggleRuntime(key) {
      this.runtime[key] = !this.runtime[key];
      const resp = await vFetch("/api/user/runtime-control", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(this.runtime)
      });
      if (resp && resp.ok) {
        const d = await resp.json().catch(() => null);
        if (d) {
          this.runtime.auto_bet = !!d.auto_bet;
          this.runtime.copy_trade = !!d.copy_trade;
          this.runtime.stop_loss = !!d.stop_loss;
        }
        showToast(`${key} ` + _t("user.toast.saved", "saved"), "success");
      } else {
        this.runtime[key] = !this.runtime[key];
        showToast(_t("user.toast.opFailed", "Operation failed"), "error");
      }
    },
    async bindWallet() {
      showToast(_t("user.toast.manualBindOff", "Manual PM wallet binding is disabled. Please log in via Privy to re-authorize."), "warning");
      window.location.href = "/Index";
    },
    copyRef() {
      const link = "https://veyra.trade/?ref=" + (this.referral.code || "");
      navigator.clipboard.writeText(link).then(() => showToast(_t("user.toast.referCopied", "Referral link copied"), "success"), () => showToast(_t("user.toast.copyFailed", "Copy failed"), "error"));
    }
  };
}