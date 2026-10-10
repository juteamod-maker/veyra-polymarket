-- ============================================================
-- VagusPoly V2 — PostgreSQL Schema 初始化脚本
-- 等效于 src/utils/db.py 中的 init_db() + 全部 migrations
-- 运行: psql -d polymarket_v2 -f scripts/init_pg_schema.sql
-- ============================================================

-- ── 核心数据表 ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scans (
    id          SERIAL PRIMARY KEY,
    ts          TEXT    NOT NULL,
    market_id   TEXT    NOT NULL,
    question    TEXT,
    score       REAL,
    tier        INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id          SERIAL PRIMARY KEY,
    ts          TEXT    NOT NULL,
    market_id   TEXT    NOT NULL,
    outcome     TEXT,
    ev          REAL,
    confidence  REAL,
    size_usdc   REAL,
    model       TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    ts          TEXT    NOT NULL,
    market_id   TEXT    NOT NULL,
    outcome     TEXT,
    size_usdc   REAL,
    price       REAL,
    tx_hash     TEXT,
    status      TEXT
);

CREATE TABLE IF NOT EXISTS balance_log (
    id          SERIAL PRIMARY KEY,
    ts          TEXT    NOT NULL,
    usdc_balance REAL
);

CREATE TABLE IF NOT EXISTS news_cache (
    cache_key   TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    ts          REAL NOT NULL
);

