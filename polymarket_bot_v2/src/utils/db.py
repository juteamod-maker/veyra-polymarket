"""
db.py — PostgreSQL 数据库初始化与操作工具

已从 SQLite 迁移至 PostgreSQL。所有连接通过 src/utils/database.py 连接池管理。
保持原有函数签名兼容（db_path 参数被忽略）。
"""

import json
import logging
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from src.utils.database import (
    OperationalError,
    _adapt_ddl,
    close_pool,
    connect,
    db_connect,
    get_table_columns,
    init_pool,
)

logger = logging.getLogger(__name__)


def set_db_path(path: Path) -> None:
    """[兼容保留] 原 SQLite 路径设置，PostgreSQL 模式下忽略。"""
    logger.info(f"[兼容] set_db_path({path}) — PostgreSQL 模式下忽略，使用连接池")


def init_db(db_path: Path = None) -> None:
    """建库建表（幂等）。PostgreSQL 模式下通过连接池执行 DDL。"""
    init_pool()
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                question    TEXT,
                score       REAL,
                tier        INTEGER
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                outcome     TEXT,
                ev          REAL,
                confidence  REAL,
                size_usdc   REAL,
                model       TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                outcome     TEXT,
                size_usdc   REAL,
                price       REAL,
                tx_hash     TEXT,
                status      TEXT
            );

            CREATE TABLE IF NOT EXISTS balance_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                usdc_balance REAL
            );

            CREATE TABLE IF NOT EXISTS news_cache (
                cache_key   TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                ts          REAL NOT NULL
            );
        """)
        # ── 自我进化系统表 ──────────────────────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calibration_data (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                category        TEXT    NOT NULL,
                bin_index       INTEGER NOT NULL,
                ai_probability  REAL    NOT NULL,
                actual_outcome  INTEGER NOT NULL,
                model_name      TEXT    DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_calibration_category
                ON calibration_data(category);

            CREATE TABLE IF NOT EXISTS model_performance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                model_name      TEXT    NOT NULL,
                category        TEXT    NOT NULL DEFAULT '',
                predicted_prob  REAL    NOT NULL,
                actual_outcome  INTEGER NOT NULL,
                squared_error   REAL    NOT NULL,
                abs_error       REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_perf_model
                ON model_performance(model_name);

            CREATE TABLE IF NOT EXISTS evolution_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                param_name      TEXT    NOT NULL,
                old_value       REAL    NOT NULL,
                new_value       REAL    NOT NULL,
                reason          TEXT    NOT NULL,
                data_points     INTEGER NOT NULL DEFAULT 0,
                confidence      REAL    NOT NULL DEFAULT 0.0
            );
        """)
        # paper_bets 表 — 专用于模拟投注验证（1000次天气赛道测试）
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_bets (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
                exit_type           TEXT    DEFAULT NULL
            );
        """)
        # ── 版本化迁移系统（Tech Debt #8 — 替代 try/except ALTER TABLE）──
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )""")
        current_version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS ver FROM schema_version"
        ).fetchone()
        current_version = current_version_row["ver"] if current_version_row else 0

        _migrations: list[tuple[int, list[str]]] = [
            (
                1,
                [
                    "ALTER TABLE paper_bets ADD COLUMN exit_price REAL",
                    "ALTER TABLE paper_bets ADD COLUMN exit_type TEXT DEFAULT 'resolution'",
                ],
            ),
            (
                2,
                [
                    "ALTER TABLE paper_bets ADD COLUMN bet_bucket TEXT DEFAULT 'standard'",
                    "ALTER TABLE paper_bets ADD COLUMN market_slug TEXT DEFAULT ''",
                    "ALTER TABLE paper_bets ADD COLUMN event_slug TEXT DEFAULT ''",
                ],
            ),
            (
                3,
                [
                    "ALTER TABLE paper_bets ADD COLUMN scale_in_count INTEGER DEFAULT 0",
                    "ALTER TABLE paper_bets ADD COLUMN last_scale_at REAL DEFAULT NULL",
                    "ALTER TABLE paper_bets ADD COLUMN position_history TEXT DEFAULT '[]'",
                    "ALTER TABLE paper_bets ADD COLUMN parent_bet_id TEXT DEFAULT NULL",
                    "ALTER TABLE paper_bets ADD COLUMN bet_type TEXT DEFAULT 'entry'",
                ],
            ),
            (
                4,
                [
                    "ALTER TABLE paper_bets ADD COLUMN dimension_tags TEXT DEFAULT ''",
                    "ALTER TABLE paper_bets ADD COLUMN ai_reasoning TEXT DEFAULT ''",
                ],
            ),
            (
                5,
                [
                    "ALTER TABLE paper_bets ADD COLUMN condition_id TEXT DEFAULT ''",
                    "ALTER TABLE paper_bets ADD COLUMN source TEXT DEFAULT 'auto'",
                ],
            ),
            (
                6,
                [
                    "ALTER TABLE paper_bets ADD COLUMN matched_shares REAL DEFAULT 0",
                ],
            ),
            (
                7,
                [
                    "ALTER TABLE paper_bets ADD COLUMN sell_fail_count INTEGER DEFAULT 0",
                ],
            ),
            (
                8,
                [
                    # Phase 37: exit_type DEFAULT 修复
                    # 1) 尚未结算的仓位不应有 exit_type，清除错误的 DEFAULT 值
                    "UPDATE paper_bets SET exit_type = NULL WHERE resolved_outcome IS NULL AND exit_type = 'resolution'",
                    # 2) 已结算(非主动退出)的仓位应明确 exit_type='resolution'
                    "UPDATE paper_bets SET exit_type = 'resolution' WHERE resolved_outcome IS NOT NULL AND exit_type = 'resolution'",
                ],
            ),
            (
                9,
                [
                    # T152: 进化系统 runtime_overrides 持久化
                    """CREATE TABLE IF NOT EXISTS evolution_overrides (
                        param_name  TEXT PRIMARY KEY,
                        value       REAL NOT NULL,
                        updated_at  TEXT NOT NULL,
                        reason      TEXT NOT NULL DEFAULT ''
                    )""",
                    """CREATE TABLE IF NOT EXISTS evolution_overrides_history (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        param_name  TEXT NOT NULL,
                        old_value   REAL,
                        new_value   REAL NOT NULL,
                        reason      TEXT NOT NULL DEFAULT '',
                        created_at  TEXT NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_evo_hist_param ON evolution_overrides_history(param_name)",
                ],
            ),
            (
                10,
                [
                    # T169: 分级涨跌记录 — 价格时序 + 阈值穿越事件
                    """CREATE TABLE IF NOT EXISTS bet_price_series (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        bet_id      TEXT    NOT NULL,
                        ts          TEXT    NOT NULL,
                        price_mid   REAL    NOT NULL,
                        price_bid   REAL,
                        price_ask   REAL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_bps_bet_ts ON bet_price_series(bet_id, ts)",
                    """CREATE TABLE IF NOT EXISTS price_move_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_pml_ts ON price_move_log(ts)",
                    "CREATE INDEX IF NOT EXISTS idx_pml_bet ON price_move_log(bet_id)",
                ],
            ),
            (
                11,
                [
                    # T177: 清除被双重反转污染的 NO 方向价格数据
                    # commit 83eeb72~6751c3f 期间 flush_to_db 写入了 NO 调整后价格
                    # 而前端已有 isNO 转换 → 双重反转。直接删除，PriceMonitor 会重建。
                    "DELETE FROM bet_price_series WHERE bet_id IN "
                    "(SELECT bet_id FROM paper_bets WHERE UPPER(outcome) = 'NO')",
                    "DELETE FROM price_move_log WHERE bet_id IN "
                    "(SELECT bet_id FROM paper_bets WHERE UPPER(outcome) = 'NO')",
                ],
            ),
            (
                12,
                [
                    # T179: 幽灵仓位重开标记 — 防止 check_and_update 自解析立即回关
                    # reopen_ghost_position() 设置此字段，check_and_update() 跳过自解析
                    "ALTER TABLE paper_bets ADD COLUMN ghost_reopened_at REAL DEFAULT NULL",
                ],
            ),
            (
                13,
                [
                    # Phase 54 Fix: exit_type DEFAULT 'resolution' 残留彻底清除
                    # migration v1 添加列时 DEFAULT='resolution'，导致所有通过 INSERT
                    # 创建的新投注（不显式指定 exit_type）都带有 exit_type='resolution'，
                    # 在仪表盘和过滤逻辑中造成状态混乱。
                    # insert_paper_bet 同步修复（显式传 exit_type=NULL），但已有脏数据需清除。
                    "UPDATE paper_bets SET exit_type = NULL WHERE resolved_outcome IS NULL AND exit_type = 'resolution'",
                ],
            ),
            (
                14,
                [
                    # Phase 55 Fix: write_off 概念废弃，统一改用 stop_loss
                    # write_off 与平台无对应操作，且 pnl 用全损计算（exit_price=0.0 pnl=-cost）
                    # 导致数据与平台实际状态不符。统一改为 stop_loss（主动关闭语义）。
                    "UPDATE paper_bets SET exit_type='stop_loss', resolved_outcome='STOP_LOSS' WHERE exit_type='write_off'",
                    # 修正历史 phantom 记录的 pnl：原用 Gamma 当时市价估算
                    # Denmark FIFA YES (e7703449): Gamma YES=0.605, shares≈6.81, payout=4.12
                    "UPDATE paper_bets SET exit_price=0.605, payout=4.12, pnl=0.5107, is_win=1 WHERE bet_id LIKE 'e7703449%' AND exit_type='stop_loss'",
                    # Miami temp NO (53962c93): Gamma NO=0.13, shares≈6.0, payout=0.78
                    "UPDATE paper_bets SET exit_price=0.13, payout=0.78, pnl=-3.06, is_win=0 WHERE bet_id LIKE '53962c93%' AND exit_type='stop_loss'",
                ],
            ),
            (
                15,
                [
                    # Phase 55 Fix: 删除 position_syncer 误导入的 pre-Phase53 gone持仓
                    # 这两条是 2026-03-21 天气市场的链上 dust 残留（市场已 resolved），
                    # 在 2026-03-27 23:57 被 position_syncer 错误导入为 source=manual。
                    # 根本原因: (1) redeemable 字段为 None（非 True），未被过滤；
                    #           (2) 无 market_end_date > 24h 守卫；
                    #           (3) c9c70723 size=0.01 恰好等于 min_shares 阈值（< 而非 <=）
                    "DELETE FROM paper_bets WHERE bet_id LIKE 'c9c70723%'",
                    "DELETE FROM paper_bets WHERE bet_id LIKE '2b605f44%'",
                    # Phase 55 Fix: 修正 33942c93 Toronto YES 投注的腐败数据
                    # 原 market_price=0.0、size_usdc=0.0 导致 pnl=payout=1.86（虚假盈利）
                    # 真实成本: sim_balance_before - sim_balance_after = 2.692 - 0.652 = 2.04 USDC
                    # 真实入场价: ai_probability - ev = 0.9 - 0.5465 = 0.3535
                    # 真实 pnl: payout(1.86) - cost(2.04) = -0.18 (LOSS, not WIN)
                    "UPDATE paper_bets SET size_usdc=2.04, market_price=0.3535, pnl=-0.18, is_win=0 WHERE bet_id LIKE '33942c93%'",
                ],
            ),
            (
                16,
                [
                    # Phase 57: trailing 止盈状态持久化 — 防止重启后 floor/peak 数据丢失
                    "ALTER TABLE paper_bets ADD COLUMN trailing_peak_price REAL DEFAULT NULL",
                    "ALTER TABLE paper_bets ADD COLUMN trailing_floor_price REAL DEFAULT NULL",
                    # Phase 57: RiskMonitor 暂停状态 KV 表 — 跨重启保留暂停标志和赛道连亏
                    """CREATE TABLE IF NOT EXISTS pause_state (
                        key        TEXT PRIMARY KEY,
                        value      TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""",
                ],
            ),
            (
                17,
                [
                    # T239: 止损事件结构化日志表
                    """CREATE TABLE IF NOT EXISTS stop_loss_events (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_sle_ts ON stop_loss_events(timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_sle_cat ON stop_loss_events(category)",
                    "CREATE INDEX IF NOT EXISTS idx_sle_trigger ON stop_loss_events(trigger_type)",
                    "CREATE INDEX IF NOT EXISTS idx_sle_bet ON stop_loss_events(bet_id)",
                ],
            ),
            (
                18,
                [
                    # 策略标签 + 策略参数 — ExitPolicy 联动基础
                    # strategy_tag: 标记下注来源策略（如 "city_no", "conviction_no"）
                    # strategy_params: JSON 字符串，存储策略相关参数（score, yes_best 等）
                    "ALTER TABLE paper_bets ADD COLUMN strategy_tag TEXT DEFAULT NULL",
                    "ALTER TABLE paper_bets ADD COLUMN strategy_params TEXT DEFAULT NULL",
                ],
            ),
            (
                19,
                [
                    # T285: 修正 0c2819ad 北京气温投注 P&L
                    # WeatherPeakMonitor partial sell bug导致只记录首次卖出$0.51
                    # 而平台实际两次卖出: $0.51 + $0.80 = $1.31
                    # 原记录: payout=0.5075 pnl=-2.7541
                    # 修正为: payout=1.31 pnl=-1.9516
                    "UPDATE paper_bets SET payout=1.31, pnl=-1.9516 "
                    "WHERE bet_id='0c2819ad-76c2-4032-ac53-9fea60d53cbd'",
                ],
            ),
            (
                20,
                [
                    # T298: PolyHub Discover 跟投任务管理表
                    """CREATE TABLE IF NOT EXISTS copy_tasks (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_ct_wallet ON copy_tasks(wallet_address)",
                    "CREATE INDEX IF NOT EXISTS idx_ct_status ON copy_tasks(status)",
                ],
            ),
            (
                21,
                [
                    # T137: SmartEntry 轮次缓存持久化 — 重启后保留信号积累
                    """CREATE TABLE IF NOT EXISTS smart_entry_cache (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        city_name       TEXT    NOT NULL,
                        date_str        TEXT    NOT NULL,
                        city_timezone   TEXT    NOT NULL,
                        round_number    INTEGER NOT NULL,
                        timestamp_utc   TEXT    NOT NULL,
                        yesbest_threshold REAL,
                        yesbest_ask     REAL    DEFAULT 0.0,
                        signals_json    TEXT    NOT NULL,
                        UNIQUE(city_name, date_str, round_number)
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_sec_city_date ON smart_entry_cache(city_name, date_str)",
                ],
            ),
            (
                22,
                [
                    # T164: 跟投跟卖 — 标记跟投来源钱包，用于监控原始钱包卖出行为
                    "ALTER TABLE paper_bets ADD COLUMN copy_source_wallet TEXT DEFAULT NULL",
                ],
            ),
            (
                23,
                [
                    # T179-B: 下单方式字段 — 记录每笔投注的实际下单模式
                    # 值: 'maker', 'taker', 'simulated'(DRY RUN)
                    "ALTER TABLE paper_bets ADD COLUMN order_type TEXT DEFAULT NULL",
                ],
            ),
            (
                24,
                [
                    # T189-F: PM手续费记录 — 记录每笔投注被PM扣除的taker手续费
                    # 官方公式: fee_usdc = shares × feeRate × p × (1-p)
                    # 买入时以份额扣除，卖出时以USDC扣除; maker不付费
                    "ALTER TABLE paper_bets ADD COLUMN entry_fee_usdc REAL DEFAULT 0",
                    "ALTER TABLE paper_bets ADD COLUMN exit_fee_usdc REAL DEFAULT 0",
                    "ALTER TABLE paper_bets ADD COLUMN fee_rate REAL DEFAULT 0",
                ],
            ),
            (
                25,
                [
                    # T219-perf: paper_bets 查询性能索引
                    # 根因: 状态筛选/排序全靠全表扫描(5000+行), Dashboard切换状态200-500ms
                    # 覆盖索引: resolved_outcome+ts_entered 覆盖最常用的 open/resolved 筛选+排序
                    "CREATE INDEX IF NOT EXISTS idx_pb_resolved_ts ON paper_bets(resolved_outcome, ts_entered DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_pb_category ON paper_bets(category)",
                    "CREATE INDEX IF NOT EXISTS idx_pb_is_win ON paper_bets(is_win)",
                    "CREATE INDEX IF NOT EXISTS idx_pb_exit_type ON paper_bets(exit_type)",
                    "CREATE INDEX IF NOT EXISTS idx_pb_ts_entered ON paper_bets(ts_entered DESC)",
                ],
            ),
            (
                26,
                [
                    # T235追加: CertaintyHold 超时持久化 — 防止重启后计时器重置
                    # 存储 ISO 格式 UTC 时间戳，重启后从 DB 恢复 hold 开始时间
                    "ALTER TABLE paper_bets ADD COLUMN certainty_hold_since TEXT DEFAULT NULL",
                ],
            ),
            (
                27,
                [
                    # T263: 投注单标签系统 — 灵活筛选维度
                    """CREATE TABLE IF NOT EXISTS bet_tags (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        bet_id      TEXT    NOT NULL,
                        tag         TEXT    NOT NULL,
                        source      TEXT    NOT NULL DEFAULT 'auto',
                        created_at  TEXT    NOT NULL,
                        UNIQUE(bet_id, tag)
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_bt_bet ON bet_tags(bet_id)",
                    "CREATE INDEX IF NOT EXISTS idx_bt_tag ON bet_tags(tag)",
                    # T263: 市场事件标签 — 按 condition_id 标记市场级标签
                    """CREATE TABLE IF NOT EXISTS market_tags (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        condition_id    TEXT    NOT NULL,
                        tag             TEXT    NOT NULL,
                        source          TEXT    NOT NULL DEFAULT 'auto',
                        created_at      TEXT    NOT NULL,
                        UNIQUE(condition_id, tag)
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_mt_cond ON market_tags(condition_id)",
                    "CREATE INDEX IF NOT EXISTS idx_mt_tag ON market_tags(tag)",
                ],
            ),
            (
                28,
                [
                    # T265+T267+T268: 用户积分、三级分销、手续费统一架构
                    # ── 积分账户 ──
                    """CREATE TABLE IF NOT EXISTS user_points (
                        wallet_address  TEXT PRIMARY KEY,
                        balance         INTEGER NOT NULL DEFAULT 0,
                        total_earned    INTEGER NOT NULL DEFAULT 0,
                        total_spent     INTEGER NOT NULL DEFAULT 0,
                        updated_at      TEXT NOT NULL
                    )""",
                    # ── 积分流水 ──
                    """CREATE TABLE IF NOT EXISTS point_transactions (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        wallet_address  TEXT    NOT NULL,
                        type            TEXT    NOT NULL,
                        amount          INTEGER NOT NULL,
                        source          TEXT    NOT NULL,
                        ref_id          TEXT,
                        description     TEXT,
                        created_at      TEXT    NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_pt_wallet ON point_transactions(wallet_address)",
                    "CREATE INDEX IF NOT EXISTS idx_pt_source ON point_transactions(source)",
                    "CREATE INDEX IF NOT EXISTS idx_pt_ts ON point_transactions(created_at DESC)",
                    # ── 积分规则 ──
                    """CREATE TABLE IF NOT EXISTS point_rules (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        action_type     TEXT    NOT NULL UNIQUE,
                        points          INTEGER NOT NULL,
                        description     TEXT,
                        max_daily        INTEGER DEFAULT 0,
                        active          INTEGER NOT NULL DEFAULT 1,
                        condition_type  TEXT    NOT NULL DEFAULT 'always',
                        condition_json  TEXT    NOT NULL DEFAULT '{}',
                        created_at      TEXT    NOT NULL,
                        updated_at      TEXT    NOT NULL
                    )""",
                    """CREATE TABLE IF NOT EXISTS points_tiers (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        tier_name       TEXT    NOT NULL,
                        min_points      INTEGER NOT NULL,
                        max_points      INTEGER,
                        badge           TEXT,
                        description     TEXT,
                        active          INTEGER NOT NULL DEFAULT 1,
                        sort_order      INTEGER NOT NULL DEFAULT 0,
                        created_at      TEXT    NOT NULL,
                        updated_at      TEXT    NOT NULL
                    )""",
                    # ── 三级分销：推荐关系 ──
                    """CREATE TABLE IF NOT EXISTS user_referrals (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        referrer_wallet TEXT    NOT NULL,
                        referred_wallet TEXT    NOT NULL UNIQUE,
                        referral_code   TEXT    NOT NULL,
                        level           INTEGER NOT NULL DEFAULT 1,
                        created_at      TEXT    NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_ur_referrer ON user_referrals(referrer_wallet)",
                    "CREATE INDEX IF NOT EXISTS idx_ur_code ON user_referrals(referral_code)",
                    # ── 推荐码 ──
                    """CREATE TABLE IF NOT EXISTS referral_codes (
                        code            TEXT    PRIMARY KEY,
                        wallet_address  TEXT    NOT NULL UNIQUE,
                        created_at      TEXT    NOT NULL
                    )""",
                    # ── 推荐奖励日志 ──
                    """CREATE TABLE IF NOT EXISTS referral_rewards (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        wallet_address  TEXT    NOT NULL,
                        from_wallet     TEXT    NOT NULL,
                        level           INTEGER NOT NULL,
                        points          INTEGER NOT NULL,
                        reason          TEXT    NOT NULL,
                        created_at      TEXT    NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_rr_wallet ON referral_rewards(wallet_address)",
                    # ── 手续费配置 ──
                    """CREATE TABLE IF NOT EXISTS fee_config (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        fee_type        TEXT    NOT NULL,
                        rate_bps        INTEGER NOT NULL DEFAULT 0,
                        min_fee_usdc    REAL    NOT NULL DEFAULT 0,
                        max_fee_usdc    REAL    NOT NULL DEFAULT 0,
                        active          INTEGER NOT NULL DEFAULT 1,
                        effective_at    TEXT,
                        created_at      TEXT    NOT NULL,
                        updated_at      TEXT    NOT NULL
                    )""",
                    # ── 手续费流水 ──
                    """CREATE TABLE IF NOT EXISTS fee_transactions (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_ft_wallet ON fee_transactions(wallet_address)",
                    "CREATE INDEX IF NOT EXISTS idx_ft_ts ON fee_transactions(created_at DESC)",
                    # ── 积分折扣规则 ──
                    """CREATE TABLE IF NOT EXISTS fee_discount_tiers (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        min_points      INTEGER NOT NULL,
                        discount_pct    REAL    NOT NULL,
                        description     TEXT,
                        active          INTEGER NOT NULL DEFAULT 1
                    )""",
                    # users表补列: 推荐码
                    "ALTER TABLE users ADD COLUMN referral_code TEXT DEFAULT NULL",
                    "ALTER TABLE users ADD COLUMN referred_by TEXT DEFAULT NULL",
                ],
            ),
            (
                29,
                [
                    # T270: SaaS 订阅管理表（超级管理员管理页）
                    """CREATE TABLE IF NOT EXISTS user_subscriptions (
                        wallet_address   TEXT PRIMARY KEY,
                        plan             TEXT NOT NULL DEFAULT 'free',
                        status           TEXT NOT NULL DEFAULT 'active',
                        seats            INTEGER NOT NULL DEFAULT 1,
                        expires_at       TEXT,
                        notes            TEXT,
                        created_at       TEXT NOT NULL,
                        updated_at       TEXT NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_usub_plan ON user_subscriptions(plan)",
                    "CREATE INDEX IF NOT EXISTS idx_usub_status ON user_subscriptions(status)",
                    "CREATE INDEX IF NOT EXISTS idx_usub_updated ON user_subscriptions(updated_at DESC)",
                ],
            ),
            (
                30,
                [
                    # T286-I: PM 标签缓存表（Admin 标签管理 + Trades 标签筛选）
                    """CREATE TABLE IF NOT EXISTS pm_tags (
                        tag_id          BIGINT PRIMARY KEY,
                        slug            TEXT NOT NULL,
                        label           TEXT NOT NULL,
                        parent_tag_id   BIGINT,
                        source          TEXT NOT NULL DEFAULT 'pm',
                        active          INTEGER NOT NULL DEFAULT 1,
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_pm_tags_slug ON pm_tags(slug)",
                    "CREATE INDEX IF NOT EXISTS idx_pm_tags_label ON pm_tags(label)",
                ],
            ),
            (
                31,
                [
                    # T286-II: PM 赛道类别表 + tag-category 联动列
                    """CREATE TABLE IF NOT EXISTS pm_categories (
                        category_id      BIGINT PRIMARY KEY,
                        slug             TEXT NOT NULL UNIQUE,
                        label            TEXT NOT NULL,
                        source           TEXT NOT NULL DEFAULT 'pm_route',
                        pm_path          TEXT,
                        active           INTEGER NOT NULL DEFAULT 1,
                        created_at       TEXT NOT NULL,
                        updated_at       TEXT NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_pm_categories_slug ON pm_categories(slug)",
                    "CREATE INDEX IF NOT EXISTS idx_pm_categories_active ON pm_categories(active)",
                    "ALTER TABLE pm_tags ADD COLUMN category_slug TEXT",
                    "CREATE INDEX IF NOT EXISTS idx_pm_tags_category_slug ON pm_tags(category_slug)",
                ],
            ),
            (
                32,
                [
                    # T288 Wave 4-after: 删除自建邮箱验证码表
                    # 邮箱验证已统一由 Privy MFA 接管，本表彻底废弃。
                    "DROP TABLE IF EXISTS user_email_verifications",
                ],
            ),
            (
                33,
                [
                    # T302: 应用菜单表（admin 可视化管理侧栏菜单项）
                    """CREATE TABLE IF NOT EXISTS app_menus (
                        id SERIAL PRIMARY KEY,
                        label TEXT NOT NULL,
                        path TEXT NOT NULL UNIQUE,
                        icon TEXT NOT NULL DEFAULT 'circle',
                        sort_order INT NOT NULL DEFAULT 100,
                        visible BOOLEAN NOT NULL DEFAULT TRUE,
                        role_required TEXT NOT NULL DEFAULT 'user',
                        section_label TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_app_menus_visible_sort ON app_menus(visible, sort_order)",
                    # T303: 用户灰度 dry-run 标识（管理员可对单个用户开启）
                    "ALTER TABLE users ADD COLUMN gray_dry_run BOOLEAN NOT NULL DEFAULT FALSE",
                    # 种子数据：从 base.html 当前硬编码菜单迁移
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('仪表盘', '/v2', 'layout-dashboard', 10, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('发现机会', '/discover', 'compass', 20, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('天气预测', '/WeatherMap', 'cloud', 30, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('智能跟投', '/Copytraders', 'users', 40, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('投注记录', '/v2/trades', 'file-text', 50, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('回测分析', '/v2/skeleton?p=analysis', 'bar-chart-2', 60, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('投注追踪', '/v2/skeleton?p=monitor', 'activity', 70, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('积分排行榜', '/v2/skeleton?p=leaderboard', 'trophy', 80, 'user') ON CONFLICT (path) DO NOTHING",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required, section_label) VALUES ('管理后台', '/admin', 'shield', 200, 'admin', '管理') ON CONFLICT (path) DO NOTHING",
                ],
            ),
            (
                34,
                [
                    """CREATE TABLE IF NOT EXISTS copytrader_v2_tasks (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL DEFAULT 0,
                        wallet_address TEXT NOT NULL,
                        wallet_label TEXT DEFAULT '',
                        categories TEXT NOT NULL DEFAULT '[]',
                        base_usdc REAL NOT NULL DEFAULT 5.0,
                        max_per_event INTEGER NOT NULL DEFAULT 1,
                        min_score REAL NOT NULL DEFAULT 60.0,
                        max_price REAL NOT NULL DEFAULT 0.85,
                        follow_sell BOOLEAN NOT NULL DEFAULT TRUE,
                        status TEXT NOT NULL DEFAULT 'running',
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_trade_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                        error_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT DEFAULT '',
                        total_signals INTEGER NOT NULL DEFAULT 0,
                        notes TEXT DEFAULT ''
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_user ON copytrader_v2_tasks(user_id)",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_wallet ON copytrader_v2_tasks(wallet_address)",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_status ON copytrader_v2_tasks(status)",
                    """CREATE TABLE IF NOT EXISTS copytrader_v2_settings (
                        user_id INTEGER PRIMARY KEY,
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""",
                    """CREATE TABLE IF NOT EXISTS copytrader_v2_wallet_pool (
                        address TEXT PRIMARY KEY,
                        source TEXT NOT NULL DEFAULT 'manual',
                        last_metrics_json TEXT DEFAULT '{}',
                        last_scored_at TIMESTAMP,
                        primary_category TEXT DEFAULT 'other',
                        score REAL NOT NULL DEFAULT 0,
                        is_likely_bot BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_pool_score ON copytrader_v2_wallet_pool(score DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_pool_category ON copytrader_v2_wallet_pool(primary_category)",
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required) VALUES ('Copytrader V2', '/Copytrader', 'users', 45, 'user') ON CONFLICT (path) DO NOTHING",
                ],
            ),
            (
                35,
                [
                    "DELETE FROM app_menus WHERE path='/Copytraders' AND EXISTS (SELECT 1 FROM app_menus a2 WHERE a2.path='/Copytrader')",
                    "UPDATE app_menus SET path='/Copytrader' WHERE path='/Copytraders'",
                    "UPDATE app_menus SET label='nav.copytraders' WHERE path='/Copytrader'",
                ],
            ),
            (
                36,
                [
                    "ALTER TABLE copytrader_v2_tasks ADD COLUMN owner_wallet_address TEXT",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_owner_wallet ON copytrader_v2_tasks(owner_wallet_address)",
                    "ALTER TABLE copytrader_v2_settings ADD COLUMN owner_wallet_address TEXT",
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ctv2_settings_owner_wallet ON copytrader_v2_settings(owner_wallet_address)",
                ],
            ),
            (
                37,
                [
                    # T397: Smart Wallet Leaderboard — 新增快速查询列，消除 JSON 解析开销
                    "ALTER TABLE copytrader_v2_wallet_pool ADD COLUMN pnl_realized_usd REAL DEFAULT 0",
                    "ALTER TABLE copytrader_v2_wallet_pool ADD COLUMN win_rate_f REAL DEFAULT 0",
                    "ALTER TABLE copytrader_v2_wallet_pool ADD COLUMN trade_count_30d INTEGER DEFAULT 0",
                    "ALTER TABLE copytrader_v2_wallet_pool ADD COLUMN max_drawdown_usd REAL DEFAULT 0",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_pool_pnl ON copytrader_v2_wallet_pool(pnl_realized_usd DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_ctv2_pool_winrate ON copytrader_v2_wallet_pool(win_rate_f DESC)",
                ],
            ),
            (
                # 注：当前 schema_version 已被外部脚本写入到 471，迁移循环会跳过 <= current 的版本号，
                # 因此这里必须使用 > 471 的版本号才能让本次 ALTER 实际生效。
                472,
                [
                    # T558: 应用菜单 i18n 化 —— 让 admin 在「应用菜单管理」里
                    # 直接维护 i18n key 与中英文文案，不再回头改前端代码。
                    # i18n_key 优先用作 data-i18n（命中 i18n 字典）；
                    # label_zh / label_en 在 i18n_key 缺失或字典未命中时用于
                    # 按当前语言直出文案；都缺失则回退到原 label 字段。
                    "ALTER TABLE app_menus ADD COLUMN i18n_key TEXT",
                    "ALTER TABLE app_menus ADD COLUMN label_zh TEXT",
                    "ALTER TABLE app_menus ADD COLUMN label_en TEXT",
                    # 既有 9 条种子菜单回填默认 i18n_key 与 zh/en 文案，
                    # 升级后行为与 base.html 之前的硬编码 I18N_PATH_KEYS 完全一致。
                    "UPDATE app_menus SET i18n_key='nav.dashboard',  label_zh='仪表盘',     label_en='Dashboard'        WHERE path IN ('/v2','/v2/')                AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.discover',   label_zh='发现机会',   label_en='Discover'         WHERE path='/discover'                       AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.weatherMap', label_zh='天气雷达',   label_en='Weather Map'      WHERE path IN ('/WeatherMap','/WeatherEventMap','/weather') AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.copytraders',label_zh='智能跟投',   label_en='Smart Copytrader' WHERE path IN ('/Copytrader','/Copytraders') AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.trades',     label_zh='投注记录',   label_en='MyTrades'         WHERE path IN ('/v2/trades','/trades')       AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.admin',      label_zh='管理后台',   label_en='Admin'            WHERE path='/admin'                           AND (i18n_key IS NULL OR i18n_key='')",
                    "UPDATE app_menus SET i18n_key='nav.sportsLive', label_zh='赛事赔率',   label_en='Sports Odds'      WHERE path IN ('/sportslive','/v2/sportslive') AND (i18n_key IS NULL OR i18n_key='')",
                ],
            ),
            (
                # T874: 新增 /futures 期货赛事菜单条目（OddsPapi futures markets）。
                # 仅插入一次（ON CONFLICT DO NOTHING），不会覆盖管理员后续手动调整。
                473,
                [
                    "INSERT INTO app_menus (label, path, icon, sort_order, role_required, i18n_key, label_zh, label_en) "
                    "VALUES ('期货赛事', '/futures', 'trophy', 35, 'user', 'nav.futures', '期货赛事', 'Futures') "
                    "ON CONFLICT (path) DO NOTHING",
                ],
            ),
        ]

        for ver, stmts in _migrations:
            if ver <= current_version:
                continue
            for stmt in stmts:
                try:
                    conn.execute(_adapt_ddl(stmt))
                except (OperationalError, Exception) as e:
                    err_msg = str(e).lower()
                    # PostgreSQL: "column ... already exists" / SQLite: "duplicate column"
                    if (
                        "already exists" not in err_msg
                        and "duplicate column" not in err_msg
                    ):
                        raise
                    # Column already exists — safe to skip
            conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (ver,))
            logger.info(f"[DB迁移] schema v{ver} 完成")

        try:
            rows = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address IS NOT NULL"
            ).fetchall()
            owner_map: dict[int, str] = {}
            for row in rows:
                wallet = str(row["wallet_address"] or "").strip().lower()
                if not wallet.startswith("0x") or len(wallet) != 42:
                    continue
                legacy_id = zlib.crc32(wallet.encode("utf-8")) & 0x7FFFFFFF
                if legacy_id not in owner_map:
                    owner_map[legacy_id] = wallet
            for legacy_id, wallet in owner_map.items():
                conn.execute(
                    "UPDATE copytrader_v2_tasks SET owner_wallet_address=%s "
                    "WHERE user_id=%s AND (owner_wallet_address IS NULL OR owner_wallet_address='')",
                    (wallet, legacy_id),
                )
                conn.execute(
                    "UPDATE copytrader_v2_settings SET owner_wallet_address=%s "
                    "WHERE user_id=%s AND (owner_wallet_address IS NULL OR owner_wallet_address='')",
                    (wallet, legacy_id),
                )
        except Exception:
            pass

    logger.info(f"数据库已初始化: {db_path} (schema v{max(v for v, _ in _migrations)})")


# ------------------------------------------------------------------
# T189-F: PM 手续费计算
# ------------------------------------------------------------------

# 官方 feeRate 表（源: docs.polymarket.com/trading/fees）
_PM_FEE_RATES: dict[str, float] = {
    "crypto": 0.072,
    "sports": 0.03,
    "financial": 0.04,
    "finance": 0.04,
    "politics": 0.04,
    "weather": 0.05,
    "economy": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "geopolitics": 0,
    "mentions": 0.04,
    "tech": 0.04,
}


def get_market_fee_rate(market_data: dict | None, category: str | None) -> float:
    """从市场 API 数据动态读取 taker 费率，fallback 到 config 赛道配置。

    优先级:
    1. feesEnabled=False → 0.0 (免费市场，如大多数政治/天气市场)
    2. feeSchedule.rate  → 动态费率 (API 直接提供，最准确)
    3. config paper_trading.taker_fee_rates[category] → 赛道配置费率
    4. config paper_trading.taker_fee_rate_default → 全局默认费率

    已知 feeSchedule 结构 (Gamma API 实测):
      sports_fees_v2:  {'exponent': 1, 'rate': 0.03, 'takerOnly': True, 'rebateRate': 0.25}
      crypto_fees_v2:  {'exponent': 1, 'rate': 0.072, 'takerOnly': True, 'rebateRate': 0.2}
      crypto_15_min:   {'exponent': 2, 'rate': 0.25, 'takerOnly': True, 'rebateRate': 0.2}

    官方费用公式: fee_per_share = feeSchedule.rate × p × (1-p)
    """
    if market_data is not None:
        # 1. feesEnabled=False → 免费市场
        if market_data.get("feesEnabled") is False:
            return 0.0
        # 2. feeSchedule.rate → 动态费率
        fs = market_data.get("feeSchedule")
        if fs and isinstance(fs, dict):
            rate = fs.get("rate")
            if rate is not None:
                return float(rate)
    # 3 & 4. config fallback
    from src.utils.config_loader import get as _cfg_get

    _cfg_rates = _cfg_get("paper_trading.taker_fee_rates") or {}
    _cfg_default = float(_cfg_get("paper_trading.taker_fee_rate_default") or 0.05)
    return float(_cfg_rates.get(category or "", 0) or _cfg_default)


def calc_pm_fee(
    shares: float, price: float, category: str, order_type: str = "taker"
) -> tuple[float, float]:
    """计算 Polymarket 手续费（官方公式: fee = shares × feeRate × p × (1-p)）。

    Returns:
        (fee_usdc, fee_rate) — 手续费金额和该赛道的 feeRate
    """
    if order_type == "maker" or order_type == "simulated":
        # Maker 不付费; DRY RUN 模拟也不扣真实手续费
        # 但 simulated 模式仍返回 fee_rate 以供估算参考
        from src.utils.config_loader import get as _cfg_get

        _cfg_rates = _cfg_get("paper_trading.taker_fee_rates") or {}
        _cfg_default = float(_cfg_get("paper_trading.taker_fee_rate_default") or 0.05)
        fr = float(_cfg_rates.get(category, 0) or _cfg_default)
        if order_type == "maker":
            return 0.0, fr
        # simulated: 模拟估算费用
        if shares > 0 and 0 < price < 1:
            return round(shares * fr * price * (1 - price), 6), fr
        return 0.0, fr
    # Taker
    from src.utils.config_loader import get as _cfg_get

    _cfg_rates = _cfg_get("paper_trading.taker_fee_rates") or {}
    _cfg_default = float(_cfg_get("paper_trading.taker_fee_rate_default") or 0.05)
    fr = float(_cfg_rates.get(category, 0) or _cfg_default)
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0, fr
    fee = round(shares * fr * price * (1 - price), 6)
    return fee, fr


# ------------------------------------------------------------------
# paper_bets CRUD
# ------------------------------------------------------------------


def insert_paper_bet(bet: dict, db_path: Path = None) -> None:
    """插入一条模拟投注记录。"""
    # 确保所有 SQL 绑定参数都有对应 key，避免 sqlite3.ProgrammingError（缺失参数）
    # 可选字段默认 None（SQLite 存为 NULL），必填字段提供合理默认值
    row = {
        **bet,
        "confidence": bet.get("confidence"),
        "ai_probability": bet.get("ai_probability"),
        "ev": bet.get("ev", 0),
        "sim_balance_before": bet.get("sim_balance_before"),
        "sim_balance_after": bet.get("sim_balance_after"),
        "market_end_date": bet.get("market_end_date"),
        "weather_context": bet.get("weather_context"),
        "ai_models": bet.get("ai_models"),
        "cycle_id": bet.get("cycle_id"),
        "bet_bucket": bet.get("bet_bucket") or "standard",
        "market_slug": bet.get("market_slug") or "",
        "event_slug": bet.get("event_slug") or "",
        "bet_type": bet.get("bet_type") or "entry",
        "parent_bet_id": bet.get("parent_bet_id"),
        "dimension_tags": bet.get("dimension_tags") or "",
        "ai_reasoning": bet.get("ai_reasoning") or "",
        "condition_id": bet.get("condition_id") or "",
        "source": bet.get("source") or "auto",
        "matched_shares": bet.get("matched_shares") or 0,
        # Phase 54 Fix: 显式设置 exit_type=NULL，防止 SQLite DEFAULT 'resolution'
        # 列默认值（migration v1 遗留）污染所有新建投注记录
        "exit_type": None,
        # 策略标签 + 策略参数
        "strategy_tag": bet.get("strategy_tag"),
        "strategy_params": (
            json.dumps(bet["strategy_params"])
            if isinstance(bet.get("strategy_params"), dict)
            else bet.get("strategy_params")
        ),
        # T164: 跟投来源钱包
        "copy_source_wallet": bet.get("copy_source_wallet"),
        # T179-B: 下单方式（maker/taker/simulated）
        "order_type": bet.get("order_type"),
        # T189-F: PM手续费记录
        "entry_fee_usdc": bet.get("entry_fee_usdc") or 0,
        "exit_fee_usdc": bet.get("exit_fee_usdc") or 0,
        "fee_rate": bet.get("fee_rate") or 0,
    }
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_bets
                (bet_id, ts_entered, market_id, question, category, outcome,
                 confidence, ai_probability, market_price, ev, size_usdc,
                 sim_balance_before, sim_balance_after, market_end_date,
                 weather_context, ai_models, cycle_id, bet_bucket, market_slug,
                 event_slug, bet_type, parent_bet_id, dimension_tags,
                 ai_reasoning, condition_id, source, matched_shares, exit_type,
                 strategy_tag, strategy_params, copy_source_wallet, order_type,
                 entry_fee_usdc, exit_fee_usdc, fee_rate)
            VALUES
                (:bet_id, :ts_entered, :market_id, :question, :category, :outcome,
                 :confidence, :ai_probability, :market_price, :ev, :size_usdc,
                 :sim_balance_before, :sim_balance_after, :market_end_date,
                 :weather_context, :ai_models, :cycle_id, :bet_bucket, :market_slug,
                 :event_slug, :bet_type, :parent_bet_id, :dimension_tags,
                 :ai_reasoning, :condition_id, :source, :matched_shares, :exit_type,
                 :strategy_tag, :strategy_params, :copy_source_wallet, :order_type,
                 :entry_fee_usdc, :exit_fee_usdc, :fee_rate)
            ON CONFLICT (bet_id) DO NOTHING
            """,
            row,
        )


def update_paper_bet_outcome(
    bet_id: str,
    resolved_outcome: str,
    resolved_at: float,
    pnl: float,
    payout: float,
    is_win: int,
    db_path: Path = None,
) -> None:
    """更新已解决市场的模拟投注结果。"""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET resolved_outcome=?, resolved_at=?, pnl=?, payout=?, is_win=?,
                exit_type='resolution', ghost_reopened_at=NULL
            WHERE bet_id=?
            """,
            (resolved_outcome, resolved_at, pnl, payout, is_win, bet_id),
        )


def close_paper_bet_profit_take(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    db_path: Path = None,
) -> None:
    """主动止盈平仓：用当前市场价结算模拟仓位，exit_type='profit_take'。"""
    import time as _time

    is_win = 1 if pnl > 0 else 0
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET resolved_outcome='PROFIT_TAKE', resolved_at=?, pnl=?, payout=?,
                is_win=?, exit_price=?, exit_type='profit_take', ghost_reopened_at=NULL
            WHERE bet_id=?
            """,
            (_time.time(), round(pnl, 4), round(payout, 4), is_win, exit_price, bet_id),
        )


def close_paper_bet_stop_loss(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    db_path: Path = None,
) -> None:
    """主动止损平仓：以当前市场价提前止损，exit_type='stop_loss'，is_win=0。"""
    import time as _time

    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET resolved_outcome='STOP_LOSS', resolved_at=?, pnl=?, payout=?,
                is_win=0, exit_price=?, exit_type='stop_loss', ghost_reopened_at=NULL
            WHERE bet_id=?
            """,
            (_time.time(), round(pnl, 4), round(payout, 4), exit_price, bet_id),
        )


def close_paper_bet_wu_temp_breach(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    db_path: Path = None,
) -> None:
    """
    WU Daily Observations 实测温度已达阈值强制平仓。
    exit_type='wu_temp_breach', resolved_outcome='WU_TEMP_BREACH', is_win=0。
    用于区别普通止损（stop_loss），便于后期收益率分析。
    """
    import time as _time

    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET resolved_outcome='WU_TEMP_BREACH', resolved_at=?, pnl=?, payout=?,
                is_win=0, exit_price=?, exit_type='wu_temp_breach', ghost_reopened_at=NULL
            WHERE bet_id=?
            """,
            (_time.time(), round(pnl, 4), round(payout, 4), exit_price, bet_id),
        )


def close_paper_bet_manual_exit(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    db_path: Path = None,
) -> None:
    """手动退出：链上持仓已消失（用户在网站卖出或市场结算后赎回），exit_type='manual_exit'。"""
    import time as _time

    is_win = 1 if pnl > 0 else 0
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET resolved_outcome='MANUAL_EXIT', resolved_at=?, pnl=?, payout=?,
                is_win=?, exit_price=?, exit_type='manual_exit', ghost_reopened_at=NULL
            WHERE bet_id=? AND resolved_outcome IS NULL
            """,
            (
                _time.time(),
                round(pnl, 4),
                round(payout, 4),
                is_win,
                exit_price,
                bet_id,
            ),
        )


def close_paper_bet_write_off(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    db_path: Path = None,
) -> None:
    """Phase 55 DEPRECATED: write_off 概念已废弃，统一改用 stop_loss。
    保留本函数签名以兼容旧调用点和测试（内部委托给 close_paper_bet_stop_loss）。"""
    close_paper_bet_stop_loss(
        bet_id=bet_id,
        exit_price=exit_price,
        pnl=pnl,
        payout=payout,
        db_path=db_path,
    )


def update_bet_fees(
    bet_id: str,
    entry_fee_usdc: float | None = None,
    exit_fee_usdc: float | None = None,
    fee_rate: float | None = None,
    db_path: Path = None,
) -> None:
    """T189-F: 更新投注单手续费字段（可单独更新任一字段）。"""
    updates = []
    params: list = []
    if entry_fee_usdc is not None:
        updates.append("entry_fee_usdc=?")
        params.append(round(entry_fee_usdc, 6))
    if exit_fee_usdc is not None:
        updates.append("exit_fee_usdc=?")
        params.append(round(exit_fee_usdc, 6))
    if fee_rate is not None:
        updates.append("fee_rate=?")
        params.append(round(fee_rate, 6))
    if not updates:
        return
    params.append(bet_id)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE paper_bets SET {', '.join(updates)} WHERE bet_id=?",
            params,
        )


def correct_closed_bet_exit_data(
    bet_id: str,
    exit_price: float,
    pnl: float,
    payout: float,
    condition_id: str = "",
    db_path: Path = None,
) -> bool:
    """用链上真实成交数据修正已关闭仓位的 exit_price / pnl / payout。

    仅更新已关闭（resolved_outcome IS NOT NULL）的记录。
    如果 condition_id 非空且原记录为空，一并回填。
    Returns:
        True 如果更新了至少一行。
    """
    with connect(db_path) as conn:
        is_win = 1 if pnl > 0 else 0
        if condition_id:
            cur = conn.execute(
                """UPDATE paper_bets
                   SET exit_price=?, pnl=?, payout=?, is_win=?,
                       condition_id=CASE WHEN condition_id IS NULL OR condition_id=''
                                         THEN ? ELSE condition_id END
                   WHERE bet_id=? AND resolved_outcome IS NOT NULL""",
                (
                    exit_price,
                    round(pnl, 4),
                    round(payout, 4),
                    is_win,
                    condition_id,
                    bet_id,
                ),
            )
        else:
            cur = conn.execute(
                """UPDATE paper_bets
                   SET exit_price=?, pnl=?, payout=?, is_win=?
                   WHERE bet_id=? AND resolved_outcome IS NOT NULL""",
                (exit_price, round(pnl, 4), round(payout, 4), is_win, bet_id),
            )
        return cur.rowcount > 0


# ------------------------------------------------------------------
# 持仓管理 — 加仓 / 部分止盈 / 部分卖出
# ------------------------------------------------------------------


def reduce_paper_bet_partial_sell(
    bet_id: str,
    sold_usdc: float,
    sold_pnl: float,
    remaining_usdc: float,
    db_path: Path = None,
) -> None:
    """部分卖出后缩减 DB 仓位，保留残余部分继续跟踪。

    已卖出部分的 P&L 记入 position_history；size_usdc 更新为残余金额。
    """
    import time as _time

    note = f"partial_sell={sold_usdc:.4f} pnl={sold_pnl:+.4f} @{_time.time():.0f}"
    with connect(db_path) as conn:
        # 追加到 position_history
        row = conn.execute(
            "SELECT position_history FROM paper_bets WHERE bet_id=?", (bet_id,)
        ).fetchone()
        old_hist = (row["position_history"] or "") if row else ""
        new_hist = f"{old_hist}|{note}" if old_hist else note

        conn.execute(
            """UPDATE paper_bets
               SET size_usdc=?, position_history=?
               WHERE bet_id=? AND resolved_outcome IS NULL""",
            (round(remaining_usdc, 4), new_hist, bet_id),
        )


def update_paper_bet_scale_in(
    bet_id: str,
    scale_in_count: int,
    last_scale_at: float,
    position_history: str,
    db_path: Path = None,
) -> None:
    """加仓后更新父订单追踪字段：递增 scale_in_count、记录时间和操作历史。"""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET scale_in_count=?, last_scale_at=?, position_history=?
            WHERE bet_id=? AND resolved_outcome IS NULL
            """,
            (
                scale_in_count,
                last_scale_at,
                position_history,
                bet_id,
            ),
        )


def get_open_bets_for_market(market_id: str, db_path: Path = None) -> list[dict]:
    """返回指定市场所有未解决的模拟仓位（含 entry 和 scale_in）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_bets WHERE market_id=? AND resolved_outcome IS NULL "
            "ORDER BY ts_entered",
            (market_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_open_entry_bets_for_market(market_id: str, db_path: Path = None) -> int:
    """返回特定市场当前未解决的入场（非加仓）仓位数量，用于扫描去重。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_bets "
            "WHERE market_id=? AND resolved_outcome IS NULL "
            "AND (bet_type='entry' OR bet_type IS NULL)",
            (market_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def get_open_paper_bets(db_path: Path = None) -> list[dict]:
    """返回所有尚未解决（resolved_outcome IS NULL）的模拟投注。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_bets WHERE resolved_outcome IS NULL ORDER BY ts_entered"
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------
# 卖出失败持久化计数（替代 main.py 模块级 dict，重启不丢失）
# ------------------------------------------------------------------


def increment_sell_fail_count(bet_id: str, db_path: Path = None) -> int:
    """+1 并返回当前 sell_fail_count。"""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE paper_bets SET sell_fail_count = COALESCE(sell_fail_count, 0) + 1 "
            "WHERE bet_id = ?",
            (bet_id,),
        )
        row = conn.execute(
            "SELECT sell_fail_count FROM paper_bets WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        return int(row["sell_fail_count"]) if row else 1


def reset_sell_fail_count(bet_id: str, db_path: Path = None) -> None:
    """重置 sell_fail_count 为 0（卖出成功或核销后调用）。"""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE paper_bets SET sell_fail_count = 0 WHERE bet_id = ?", (bet_id,)
        )


def reopen_ghost_position(bet_id: str, db_path: Path = None) -> bool:
    """重新打开幽灵仓位：清除关闭状态，使其重新进入监控循环。

    设置 ghost_reopened_at 时间戳，防止 check_and_update() 自解析立即回关
    （T179: 修复 reopen→self-resolve 无限循环 BUG）。

    Returns:
        True 如果更新了至少一行。
    """
    import time as _time

    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE paper_bets SET resolved_outcome = NULL, resolved_at = NULL, "
            "pnl = NULL, payout = NULL, exit_price = NULL, exit_type = NULL, "
            "sell_fail_count = 0, ghost_reopened_at = ? "
            "WHERE bet_id = ? AND resolved_outcome IS NOT NULL",
            (_time.time(), bet_id),
        )
        return cur.rowcount > 0


def get_recently_closed_bets(days: int = 7, db_path: Path = None) -> list[dict]:
    """返回最近 N 天内关闭的 paper_bets（用于幽灵仓位检测）。"""
    import time as _time

    cutoff = _time.time() - days * 86400
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_bets WHERE resolved_outcome IS NOT NULL "
            "AND resolved_at >= ? ORDER BY resolved_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_tracked_condition_ids(db_path: Path = None) -> set[str]:
    """返回 paper_bets 中所有已追踪的 condition_id（含已结算）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM paper_bets WHERE condition_id != ''"
        ).fetchall()
        return {r["condition_id"] for r in rows}


def update_condition_id(bet_id: str, condition_id: str, db_path: Path = None) -> bool:
    """为已有 paper_bet 补填 condition_id（修复旧记录无 condition_id 的问题）。

    仅当 condition_id 为空时才更新，避免覆盖已有值。
    Returns:
        True 如果更新了至少一行。
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE paper_bets SET condition_id = ? WHERE bet_id = ? AND (condition_id IS NULL OR condition_id = '')",
            (condition_id, bet_id),
        )
        return cur.rowcount > 0


def get_tracked_market_ids(db_path: Path = None) -> set[str]:
    """返回 paper_bets 中所有已追踪的 market_id（含已结算）。"""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT market_id FROM paper_bets").fetchall()
        return {r["market_id"] for r in rows}


def count_open_bets_for_market(market_id: str, db_path: Path = None) -> int:
    """返回特定市场当前未解决的模拟仓位数量。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_bets WHERE market_id=? AND resolved_outcome IS NULL",
            (market_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def count_open_bets_for_event(event_slug: str, db_path: Path = None) -> int:
    """返回指定 event_slug 的未平仓投注数量（防同事件多BIN散投）。"""
    if not event_slug:
        return 0
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_bets WHERE event_slug=? AND resolved_outcome IS NULL",
            (event_slug,),
        ).fetchone()
        return row["cnt"] if row else 0


def count_open_copy_bets_for_event(event_slug: str, db_path: Path = None) -> int:
    """返回指定 event_slug 来源于 copy_trade 的未平仓投注数量（跟投同事件去重）。"""
    if not event_slug:
        return 0
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_bets "
            "WHERE event_slug=? AND resolved_outcome IS NULL AND source='copy_trade'",
            (event_slug,),
        ).fetchone()
        return row["cnt"] if row else 0


def had_recent_stop_loss(
    market_id: str, cooldown_hours: float = 8.0, db_path: Path = None
) -> bool:
    """检查该市场在冷却期内是否发生过止损退出。若是，禁止重入。"""
    import time as _time

    cutoff = _time.time() - cooldown_hours * 3600
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM paper_bets
               WHERE market_id=? AND exit_type='stop_loss'
               AND resolved_at IS NOT NULL AND resolved_at > ?
               LIMIT 1""",
            (market_id, cutoff),
        ).fetchone()
    return row is not None


def had_recent_exit_by_condition(
    condition_id: str, cooldown_hours: float = 1.0, db_path: Path = None
) -> bool:
    """检查该 condition_id 在冷却期内是否有过任何主动卖出（profit_take/stop_loss/write_off）。

    Phase 36: 防止 Reconcile 重开或信号引擎在刚卖出后立即重买同一 condition_id。
    """
    import time as _time

    cutoff = _time.time() - cooldown_hours * 3600
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM paper_bets
               WHERE condition_id=?
               AND exit_type IN ('profit_take', 'stop_loss', 'write_off', 'manual_exit')
               AND resolved_at IS NOT NULL AND resolved_at > ?
               LIMIT 1""",
            (condition_id, cutoff),
        ).fetchone()
    return row is not None


def get_last_stop_loss_outcome(market_id: str, db_path: Path = None) -> str:
    """获取该市场最近一次止损的投注方向（YES/NO），无则返回空字符串。"""
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT outcome FROM paper_bets
               WHERE market_id=? AND exit_type='stop_loss'
               AND resolved_at IS NOT NULL
               ORDER BY resolved_at DESC LIMIT 1""",
            (market_id,),
        ).fetchone()
    return row["outcome"] if row else ""


def get_paper_summary(db_path: Path = None) -> dict:
    """返回模拟投注汇总统计。"""
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM paper_bets").fetchone()["c"]
        resolved = conn.execute(
            "SELECT COUNT(*) as c FROM paper_bets WHERE resolved_outcome IS NOT NULL"
        ).fetchone()["c"]
        wins = conn.execute(
            "SELECT COUNT(*) as c FROM paper_bets WHERE is_win=1"
        ).fetchone()["c"]
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) as s FROM paper_bets WHERE pnl IS NOT NULL"
        ).fetchone()["s"]
        total_staked = conn.execute(
            "SELECT COALESCE(SUM(size_usdc),0) as s FROM paper_bets"
        ).fetchone()["s"]
        return {
            "total": total,
            "resolved": resolved,
            "wins": wins,
            "losses": resolved - wins,
            "win_rate": wins / resolved if resolved > 0 else 0.0,
            "total_pnl": round(total_pnl, 4),
            "total_staked": round(total_staked, 4),
            "roi": round(total_pnl / total_staked, 4) if total_staked > 0 else 0.0,
        }


def had_any_exit_for_market(market_id: str, db_path: Path = None) -> bool:
    """T173: 检查该 market_id 是否曾有任何已退出的 entry 仓位。

    用于防止已止损/手动退出/已结算的市场被重复入场。
    仅检查 bet_type='entry'，不影响 scale_in 记录。
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM paper_bets
               WHERE market_id=? AND resolved_outcome IS NOT NULL
               AND bet_type='entry'
               LIMIT 1""",
            (market_id,),
        ).fetchone()
    return row is not None


def get_recent_exits_for_market(
    market_id: str, limit: int = 5, hours: float = 24.0, db_path: Path = None
) -> list[str]:
    """返回最近N次同市场已平仓的exit_type列表（用于熔断器判断连续止损）。"""
    import time as _time

    cutoff = _time.time() - hours * 3600
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT exit_type FROM paper_bets
               WHERE market_id=? AND resolved_outcome IS NOT NULL
               AND resolved_at IS NOT NULL AND resolved_at > ?
               ORDER BY resolved_at DESC LIMIT ?""",
            (market_id, cutoff, limit),
        ).fetchall()
    return [r["exit_type"] for r in rows if r["exit_type"]]


def get_metrics_summary(db_path: Path = None) -> dict:
    """返回关键指标聚合（止损率、命中率、按赛道分析）。"""
    with connect(db_path) as conn:
        resolved = conn.execute(
            "SELECT COUNT(*) as c FROM paper_bets WHERE resolved_outcome IS NOT NULL"
        ).fetchone()["c"]
        stop_losses = conn.execute(
            "SELECT COUNT(*) as c FROM paper_bets WHERE exit_type='stop_loss'"
        ).fetchone()["c"]
        profit_takes = conn.execute(
            "SELECT COUNT(*) as c FROM paper_bets WHERE exit_type='profit_take'"
        ).fetchone()["c"]
        # 按赛道统计
        cat_rows = conn.execute("""SELECT category,
                      COUNT(*) as total,
                      SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END) as wins,
                      COALESCE(SUM(pnl), 0) as pnl
               FROM paper_bets
               WHERE resolved_outcome IS NOT NULL
               GROUP BY category""").fetchall()
        by_category = {}
        for r in cat_rows:
            cat = r["category"] or "unknown"
            t = r["total"]
            w = r["wins"]
            by_category[cat] = {
                "total": t,
                "wins": w,
                "win_rate": round(w / t, 4) if t > 0 else 0.0,
                "pnl": round(r["pnl"], 4),
            }
        return {
            "resolved": resolved,
            "stop_loss_count": stop_losses,
            "stop_loss_ratio": (
                round(stop_losses / resolved, 4) if resolved > 0 else 0.0
            ),
            "profit_take_count": profit_takes,
            "by_category": by_category,
        }


def get_recent_resolved_exits(limit: int = 10, db_path: Path = None) -> list[str]:
    """返回最近N笔已结算投注的exit_type列表（用于止损率快速上升告警）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT exit_type FROM paper_bets
               WHERE resolved_outcome IS NOT NULL
               AND resolved_at IS NOT NULL
               ORDER BY resolved_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [r["exit_type"] for r in rows if r["exit_type"]]


def query_city_no_performance(db_path: Path = None) -> dict:
    """天气策略累计表现统计（覆盖所有天气相关投注）。

    Returns dict with keys: total, wins, win_rate, pnl, by_city, by_strategy.
    by_city: {city: {total, wins, pnl}}.
    by_strategy: {strategy_tag: {total, wins, pnl}}.
    """
    return query_city_no_performance_windowed(db_path=db_path, window_days=None)


def query_city_no_performance_windowed(
    db_path: Path = None, window_days: int | None = 30
) -> dict:
    """天气策略表现统计（支持时间窗口过滤）。M5-T251新增。

    Args:
        db_path: 数据库路径，None=默认
        window_days: 时间窗口（天），None=全量

    Returns:
        dict with keys: total, wins, win_rate, pnl, by_city, by_strategy.
        by_city: {city: {total, wins, pnl, win_rate}}.
    """
    window_clause = ""
    if window_days is not None:
        cutoff_ts = __import__("time").time() - window_days * 86400
        window_clause = f"AND ts_entered >= {cutoff_ts}"
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT question, strategy_tag, is_win, COALESCE(pnl, 0) as pnl
               FROM paper_bets
               WHERE (
                   strategy_tag IN ('city_no', 't268_city_no',
                                    'weather_double_no_left', 'weather_double_no_right',
                                    'weather_double_no_same',
                                    'weather_right_tail_left', 'weather_right_tail_right',
                                    'weather_right_tail_same', 'weather_yes_ambush')
                   OR (category = 'weather' AND strategy_tag IS NULL)
               )
               AND resolved_outcome IS NOT NULL
               {window_clause}"""
        ).fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r["is_win"])
    pnl = sum(r["pnl"] for r in rows)
    by_city: dict = {}
    by_strategy: dict = {}
    for r in rows:
        city = _extract_city_from_question(r["question"] or "")
        if city not in by_city:
            by_city[city] = {"total": 0, "wins": 0, "pnl": 0.0}
        by_city[city]["total"] += 1
        if r["is_win"]:
            by_city[city]["wins"] += 1
        by_city[city]["pnl"] += r["pnl"]

        stag = r["strategy_tag"] or "legacy"
        if stag not in by_strategy:
            by_strategy[stag] = {"total": 0, "wins": 0, "pnl": 0.0}
        by_strategy[stag]["total"] += 1
        if r["is_win"]:
            by_strategy[stag]["wins"] += 1
        by_strategy[stag]["pnl"] += r["pnl"]

    # 补充 win_rate 到 by_city
    for city_data in by_city.values():
        t = city_data["total"]
        city_data["win_rate"] = round(city_data["wins"] / t, 4) if t > 0 else 0.0
        city_data["pnl"] = round(city_data["pnl"], 4)

    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total, 4) if total > 0 else 0.0,
        "pnl": round(pnl, 4),
        "by_city": by_city,
        "by_strategy": by_strategy,
    }


def _extract_city_from_question(question: str) -> str:
    """Extract city name from weather question text for city NO grouping."""
    # Common patterns: "Will the high temperature in Tokyo ..."
    # "Will it be 30°F or higher in New York City ..."
    import re

    m = re.search(
        r"(?:temperature|temp|weather)\s+in\s+([A-Z][a-zA-Z\s]+?)(?:\s+on\s|\s+be\s|\s+exceed|\s+reach|\?)",
        question,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r"in\s+([A-Z][a-zA-Z\s]+?)(?:\s+on\s|\s+be\s|\s+exceed|\s+reach|\?|$)", question
    )
    if m:
        return m.group(1).strip()
    return "unknown"


def get_prediction_accuracy(
    recent_n: int = 50, db_path: Path = None
) -> dict[str, dict]:
    """
    预测准确率追踪（来源: solship PredictionScore 系统）。

    返回各赛道最近 N 笔已结算投注的:
      - total: 总数
      - wins: 胜利数
      - win_rate: 胜率
      - avg_ev: 平均 EV
      - avg_confidence: 平均置信度
      - calibration_error: |ai_probability - actual_win_rate| （校准误差）

    用途: 未来可基于此动态调整 shrinkage_factor
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT category, outcome, ai_probability, market_price,
                      confidence, ev, is_win, pnl
               FROM paper_bets
               WHERE resolved_outcome IS NOT NULL
               ORDER BY resolved_at DESC
               LIMIT ?""",
            (recent_n * 10,),  # 多取一些，按赛道分组后取各自 recent_n
        ).fetchall()

    # 按赛道分组
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        cat = r["category"] or "unknown"
        by_cat.setdefault(cat, []).append(dict(r))

    result: dict[str, dict] = {}
    overall_wins = 0
    overall_total = 0

    for cat, bets in by_cat.items():
        recent = bets[:recent_n]
        total = len(recent)
        wins = sum(1 for b in recent if b.get("is_win"))
        win_rate = wins / total if total > 0 else 0.0

        avg_ev = sum(b.get("ev", 0) or 0 for b in recent) / total if total > 0 else 0.0
        avg_conf = (
            sum(b.get("confidence", 0) or 0 for b in recent) / total
            if total > 0
            else 0.0
        )

        # 校准误差: AI 平均概率 vs 实际胜率
        avg_ai_prob = (
            sum(b.get("ai_probability", 0) or 0 for b in recent) / total
            if total > 0
            else 0.0
        )
        calibration_error = abs(avg_ai_prob - win_rate)

        result[cat] = {
            "total": total,
            "wins": wins,
            "win_rate": round(win_rate, 4),
            "avg_ev": round(avg_ev, 4),
            "avg_confidence": round(avg_conf, 4),
            "avg_ai_probability": round(avg_ai_prob, 4),
            "calibration_error": round(calibration_error, 4),
        }
        overall_wins += wins
        overall_total += total

    # 汇总
    result["_overall"] = {
        "total": overall_total,
        "wins": overall_wins,
        "win_rate": (
            round(overall_wins / overall_total, 4) if overall_total > 0 else 0.0
        ),
    }
    return result


# ------------------------------------------------------------------
# 自我进化系统 — CRUD
# ------------------------------------------------------------------


def insert_calibration_point(
    category: str,
    bin_index: int,
    ai_probability: float,
    actual_outcome: int,
    model_name: str = "",
    db_path: Path = None,
) -> None:
    """记录一条校准数据点（AI预测概率 vs 实际结果）。"""
    import time as _time

    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO calibration_data (ts, category, bin_index, ai_probability, actual_outcome, model_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _time.time(),
                category,
                bin_index,
                ai_probability,
                actual_outcome,
                model_name,
            ),
        )


def insert_model_performance(
    model_name: str,
    category: str,
    predicted_prob: float,
    actual_outcome: int,
    db_path: Path = None,
) -> None:
    """记录模型预测表现（用于模型权重自学习）。"""
    import time as _time

    sq_err = (predicted_prob - actual_outcome) ** 2
    abs_err = abs(predicted_prob - actual_outcome)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO model_performance (ts, model_name, category, predicted_prob, actual_outcome, squared_error, abs_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _time.time(),
                model_name,
                category,
                predicted_prob,
                actual_outcome,
                sq_err,
                abs_err,
            ),
        )


def insert_evolution_log(
    param_name: str,
    old_value: float,
    new_value: float,
    reason: str,
    data_points: int = 0,
    confidence: float = 0.0,
    db_path: Path = None,
) -> None:
    """记录一次进化调整事件。"""
    import time as _time

    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO evolution_log (ts, param_name, old_value, new_value, reason, data_points, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _time.time(),
                param_name,
                old_value,
                new_value,
                reason,
                data_points,
                confidence,
            ),
        )


def get_calibration_bins(
    category: str = "", min_samples: int = 5, db_path: Path = None
) -> list[dict]:
    """
    获取校准曲线数据（10个概率桶）。
    优先从 calibration_data 表读取；若为空则直接从 paper_bets 计算。
    返回: [{bin_index, total, wins, calibrated_rate, midpoint}, ...]
    """
    where = "WHERE category = ?" if category else ""
    params: list = [category] if category else []
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT bin_index,
                       COUNT(*) as total,
                       SUM(actual_outcome) as wins
                FROM calibration_data {where}
                GROUP BY bin_index
                ORDER BY bin_index""",
            params,
        ).fetchall()

    # 如果 calibration_data 为空，从 paper_bets 直接计算
    total_from_cal = sum(dict(r)["total"] for r in rows) if rows else 0
    if total_from_cal == 0:
        rows = _calibration_from_paper_bets(category, db_path)

    return _build_bin_list(rows, min_samples)


def _calibration_from_paper_bets(category: str = "", db_path: Path = None) -> list:
    """从 paper_bets 直接计算校准桶（当 calibration_data 为空时的 fallback）。"""
    cat_clause = "AND category = ?" if category else ""
    params: list = [category] if category else []
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT
                    MIN(CAST(ai_probability / 0.1 AS INTEGER), 9) AS bin_index,
                    COUNT(*) AS total,
                    SUM(CASE
                        WHEN outcome = 'YES' THEN is_win
                        ELSE 1 - is_win
                    END) AS wins
                FROM paper_bets
                WHERE resolved_outcome IS NOT NULL
                  AND ai_probability IS NOT NULL
                  {cat_clause}
                GROUP BY bin_index
                ORDER BY bin_index""",
            params,
        ).fetchall()
    return rows


def _build_bin_list(rows, min_samples: int = 5) -> list[dict]:
    """将原始桶行转为标准10桶列表。"""
    bins = []
    for i in range(10):
        midpoint = (i + 0.5) / 10.0
        row = next((dict(r) for r in rows if r["bin_index"] == i), None)
        if row and row["total"] >= min_samples:
            cal_rate = (row["wins"] + 1) / (row["total"] + 2)
            bins.append(
                {
                    "bin_index": i,
                    "total": row["total"],
                    "wins": row["wins"],
                    "calibrated_rate": round(cal_rate, 4),
                    "midpoint": midpoint,
                }
            )
        else:
            total = row["total"] if row else 0
            wins = row["wins"] if row else 0
            bins.append(
                {
                    "bin_index": i,
                    "total": total,
                    "wins": wins,
                    "calibrated_rate": round(midpoint, 4),
                    "midpoint": midpoint,
                }
            )
    return bins


def get_model_stats(recent_n: int = 200, db_path: Path = None) -> dict[str, dict]:
    """
    获取各模型的预测表现统计。
    优先从 model_performance 表读取；若为空则从 paper_bets.ai_models 聚合。
    返回: {model_name: {total, mae, brier, weight}, ...}
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT model_name,
                      COUNT(*) as total,
                      AVG(abs_error) as mae,
                      AVG(squared_error) as brier
               FROM (SELECT * FROM model_performance ORDER BY ts DESC LIMIT ?)
               GROUP BY model_name""",
            (recent_n,),
        ).fetchall()

    result = {}
    for r in rows:
        mae = r["mae"] or 0.5
        weight = 1.0 / (1.0 + mae * 10)
        result[r["model_name"]] = {
            "total": r["total"],
            "mae": round(mae, 4),
            "brier": round(r["brier"] or 0, 4),
            "weight": round(weight, 4),
        }

    # fallback: 从 paper_bets 聚合模型使用统计
    if not result:
        result = _model_stats_from_paper_bets(recent_n, db_path)

    return result


def _model_stats_from_paper_bets(
    recent_n: int = 200, db_path: Path = None
) -> dict[str, dict]:
    """从 paper_bets.ai_models 聚合模型表现（fallback）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT ai_models, ai_probability, is_win, outcome
               FROM paper_bets
               WHERE resolved_outcome IS NOT NULL
                 AND ai_probability IS NOT NULL
                 AND ai_models IS NOT NULL AND ai_models != ''
               ORDER BY resolved_at DESC
               LIMIT ?""",
            (recent_n,),
        ).fetchall()

    from collections import defaultdict

    stats: dict[str, list] = defaultdict(list)
    for r in rows:
        ai_models = r["ai_models"] or ""
        ai_prob = r["ai_probability"] or 0.5
        is_win = r["is_win"] or 0
        outcome_side = r["outcome"] or "YES"
        # 转换为 P(YES) 方向
        if outcome_side == "NO":
            actual = 1 - is_win
            pred = 1.0 - ai_prob
        else:
            actual = is_win
            pred = ai_prob
        for model_name in ai_models.split(","):
            model_name = model_name.strip()
            if model_name:
                stats[model_name].append((pred, actual))

    result = {}
    for name, pairs in stats.items():
        n = len(pairs)
        mae = sum(abs(p - a) for p, a in pairs) / n if n else 0.5
        brier = sum((p - a) ** 2 for p, a in pairs) / n if n else 0.5
        weight = 1.0 / (1.0 + mae * 10)
        result[name] = {
            "total": n,
            "mae": round(mae, 4),
            "brier": round(brier, 4),
            "weight": round(weight, 4),
        }
    return result


def get_evolution_history(limit: int = 50, db_path: Path = None) -> list[dict]:
    """获取最近的进化调整历史。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM evolution_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_resolved_for_calibration(
    recent_n: int = 500, db_path: Path = None
) -> list[dict]:
    """获取最近已结算的投注（用于进化系统分析）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT bet_id, category, outcome, ai_probability, market_price,
                      confidence, ev, is_win, pnl, size_usdc, resolved_at,
                      ai_models, exit_type
               FROM paper_bets
               WHERE resolved_outcome IS NOT NULL AND ai_probability IS NOT NULL
               ORDER BY resolved_at DESC
               LIMIT ?""",
            (recent_n,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_settled_pnl(db_path: Path = None) -> float:
    """返回今日（UTC）已结算投注的总实现盈亏（负值=亏损）。

    专供 RiskMonitor 使用，避免将「下注部署」误算为每日亏损。
    只统计 resolved_outcome IS NOT NULL 的已结算仓位。
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()  # e.g. '2026-03-23'
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS total_pnl
            FROM paper_bets
            WHERE date(resolved_at, 'unixepoch') = ?
              AND resolved_outcome IS NOT NULL
              AND pnl IS NOT NULL
            """,
            (today,),
        ).fetchone()
    return float(row["total_pnl"]) if row else 0.0


def get_total_realized_loss(db_path: Path = None) -> float:
    """返回历史全部已结算失败投注的总亏损（正数，单位 USDC）。

    专供 RiskMonitor 使用，替代由余额变动推算的 _cum_loss，
    消除「开仓部署资金」被误计为累计亏损的问题。
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS total_loss
            FROM paper_bets
            WHERE resolved_outcome IS NOT NULL
              AND pnl IS NOT NULL
            """,
        ).fetchone()
    return float(row["total_loss"]) if row else 0.0


def get_total_realized_pnl(db_path: Path = None) -> float:
    """返回历史全部已结算投注的净盈亏（正值=盈利，负值=亏损，单位 USDC）。

    专供 RiskMonitor 提现告警使用，基于实际交易盈亏而非余额差，
    避免将用户充值/提现误判为交易利润。
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS net_pnl
            FROM paper_bets
            WHERE resolved_outcome IS NOT NULL
              AND pnl IS NOT NULL
            """,
        ).fetchone()
    return float(row["net_pnl"]) if row else 0.0


# ------------------------------------------------------------------
# Phase 57: trailing 止盈状态持久化 + RiskMonitor 暂停状态持久化
# ------------------------------------------------------------------


def update_trailing_state(
    bet_id: str,
    peak_price: float | None = None,
    floor_price: float | None = None,
    db_path: Path = None,
) -> None:
    """更新投注的 trailing 止盈状态（peak_price / floor_price），重启后可恢复。"""
    updates: list[str] = []
    params: list = []
    if peak_price is not None:
        updates.append("trailing_peak_price = ?")
        params.append(peak_price)
    if floor_price is not None:
        updates.append("trailing_floor_price = ?")
        params.append(floor_price)
    if not updates:
        return
    params.append(bet_id)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE paper_bets SET {', '.join(updates)} WHERE bet_id = ?",
            params,
        )


def get_open_bets_trailing_state(db_path: Path = None) -> list[dict]:
    """读取所有未结算投注的 trailing + CertaintyHold 状态，用于 OutcomeTracker 重启恢复。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT bet_id, trailing_peak_price, trailing_floor_price, certainty_hold_since "
            "FROM paper_bets WHERE resolved_outcome IS NULL"
        ).fetchall()
    return [
        {
            "bet_id": r["bet_id"],
            "peak_price": r["trailing_peak_price"],
            "floor_price": r["trailing_floor_price"],
            "certainty_hold_since": r["certainty_hold_since"],
        }
        for r in rows
    ]


def update_certainty_hold_since(
    bet_id: str, hold_since: str | None, db_path: Path = None
) -> None:
    """更新投注的 CertaintyHold 首次持有时间（ISO UTC），重启后可恢复。
    传入 None 可清除记录（超时卖出后清理）。"""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE paper_bets SET certainty_hold_since = ? WHERE bet_id = ?",
            (hold_since, bet_id),
        )


def save_pause_state(key: str, value: str, db_path: Path = None) -> None:
    """持久化风控暂停状态键值对（UPSERT）。"""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pause_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, ts),
        )


def get_all_pause_state(db_path: Path = None) -> dict[str, str]:
    """读取所有风控暂停状态 KV，返回 {key: value}。"""
    try:
        with connect(db_path) as conn:
            rows = conn.execute("SELECT key, value FROM pause_state").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


# ------------------------------------------------------------------
# evolution_overrides CRUD（T152 — 持久化进化参数）
# ------------------------------------------------------------------


def load_evolution_overrides(db_path: Path = None) -> dict[str, float]:
    """加载所有已持久化的进化参数覆盖值。返回 {param_name: value}。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT param_name, value FROM evolution_overrides"
        ).fetchall()
    return {row["param_name"]: float(row["value"]) for row in rows}


def save_evolution_override(
    param_name: str,
    new_value: float,
    reason: str = "",
    old_value: float = None,
    db_path: Path = None,
) -> None:
    """保存/更新一个进化参数覆盖值，并记录变更历史。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        # 1. 记录历史
        conn.execute(
            """INSERT INTO evolution_overrides_history
               (param_name, old_value, new_value, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (param_name, old_value, new_value, reason, now),
        )
        # 2. UPSERT 当前值
        conn.execute(
            """INSERT INTO evolution_overrides (param_name, value, updated_at, reason)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(param_name) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   reason = excluded.reason""",
            (param_name, new_value, now, reason),
        )


def rollback_evolution_override(
    param_name: str, db_path: Path = None
) -> Optional[float]:
    """回滚一个进化参数到上一个值。返回回滚后的值，无历史则返回 None。"""
    from datetime import datetime, timezone

    with connect(db_path) as conn:
        # 取最近一条历史（当前值对应的那条），然后取其 old_value
        row = conn.execute(
            """SELECT old_value FROM evolution_overrides_history
               WHERE param_name = ? ORDER BY id DESC LIMIT 1""",
            (param_name,),
        ).fetchone()
        if row is None or row["old_value"] is None:
            return None
        rolled_back_value = float(row["old_value"])
        now = datetime.now(timezone.utc).isoformat()
        # 记录回滚操作本身到历史
        current_row = conn.execute(
            "SELECT value FROM evolution_overrides WHERE param_name = ?",
            (param_name,),
        ).fetchone()
        current_val = float(current_row["value"]) if current_row else None
        conn.execute(
            """INSERT INTO evolution_overrides_history
               (param_name, old_value, new_value, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (param_name, current_val, rolled_back_value, "ROLLBACK", now),
        )
        # 更新当前值
        conn.execute(
            """INSERT INTO evolution_overrides (param_name, value, updated_at, reason)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(param_name) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   reason = excluded.reason""",
            (param_name, rolled_back_value, now, "ROLLBACK"),
        )
    return rolled_back_value


def list_evolution_override_history(
    param_name: str = None, limit: int = 50, db_path: Path = None
) -> list[dict]:
    """查询进化参数变更历史。param_name=None 则返回所有参数的历史。"""
    with connect(db_path) as conn:
        if param_name:
            rows = conn.execute(
                """SELECT id, param_name, old_value, new_value, reason, created_at
                   FROM evolution_overrides_history
                   WHERE param_name = ?
                   ORDER BY id DESC LIMIT ?""",
                (param_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, param_name, old_value, new_value, reason, created_at
                   FROM evolution_overrides_history
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# T239: 止损事件结构化日志
# ------------------------------------------------------------------


def log_stop_loss_event(
    bet_id: str,
    question: str,
    category: str,
    trigger_type: str,
    entry_price: float,
    current_price: float,
    return_pct: float,
    threshold_pct: float,
    size_usdc: float = 0.0,
    hours_to_expiry: float | None = None,
    pnl: float | None = None,
    conditions: dict | None = None,
    context: dict | None = None,
    db_path: Path = None,
) -> None:
    """记录一次止损触发事件到 stop_loss_events 表。"""
    import json as _json
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    ts = _dt.now(_tz.utc).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO stop_loss_events
               (timestamp, bet_id, question, category, trigger_type,
                entry_price, current_price, exit_price, return_pct,
                hours_to_expiry, threshold_pct, size_usdc, pnl,
                conditions_json, context_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                bet_id,
                question[:200] if question else "",
                category or "",
                trigger_type,
                round(entry_price, 6),
                round(current_price, 6),
                round(current_price, 6),  # exit_price = current_price at trigger time
                round(return_pct, 6),
                round(hours_to_expiry, 2) if hours_to_expiry is not None else None,
                round(threshold_pct, 4),
                round(size_usdc, 4),
                round(pnl, 4) if pnl is not None else None,
                _json.dumps(conditions or {}, ensure_ascii=False),
                _json.dumps(context or {}, ensure_ascii=False),
            ),
        )


def get_stop_loss_events(
    limit: int = 100,
    category: str = "",
    trigger_type: str = "",
    days: int = 0,
    db_path: Path = None,
) -> list[dict]:
    """查询止损事件，支持按赛道、触发类型、天数过滤。"""
    where_clauses: list[str] = []
    params: list = []
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    if trigger_type:
        where_clauses.append("trigger_type = ?")
        params.append(trigger_type)
    if days > 0:
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()
        where_clauses.append("timestamp >= ?")
        params.append(cutoff)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    params.append(limit)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT * FROM stop_loss_events
                {where_sql}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_stop_loss_summary(days: int = 7, db_path: Path = None) -> dict:
    """生成止损事件汇总统计（最近N天）。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()
    with connect(db_path) as conn:
        # 按 trigger_type 统计
        by_trigger = conn.execute(
            """SELECT trigger_type, COUNT(*) as cnt,
                      ROUND(AVG(return_pct), 4) as avg_return,
                      ROUND(SUM(COALESCE(pnl, 0)), 4) as total_pnl
               FROM stop_loss_events
               WHERE timestamp >= ?
               GROUP BY trigger_type
               ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()

        # 按 category 统计
        by_category = conn.execute(
            """SELECT category, COUNT(*) as cnt,
                      ROUND(AVG(return_pct), 4) as avg_return,
                      ROUND(SUM(COALESCE(pnl, 0)), 4) as total_pnl
               FROM stop_loss_events
               WHERE timestamp >= ?
               GROUP BY category
               ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()

        total = conn.execute(
            """SELECT COUNT(*) as cnt,
                      ROUND(SUM(COALESCE(pnl, 0)), 4) as total_pnl
               FROM stop_loss_events
               WHERE timestamp >= ?""",
            (cutoff,),
        ).fetchone()

    return {
        "days": days,
        "total_events": total["cnt"] if total else 0,
        "total_pnl": total["total_pnl"] if total else 0,
        "by_trigger": [dict(r) for r in by_trigger],
        "by_category": [dict(r) for r in by_category],
    }


# connect() 已从 database.py 导入，不再在此定义
# 保留向后兼容：from src.utils.db import connect 仍然有效
