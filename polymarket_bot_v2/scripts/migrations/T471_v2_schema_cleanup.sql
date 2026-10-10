-- T471 — V2 schema cleanup migration (2026-04-27).
--
-- Goal: After the 2026-04-28 CLOB v2 cutover, remove tables that only exist
-- to support deprecated V1 strategy / points / referral / fees subsystems.
-- Preserve everything that the manual-user dashboard, copytrader sidecar,
-- and event_translations cache rely on.
--
-- Idempotent. Safe to re-run. Wrapped in a transaction; errors roll back.
--
-- Apply on VPS via:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/T471_v2_schema_cleanup.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Drop unused V1 / strategy / points / referral / fees tables.
--    Keep: users, user_sessions, event_translations, vault_transactions,
--          wallet_bindings, user_pm_profiles, user_pm_positions_snapshots,
--          user_pm_equity_snapshots, user_pm_activity_snapshots,
--          user_runtime_controls, copy_tasks, schema_version, balance_log.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS evolution_log CASCADE;
DROP TABLE IF EXISTS evolution_overrides CASCADE;
DROP TABLE IF EXISTS evolution_overrides_history CASCADE;
DROP TABLE IF EXISTS smart_entry_cache CASCADE;
DROP TABLE IF EXISTS calibration_data CASCADE;
DROP TABLE IF EXISTS model_performance CASCADE;
DROP TABLE IF EXISTS signals CASCADE;
DROP TABLE IF EXISTS scans CASCADE;
DROP TABLE IF EXISTS bet_price_series CASCADE;
DROP TABLE IF EXISTS price_move_log CASCADE;
DROP TABLE IF EXISTS stop_loss_events CASCADE;
DROP TABLE IF EXISTS news_cache CASCADE;
DROP TABLE IF EXISTS pause_state CASCADE;

-- Points / referral / fees sub-systems were never wired into the V2 manual
-- user flow. Drop them and any orphan history.
DROP TABLE IF EXISTS point_rules CASCADE;
DROP TABLE IF EXISTS point_transactions CASCADE;
DROP TABLE IF EXISTS user_points CASCADE;
DROP TABLE IF EXISTS referral_codes CASCADE;
DROP TABLE IF EXISTS referral_rewards CASCADE;
DROP TABLE IF EXISTS user_referrals CASCADE;
DROP TABLE IF EXISTS user_subscriptions CASCADE;
DROP TABLE IF EXISTS fee_config CASCADE;
DROP TABLE IF EXISTS fee_discount_tiers CASCADE;
DROP TABLE IF EXISTS fee_transactions CASCADE;

-- Tag tables only used by V1 ranking dashboards.
DROP TABLE IF EXISTS market_tags CASCADE;
DROP TABLE IF EXISTS bet_tags CASCADE;

-- V1 paper bet ledger and CLOB orders mirror — no longer used by V2 runtime.
DROP TABLE IF EXISTS paper_bets CASCADE;
DROP TABLE IF EXISTS orders CASCADE;

-- ---------------------------------------------------------------------------
-- 2. Truncate non-admin users.
--    Spec (T471): "所有目前系统中的用户除了管理员其他都可以清理".
--    Admin and super_admin rows are preserved; everything else (and dependent
--    rows in user_sessions, wallet_bindings, etc.) is purged via CASCADE.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='users' AND column_name='role') THEN
        EXECUTE $sql$
            DELETE FROM users
             WHERE COALESCE(role, '') NOT IN ('admin', 'super_admin')
        $sql$;
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 3. Drop legacy V1-only columns on users (best-effort; only if present).
--    These columns were added during V1 single-bot iteration and are no
--    longer referenced anywhere in the V2 codebase.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    col TEXT;
BEGIN
    FOR col IN SELECT unnest(ARRAY[
        'kucoin_api_key', 'kucoin_api_secret', 'kucoin_passphrase',
        'binance_api_key', 'binance_api_secret',
        'okx_api_key', 'okx_api_secret', 'okx_passphrase',
        'bot_strategy_preset', 'auto_trade_enabled', 'auto_trade_paused_at',
        'risk_score', 'last_evolution_run', 'evolution_score',
        'referral_code', 'referrer_user_id'
    ]) LOOP
        EXECUTE format(
            'ALTER TABLE users DROP COLUMN IF EXISTS %I',
            col
        );
    END LOOP;
END$$;

-- ---------------------------------------------------------------------------
-- 4. Schema version stamp.
-- ---------------------------------------------------------------------------
INSERT INTO schema_version (version)
VALUES (471)
ON CONFLICT DO NOTHING;

COMMIT;

-- VACUUM ANALYZE outside the txn to reclaim space.
VACUUM ANALYZE;
