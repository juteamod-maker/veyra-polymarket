-- Veyra.trade PostgreSQL Schema Initialization
-- 用途: VPS 生产环境和本地开发环境的数据库 Schema 初始化
-- 运行: psql $DATABASE_URL -f scripts/init_postgres_v2.sql
--
-- 注意: V2 仍使用 SQLite 作为当前存储层（Phase 3 再迁移到 PostgreSQL）
-- 本文件是 Phase 3 PostgreSQL 迁移的前置准备，确认数据库可连接

-- 验证连接
SELECT version() AS "PostgreSQL Version";

-- 创建扩展（如果有需要）
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ───────────────────────────────────────────────────────────
-- 健康检查表（确认 V2 系统对 PostgreSQL 的基本读写能力）
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS v2_system_health (
    id          SERIAL PRIMARY KEY,
    check_time  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    component   VARCHAR(64) NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'ok',
    notes       TEXT
);

-- 插入初始化记录
INSERT INTO v2_system_health (component, status, notes)
VALUES ('postgres_init', 'ok', 'Schema initialized by init_postgres_v2.sql');

SELECT * FROM v2_system_health;