-- ── 自我进化系统 ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calibration_data (
    id              SERIAL PRIMARY KEY,
    ts              REAL    NOT NULL,
    category        TEXT    NOT NULL,
    bin_index       INTEGER NOT NULL,
    ai_probability  REAL    NOT NULL,
    actual_outcome  INTEGER NOT NULL,
    model_name      TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calibration_category ON calibration_data(category);

CREATE TABLE IF NOT EXISTS model_performance (
    id              SERIAL PRIMARY KEY,
    ts              REAL    NOT NULL,
    model_name      TEXT    NOT NULL,
    category        TEXT    NOT NULL DEFAULT '',
    predicted_prob  REAL    NOT NULL,
    actual_outcome  INTEGER NOT NULL,
    squared_error   REAL    NOT NULL,
    abs_error       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_perf_model ON model_performance(model_name);

CREATE TABLE IF NOT EXISTS evolution_log (
    id              SERIAL PRIMARY KEY,
    ts              REAL    NOT NULL,
    param_name      TEXT    NOT NULL,
    old_value       REAL    NOT NULL,
    new_value       REAL    NOT NULL,
    reason          TEXT    NOT NULL,
    data_points     INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 0.0
);

-- ── 投注单主表 ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS paper_bets (
    id                  SERIAL PRIMARY KEY,
    bet_id              TEXT    UNIQUE NOT NULL,
    ts_entered          REAL    NOT NULL,
    market_id           TEXT    NOT NULL,
    question            TEXT,
    category            TEXT    DEFAULT 'weather',
    outcome             TEXT,
    confidence          REAL,
    ai_probability      REAL,
    market_price        REAL,
    ev                  REAL,
    size_usdc           REAL,
    sim_balance_before  REAL,
    sim_balance_after   REAL,
    market_end_date     TEXT,
    weather_context     TEXT,
    ai_models           TEXT,
    cycle_id            INTEGER,
    resolved_at         REAL,
    resolved_outcome    TEXT,
    is_win              INTEGER,
    payout              REAL,
    pnl                 REAL,
    exit_price          REAL,
    exit_type           TEXT    DEFAULT NULL,
    -- Migration v2
    bet_bucket          TEXT    DEFAULT 'standard',
    market_slug         TEXT    DEFAULT '',
    event_slug          TEXT    DEFAULT '',
    -- Migration v3
    scale_in_count      INTEGER DEFAULT 0,
    last_scale_at       REAL    DEFAULT NULL,
    position_history    TEXT    DEFAULT '[]',
    parent_bet_id       TEXT    DEFAULT NULL,
    bet_type            TEXT    DEFAULT 'entry',
    -- Migration v4
    dimension_tags      TEXT    DEFAULT '',
    ai_reasoning        TEXT    DEFAULT '',
    -- Migration v5
    condition_id        TEXT    DEFAULT '',
    source              TEXT    DEFAULT 'auto',
    -- Migration v6
    matched_shares      REAL    DEFAULT 0,
    -- Migration v7
    sell_fail_count     INTEGER DEFAULT 0,
    -- Migration v12
    ghost_reopened_at   REAL    DEFAULT NULL,
    -- Migration v16
    trailing_peak_price REAL    DEFAULT NULL,
    trailing_floor_price REAL   DEFAULT NULL,
    -- Migration v18
    strategy_tag        TEXT    DEFAULT NULL,
    strategy_params     TEXT    DEFAULT NULL,
    -- Migration v22
    copy_source_wallet  TEXT    DEFAULT NULL,
    -- Migration v23
    order_type          TEXT    DEFAULT NULL,
    -- Migration v24
    entry_fee_usdc      REAL    DEFAULT 0,
    exit_fee_usdc       REAL    DEFAULT 0,
    fee_rate            REAL    DEFAULT 0,
    -- Migration v26
    certainty_hold_since TEXT   DEFAULT NULL
);

-- 性能索引 (Migration v25)
CREATE INDEX IF NOT EXISTS idx_pb_resolved_ts ON paper_bets(resolved_outcome, ts_entered DESC);
CREATE INDEX IF NOT EXISTS idx_pb_category ON paper_bets(category);
CREATE INDEX IF NOT EXISTS idx_pb_is_win ON paper_bets(is_win);
CREATE INDEX IF NOT EXISTS idx_pb_exit_type ON paper_bets(exit_type);
CREATE INDEX IF NOT EXISTS idx_pb_ts_entered ON paper_bets(ts_entered DESC);

-- ── Schema Version Tracking ──────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- ── 进化系统 Runtime Overrides (Migration v9) ────────────────

CREATE TABLE IF NOT EXISTS evolution_overrides (
    param_name  TEXT PRIMARY KEY,
    value       REAL NOT NULL,
    updated_at  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evolution_overrides_history (
    id          SERIAL PRIMARY KEY,
    param_name  TEXT NOT NULL,
    old_value   REAL,
    new_value   REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evo_hist_param ON evolution_overrides_history(param_name);

-- ── 价格时序 (Migration v10) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS bet_price_series (
    id          SERIAL PRIMARY KEY,
    bet_id      TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    price_mid   REAL    NOT NULL,
    price_bid   REAL,
    price_ask   REAL
);
CREATE INDEX IF NOT EXISTS idx_bps_bet_ts ON bet_price_series(bet_id, ts);

CREATE TABLE IF NOT EXISTS price_move_log (
    id              SERIAL PRIMARY KEY,
    bet_id          TEXT    NOT NULL,
    threshold_pct   REAL    NOT NULL,
    direction       TEXT    NOT NULL,
    price_at_cross  REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    return_pct      REAL    NOT NULL,
    ts              TEXT    NOT NULL,
    action          TEXT    NOT NULL DEFAULT 'record',
    category        TEXT    DEFAULT '',
    question        TEXT    DEFAULT '',
    UNIQUE(bet_id, threshold_pct, direction)
);
CREATE INDEX IF NOT EXISTS idx_pml_ts ON price_move_log(ts);
CREATE INDEX IF NOT EXISTS idx_pml_bet ON price_move_log(bet_id);

-- ── Copytrader V2 Wallet Pool Baseline ───────────────────────────────

CREATE TABLE IF NOT EXISTS copytrader_v2_wallet_pool (
    address             TEXT PRIMARY KEY,
    source              TEXT    NOT NULL DEFAULT 'manual',
    last_metrics_json   TEXT    DEFAULT '{}',
    last_scored_at      TIMESTAMP,
    primary_category    TEXT    DEFAULT 'other',
    score               REAL    NOT NULL DEFAULT 0,
    is_likely_bot       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    pnl_realized_usd    REAL    DEFAULT 0,
    win_rate_f          REAL    DEFAULT 0,
    trade_count_30d     INTEGER DEFAULT 0,
    max_drawdown_usd    REAL    DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ctv2_pool_score ON copytrader_v2_wallet_pool(score DESC);
CREATE INDEX IF NOT EXISTS idx_ctv2_pool_category ON copytrader_v2_wallet_pool(primary_category);
CREATE INDEX IF NOT EXISTS idx_ctv2_pool_pnl ON copytrader_v2_wallet_pool(pnl_realized_usd DESC);
CREATE INDEX IF NOT EXISTS idx_ctv2_pool_winrate ON copytrader_v2_wallet_pool(win_rate_f DESC);

-- ── 暂停状态 (Migration v16) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS pause_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ── 止损事件 (Migration v17) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS stop_loss_events (
    id              SERIAL PRIMARY KEY,
    timestamp       TEXT    NOT NULL,
    bet_id          TEXT    NOT NULL,
    question        TEXT    DEFAULT '',
    category        TEXT    DEFAULT '',
    trigger_type    TEXT    NOT NULL,
    entry_price     REAL,
    current_price   REAL,
    exit_price      REAL,
    return_pct      REAL,
    hours_to_expiry REAL,
    threshold_pct   REAL,
    size_usdc       REAL,
    pnl             REAL,
    conditions_json TEXT    DEFAULT '{}',
    context_json    TEXT    DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sle_ts ON stop_loss_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_sle_cat ON stop_loss_events(category);
CREATE INDEX IF NOT EXISTS idx_sle_trigger ON stop_loss_events(trigger_type);
CREATE INDEX IF NOT EXISTS idx_sle_bet ON stop_loss_events(bet_id);

-- ── 跟投任务 (Migration v20) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS copy_tasks (
    id                  SERIAL PRIMARY KEY,
    wallet_address      TEXT    NOT NULL,
    categories          TEXT    DEFAULT '[]',
    base_usdc           REAL    DEFAULT 10.0,
    niche_only          INTEGER DEFAULT 1,
    score_threshold     REAL    DEFAULT 75.0,
    status              TEXT    DEFAULT 'running',
    created_at          TEXT    NOT NULL,
    last_active_at      TEXT,
    last_seen_trade_ts  REAL    DEFAULT 0,
    error_count         INTEGER DEFAULT 0,
    last_error          TEXT,
    tag_label           TEXT    DEFAULT '',
    total_signals       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ct_wallet ON copy_tasks(wallet_address);
CREATE INDEX IF NOT EXISTS idx_ct_status ON copy_tasks(status);

-- ── SmartEntry 缓存 (Migration v21) ─────────────────────────

CREATE TABLE IF NOT EXISTS smart_entry_cache (
    id              SERIAL PRIMARY KEY,
    city_name       TEXT    NOT NULL,
    date_str        TEXT    NOT NULL,
    city_timezone   TEXT    NOT NULL,
    round_number    INTEGER NOT NULL,
    timestamp_utc   TEXT    NOT NULL,
    yesbest_threshold REAL,
    yesbest_ask     REAL    DEFAULT 0.0,
    signals_json    TEXT    NOT NULL,
    UNIQUE(city_name, date_str, round_number)
);
CREATE INDEX IF NOT EXISTS idx_sec_city_date ON smart_entry_cache(city_name, date_str);

-- ── 标签系统 (Migration v27) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS bet_tags (
    id          SERIAL PRIMARY KEY,
    bet_id      TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'auto',
    created_at  TEXT    NOT NULL,
    UNIQUE(bet_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_bt_bet ON bet_tags(bet_id);
CREATE INDEX IF NOT EXISTS idx_bt_tag ON bet_tags(tag);

CREATE TABLE IF NOT EXISTS market_tags (
    id              SERIAL PRIMARY KEY,
    condition_id    TEXT    NOT NULL,
    tag             TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'auto',
    created_at      TEXT    NOT NULL,
    UNIQUE(condition_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_mt_cond ON market_tags(condition_id);
CREATE INDEX IF NOT EXISTS idx_mt_tag ON market_tags(tag);

-- ── 用户系统 ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    wallet_address      TEXT PRIMARY KEY,
    role                TEXT NOT NULL DEFAULT 'manual_user',
    display_name        TEXT,
    avatar_url          TEXT,
    email               TEXT,
    email_verified      INTEGER NOT NULL DEFAULT 0,
    privy_user_id       TEXT,
    auth_provider       TEXT,
    linked_wallets_json TEXT,
    polymarket_wallet   TEXT,
    created_at          TEXT NOT NULL,
    last_login_at       TEXT,
    source_ip           TEXT,
    updated_at          TEXT NOT NULL,
    referral_code       TEXT DEFAULT NULL,
    referred_by         TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token               TEXT PRIMARY KEY,
    wallet_address      TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'manual_user',
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    last_used_at        TEXT,
    source_ip           TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_wallet ON user_sessions(wallet_address);

-- ── 积分系统 (Migration v28) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS user_points (
    wallet_address  TEXT PRIMARY KEY,
    balance         INTEGER NOT NULL DEFAULT 0,
    total_earned    INTEGER NOT NULL DEFAULT 0,
    total_spent     INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_transactions (
    id              SERIAL PRIMARY KEY,
    wallet_address  TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    amount          INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    ref_id          TEXT,
    description     TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_wallet ON point_transactions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_pt_source ON point_transactions(source);
CREATE INDEX IF NOT EXISTS idx_pt_ts ON point_transactions(created_at DESC);

CREATE TABLE IF NOT EXISTS point_rules (
    id              SERIAL PRIMARY KEY,
    action_type     TEXT    NOT NULL UNIQUE,
    points          INTEGER NOT NULL,
    description     TEXT,
    max_daily       INTEGER DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    condition_type  TEXT    NOT NULL DEFAULT 'always',
    condition_json  TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

ALTER TABLE IF EXISTS point_rules ADD COLUMN IF NOT EXISTS condition_type TEXT NOT NULL DEFAULT 'always';
ALTER TABLE IF EXISTS point_rules ADD COLUMN IF NOT EXISTS condition_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS points_tiers (
    id              SERIAL PRIMARY KEY,
    tier_name       TEXT    NOT NULL,
    min_points      INTEGER NOT NULL,
    max_points      INTEGER,
    badge           TEXT,
    description     TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- ── 推荐系统 (Migration v28) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS user_referrals (
    id              SERIAL PRIMARY KEY,
    referrer_wallet TEXT    NOT NULL,
    referred_wallet TEXT    NOT NULL UNIQUE,
    referral_code   TEXT    NOT NULL,
    level           INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ur_referrer ON user_referrals(referrer_wallet);
CREATE INDEX IF NOT EXISTS idx_ur_code ON user_referrals(referral_code);

CREATE TABLE IF NOT EXISTS referral_codes (
    code            TEXT    PRIMARY KEY,
    wallet_address  TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_rewards (
    id              SERIAL PRIMARY KEY,
    wallet_address  TEXT    NOT NULL,
    from_wallet     TEXT    NOT NULL,
    level           INTEGER NOT NULL,
    points          INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rr_wallet ON referral_rewards(wallet_address);

-- ── 手续费系统 (Migration v28) ───────────────────────────────

CREATE TABLE IF NOT EXISTS fee_config (
    id              SERIAL PRIMARY KEY,
    fee_type        TEXT    NOT NULL,
    rate_bps        INTEGER NOT NULL DEFAULT 0,
    min_fee_usdc    REAL    NOT NULL DEFAULT 0,
    max_fee_usdc    REAL    NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    effective_at    TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS fee_transactions (
    id              SERIAL PRIMARY KEY,
    wallet_address  TEXT    NOT NULL,
    bet_id          TEXT,
    trade_type      TEXT    NOT NULL,
    gross_amount    REAL    NOT NULL,
    fee_rate_bps    INTEGER NOT NULL DEFAULT 0,
    fee_amount      REAL    NOT NULL DEFAULT 0,
    discount_points INTEGER NOT NULL DEFAULT 0,
    discount_amount REAL    NOT NULL DEFAULT 0,
    final_fee       REAL    NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ft_wallet ON fee_transactions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_ft_ts ON fee_transactions(created_at DESC);

CREATE TABLE IF NOT EXISTS fee_discount_tiers (
    id              SERIAL PRIMARY KEY,
    min_points      INTEGER NOT NULL,
    discount_pct    REAL    NOT NULL,
    description     TEXT,
    active          INTEGER NOT NULL DEFAULT 1
);

-- ── 订阅管理 (Migration v29) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS user_subscriptions (
    wallet_address   TEXT PRIMARY KEY,
    plan             TEXT NOT NULL DEFAULT 'free',
    status           TEXT NOT NULL DEFAULT 'active',
    seats            INTEGER NOT NULL DEFAULT 1,
    expires_at       TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usub_plan ON user_subscriptions(plan);
CREATE INDEX IF NOT EXISTS idx_usub_status ON user_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_usub_updated ON user_subscriptions(updated_at DESC);

-- ── RLS 多租户隔离 ───────────────────────────────────────────

-- 为包含用户数据的表启用 RLS
-- 注意: superuser 和 table owner 默认绕过 RLS
-- 应用程序通过 SET app.current_wallet = '0x...' 设置当前用户

-- paper_bets 暂不启用 RLS（Bot 需要全量访问）
-- 以下为用户隔离表的 RLS 策略（在 SaaS 多用户模式下启用）

-- ALTER TABLE user_points ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY user_points_isolation ON user_points
--     USING (wallet_address = current_setting('app.current_wallet', true));

-- ALTER TABLE point_transactions ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY point_txn_isolation ON point_transactions
--     USING (wallet_address = current_setting('app.current_wallet', true));

-- ALTER TABLE fee_transactions ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY fee_txn_isolation ON fee_transactions
--     USING (wallet_address = current_setting('app.current_wallet', true));

-- ── 记录最终 schema version ──────────────────────────────────

INSERT INTO schema_version (version)
SELECT v FROM generate_series(1, 29) AS v
WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE version = v);

-- ── T605 默认积分/手续费规则种子（幂等） ─────────────────────
-- 仅在表为空时插入；管理员后台可 CRUD，不会被复覆盖。

INSERT INTO point_rules (action_type, points, description, max_daily, active, condition_type, condition_json, created_at, updated_at)
SELECT v.action_type, v.points, v.description, v.max_daily, 1, 'always', '{}', NOW()::TEXT, NOW()::TEXT
FROM (VALUES
    ('signup',              200, '新用户首次注册（Privy 登录成功）',                 1),
    ('first_trade',         100, '首笔交易完成（任意金额）',                          1),
    ('daily_login',          10, '每日首次登录签到',                                  1),
    ('daily_trade',          20, '每日首笔交易',                                      1),
    ('trade_volume_10u',     5,  '每累计 10 USDC 交易量',                             0),
    ('referral_l1',         300, '一级推荐：被邀请人完成首笔交易',                    0),
    ('referral_l2',         100, '二级推荐：下下级完成首笔交易',                      0),
    ('kyc_complete',        500, '完成 KYC 验证',                                     1),
    ('mfa_enable',          150, '开启 MFA 双因子认证',                               1),
    ('weekly_streak_7',     200, '连续 7 天登录',                                     1),
    ('share_market',         15, '分享单个市场到外部社交',                            5),
    ('feedback_submit',      30, '提交一条产品反馈被采纳',                            3)
) AS v(action_type, points, description, max_daily)
WHERE NOT EXISTS (SELECT 1 FROM point_rules p WHERE p.action_type = v.action_type);

INSERT INTO points_tiers (tier_name, min_points, max_points, badge, description, active, sort_order, created_at, updated_at)
SELECT v.tier_name, v.min_points, v.max_points, v.badge, v.description, 1, v.sort_order, NOW()::TEXT, NOW()::TEXT
FROM (VALUES
    ('Bronze', 0, 999, 'bronze', 'Entry tier', 10),
    ('Silver', 1000, 4999, 'silver', 'Stable trader', 20),
    ('Gold', 5000, 19999, 'gold', 'High activity', 30),
    ('Platinum', 20000, 99999, 'platinum', 'Advanced tier', 40),
    ('Diamond', 100000, NULL, 'diamond', 'Top tier', 50)
) AS v(tier_name, min_points, max_points, badge, description, sort_order)
WHERE NOT EXISTS (SELECT 1 FROM points_tiers t WHERE t.tier_name = v.tier_name);

-- 默认手续费配置：仅当表为空时写入一条 active=1。
INSERT INTO fee_config (fee_type, rate_bps, min_fee_usdc, max_fee_usdc, active, effective_at, created_at, updated_at)
SELECT 'trade', 50, 0.01, 50.0, 1, NOW()::TEXT, NOW()::TEXT, NOW()::TEXT
WHERE NOT EXISTS (SELECT 1 FROM fee_config);

-- 默认折扣阶梯（按积分门槛累进）。
INSERT INTO fee_discount_tiers (min_points, discount_pct, description, active)
SELECT v.min_points, v.discount_pct, v.description, 1
FROM (VALUES
    (0,        0.00, 'Bronze · 无折扣'),
    (1000,     5.00, 'Silver · 5% 手续费折扣'),
    (5000,    10.00, 'Gold · 10% 手续费折扣'),
    (20000,   20.00, 'Platinum · 20% 手续费折扣'),
    (100000,  35.00, 'Diamond · 35% 手续费折扣'),
    (500000,  50.00, 'VIP · 50% 手续费折扣')
) AS v(min_points, discount_pct, description)
WHERE NOT EXISTS (
    SELECT 1 FROM fee_discount_tiers t WHERE t.min_points = v.min_points
);

-- ── 钱包绑定表 ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallet_bindings (
    wallet_address TEXT PRIMARY KEY,
    bound_at       TEXT NOT NULL,
    source_ip      TEXT,
    updated_at     TEXT NOT NULL
);

-- ── 用户 Polymarket 画像 ────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_pm_profiles (
    wallet_address          TEXT PRIMARY KEY,
    login_wallet            TEXT,
    polymarket_wallet       TEXT,
    proxy_wallet            TEXT,
    linked_wallets_json     TEXT,
    discovery_method        TEXT,
    discovery_confidence    REAL NOT NULL DEFAULT 0,
    profile_json            TEXT,
    last_synced_at          TEXT,
    last_error              TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- ── 用户权益快照 ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_pm_equity_snapshots (
    id                   SERIAL PRIMARY KEY,
    wallet_address       TEXT NOT NULL,
    snapshot_ts          TEXT NOT NULL,
    polymarket_wallet    TEXT,
    portfolio_value      REAL NOT NULL DEFAULT 0,
    usdc_balance         REAL,
    open_positions_count INTEGER NOT NULL DEFAULT 0,
    unrealized_pnl       REAL NOT NULL DEFAULT 0,
    realized_pnl         REAL NOT NULL DEFAULT 0,
    raw_json             TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_snap_wallet ON user_pm_equity_snapshots(wallet_address);

-- ── 用户持仓快照 ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_pm_positions_snapshots (
    id                SERIAL PRIMARY KEY,
    wallet_address    TEXT NOT NULL,
    snapshot_ts       TEXT NOT NULL,
    polymarket_wallet TEXT,
    condition_id      TEXT,
    asset             TEXT,
    title             TEXT,
    outcome           TEXT,
    size              REAL,
    avg_price         REAL,
    initial_value     REAL,
    current_value     REAL,
    cash_pnl          REAL,
    percent_pnl       REAL,
    cur_price         REAL,
    event_id          TEXT,
    slug              TEXT,
    event_slug        TEXT,
    end_date          TEXT,
    raw_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_snap_wallet ON user_pm_positions_snapshots(wallet_address);

-- ── 用户活动快照 ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_pm_activity_snapshots (
    id                SERIAL PRIMARY KEY,
    wallet_address    TEXT NOT NULL,
    snapshot_ts       TEXT NOT NULL,
    polymarket_wallet TEXT,
    activity_type     TEXT,
    condition_id      TEXT,
    asset             TEXT,
    title             TEXT,
    outcome           TEXT,
    size              REAL,
    usdc_size         REAL,
    price             REAL,
    event_id          TEXT,
    event_slug        TEXT,
    activity_ts       INTEGER,
    tx_hash           TEXT,
    raw_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_act_snap_wallet ON user_pm_activity_snapshots(wallet_address);

-- ─────────────────────────────────────────────────────────────────────
-- T611 — PM 快照表反膨胀架构治理（idempotent，每次部署都执行）
--
-- 历史问题：
--   _sync_user_pm_data 每 45s 一次：每用户每刷新都写入 1 equity + N positions + M activities，
--   且 activities 把同一笔 tx 反复重写。10 天 × 6 用户 = 24K activity / 6K positions / 1.5K equity
--   行。线性外推到 1 万用户 / 1 年 = 数十亿行，必崩。
--
-- 治理策略：
--   1. activities：UNIQUE (wallet_address, tx_hash) — 同一笔 tx 永远只占一行（写路径改 UPSERT）
--   2. positions：UNIQUE (wallet_address, condition_id, asset) — 当前持仓单条，写路径 UPSERT
--   3. equity：按小时桶 UNIQUE (wallet_address, snapshot_hour) + 90 天保留剪枝
--
-- 本块迁移：去重历史行 + 加约束 + 加保留剪枝。所有操作 IF NOT EXISTS / ON CONFLICT，可重放。
-- ─────────────────────────────────────────────────────────────────────

-- ── activities：去重 + UNIQUE(tx_hash) ─────────────────────
-- 1) 删除 tx_hash 重复的行（保留 id 最小的）
DELETE FROM user_pm_activity_snapshots a
USING user_pm_activity_snapshots b
WHERE a.id > b.id
  AND a.wallet_address = b.wallet_address
  AND a.tx_hash IS NOT NULL AND a.tx_hash <> ''
  AND b.tx_hash IS NOT NULL AND b.tx_hash <> ''
  AND a.tx_hash = b.tx_hash;

-- 2) 删除既无 tx_hash 又重复的行（按 condition_id|outcome|activity_ts|size|price 软去重）
DELETE FROM user_pm_activity_snapshots a
USING user_pm_activity_snapshots b
WHERE a.id > b.id
  AND a.wallet_address = b.wallet_address
  AND COALESCE(a.tx_hash, '') = ''
  AND COALESCE(b.tx_hash, '') = ''
  AND COALESCE(a.activity_type, '')  = COALESCE(b.activity_type, '')
  AND COALESCE(a.condition_id, '')   = COALESCE(b.condition_id, '')
  AND COALESCE(a.outcome, '')        = COALESCE(b.outcome, '')
  AND COALESCE(a.activity_ts, 0)     = COALESCE(b.activity_ts, 0)
  AND COALESCE(a.size, 0)            = COALESCE(b.size, 0)
  AND COALESCE(a.price, 0)           = COALESCE(b.price, 0);

-- 3) 部分唯一索引：tx_hash 非空时必须唯一
CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_activity_wallet_tx
    ON user_pm_activity_snapshots (wallet_address, tx_hash)
    WHERE tx_hash IS NOT NULL AND tx_hash <> '';

-- ── positions：去重，仅保留每钱包每仓位的最新行 ─────────────
DELETE FROM user_pm_positions_snapshots a
USING user_pm_positions_snapshots b
WHERE a.id < b.id
  AND a.wallet_address = b.wallet_address
  AND COALESCE(a.condition_id, '') = COALESCE(b.condition_id, '')
  AND COALESCE(a.asset, '')        = COALESCE(b.asset, '');

-- 部分唯一索引：(condition_id, asset) 都非空时唯一
CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_positions_wallet_cond_asset
    ON user_pm_positions_snapshots (wallet_address, condition_id, asset)
    WHERE condition_id IS NOT NULL AND asset IS NOT NULL;

-- ── equity：按小时桶去重 + 90 天保留 ───────────────────────
-- 增加 snapshot_hour 生成列（'YYYY-MM-DDTHH'），方便 UNIQUE/UPSERT 与按小时去重
ALTER TABLE user_pm_equity_snapshots
    ADD COLUMN IF NOT EXISTS snapshot_hour TEXT
    GENERATED ALWAYS AS (substring(snapshot_ts FROM 1 FOR 13)) STORED;

-- 1) 先按小时去重（保留 id 最大 = 最新一次刷新），否则下面创建 UNIQUE 会失败
DELETE FROM user_pm_equity_snapshots a
USING user_pm_equity_snapshots b
WHERE a.id < b.id
  AND a.wallet_address = b.wallet_address
  AND a.snapshot_hour  = b.snapshot_hour;

-- 2) 创建唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_equity_wallet_hour
    ON user_pm_equity_snapshots (wallet_address, snapshot_hour);

-- 3) 90 天保留：剪枝超过 90 天的 equity 历史
DELETE FROM user_pm_equity_snapshots
WHERE snapshot_ts < to_char(NOW() - INTERVAL '90 days', 'YYYY-MM-DD"T"HH24:MI:SS');

-- ── 用户运行时控制 ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_runtime_controls (
    wallet_address          TEXT PRIMARY KEY,
    freeze_auto             INTEGER NOT NULL DEFAULT 0,
    auto_trading_enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at              TEXT NOT NULL
);

-- ── Vault transactions (T483: Postgres 版) ──────────────────

CREATE TABLE IF NOT EXISTS vault_transactions (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT    NOT NULL DEFAULT 'system',
    tx_type     TEXT    NOT NULL,
    tx_hash     TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    token       TEXT,
    amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    extra_json  TEXT,
    created_at  BIGINT  NOT NULL DEFAULT 0,
    updated_at  BIGINT  NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vault_tx_user ON vault_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_tx_type ON vault_transactions(tx_type);

-- ── T485: 投注 → 积分 / 手续费 兑换审计 (银行级一致性) ──────

CREATE TABLE IF NOT EXISTS trade_settlements (
    id              SERIAL PRIMARY KEY,
    wallet_address  TEXT    NOT NULL,
    order_ref       TEXT    NOT NULL UNIQUE,    -- CLOB order_id 或 idem-key, 唯一防双计
    market_id       TEXT,
    token_id        TEXT,
    side            TEXT,                        -- BUY/SELL
    notional_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    fee_bps         INTEGER NOT NULL DEFAULT 0,
    fee_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    points_awarded  INTEGER NOT NULL DEFAULT 0,
    points_tx_id    INTEGER,                     -- FK to point_transactions(id), 软引用
    status          TEXT    NOT NULL DEFAULT 'settled',
    created_at      TEXT    NOT NULL,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts_wallet ON trade_settlements(wallet_address, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ts_status ON trade_settlements(status);

-- ── T558: app_menus i18n 字段（让 admin 在「应用菜单管理」直接维护多语言文案）──
-- app_menus 由 src/utils/db.py 的 v33 迁移创建；web_dashboard.py 不会调用 init_db()，
-- 因此必须在这里再用 ALTER ... IF NOT EXISTS 形式幂等加列，确保线上 PG 真正补齐。
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'app_menus') THEN
        ALTER TABLE app_menus ADD COLUMN IF NOT EXISTS i18n_key TEXT;
        ALTER TABLE app_menus ADD COLUMN IF NOT EXISTS label_zh TEXT;
        ALTER TABLE app_menus ADD COLUMN IF NOT EXISTS label_en TEXT;
        -- 回填默认 i18n_key 与 zh/en 文案（仅当此前为空时写入，保留 admin 改过的值）
        UPDATE app_menus SET i18n_key='nav.dashboard',  label_zh='仪表盘',     label_en='Dashboard'        WHERE path IN ('/v2','/v2/')                                AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.discover',   label_zh='发现机会',   label_en='Discover'         WHERE path='/discover'                                       AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.weatherMap', label_zh='天气雷达',   label_en='Weather Map'      WHERE path IN ('/WeatherMap','/WeatherEventMap','/weather') AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.copytraders',label_zh='智能跟投',   label_en='Smart Copytrader' WHERE path IN ('/Copytrader','/Copytraders')                AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.trades',     label_zh='投注记录',   label_en='MyTrades'         WHERE path IN ('/v2/trades','/trades')                       AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.admin',      label_zh='管理后台',   label_en='Admin'            WHERE path='/admin'                                           AND (i18n_key IS NULL OR i18n_key='');
        UPDATE app_menus SET i18n_key='nav.sportsLive', label_zh='赛事赔率',   label_en='Sports Odds'      WHERE path IN ('/sportslive','/v2/sportslive')               AND (i18n_key IS NULL OR i18n_key='');
        -- T874 — 期货赛事入口（OddsPapi futures SSR 页面 /futures），幂等插入。
        INSERT INTO app_menus (label, path, icon, sort_order, visible, role_required, i18n_key, label_zh, label_en)
        VALUES ('期货赛事', '/futures', 'trophy', 35, TRUE, 'user', 'nav.futures', '期货赛事', 'Futures')
        ON CONFLICT (path) DO NOTHING;
    END IF;
END $$;


-- ─────────────────────────────────────────────────────────────────────
-- T613 / Admin V3 Phase A — 独立后台管理系统 schema
-- 完全隔离的 admin_v3 schema，与 public 业务表零耦合，便于独立 restore / drop。
-- 参考：T612 模块规划建议 § 5「数据模型」
-- ─────────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS admin_v3;

CREATE TABLE IF NOT EXISTS admin_v3.admin_users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    linked_wallet   TEXT,
    role            TEXT NOT NULL DEFAULT 'admin',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,
    failed_attempts INT NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_admin_users_role ON admin_v3.admin_users(role);

CREATE TABLE IF NOT EXISTS admin_v3.admin_2fa_secrets (
    admin_id          BIGINT PRIMARY KEY REFERENCES admin_v3.admin_users(id) ON DELETE CASCADE,
    totp_secret       TEXT NOT NULL,
    enrolled_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS admin_v3.admin_role_permissions (
    role        TEXT NOT NULL,
    permission  TEXT NOT NULL,
    PRIMARY KEY (role, permission)
);

-- 种子权限：super_admin = 全部；admin = 读 + 部分写（V3 Phase A 极简）
INSERT INTO admin_v3.admin_role_permissions (role, permission) VALUES
    ('super_admin', '*'),
    ('admin', 'user:read'), ('admin', 'user:write'),
    ('admin', 'config:read'),
    ('admin', 'audit:read'),
    ('admin', 'security:read'), ('admin', 'security:write'),
    ('admin', 'integrations:read'),
    ('admin', 'cms:read'), ('admin', 'cms:write'),
    ('admin', 'incentives:read'),
    ('admin', 'ops:read'),
    ('admin', 'dba:read')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS admin_v3.admin_audit_log (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_id      BIGINT,
    actor_email   TEXT,
    action        TEXT NOT NULL,
    resource      TEXT,
    resource_id   TEXT,
    before_json   JSONB,
    after_json    JSONB,
    ip            TEXT,
    user_agent    TEXT,
    success       BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts        ON admin_v3.admin_audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor_ts  ON admin_v3.admin_audit_log (actor_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action    ON admin_v3.admin_audit_log (action);

-- 授权 polymarket 可读写 admin_v3 schema（V3 服务复用同一 DB user）
GRANT USAGE ON SCHEMA admin_v3 TO polymarket;
GRANT ALL ON ALL TABLES IN SCHEMA admin_v3 TO polymarket;
GRANT ALL ON ALL SEQUENCES IN SCHEMA admin_v3 TO polymarket;
ALTER DEFAULT PRIVILEGES IN SCHEMA admin_v3 GRANT ALL ON TABLES TO polymarket;
ALTER DEFAULT PRIVILEGES IN SCHEMA admin_v3 GRANT ALL ON SEQUENCES TO polymarket;
