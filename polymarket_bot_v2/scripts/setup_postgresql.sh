#!/usr/bin/env bash
# ============================================================
# setup_postgresql.sh — PostgreSQL 初始化脚本
# 用法: sudo bash scripts/setup_postgresql.sh
# ============================================================
set -euo pipefail

PG_DB="${PG_DATABASE:-polymarket_v2}"
PG_USER="${PG_USER:-polymarket}"
PG_PASS="${PG_PASSWORD:-polymarket_secret_$(openssl rand -hex 8)}"
SCHEMA_FILE="$(dirname "$0")/init_pg_schema.sql"

echo "========================================"
echo "  VagusPoly V2 — PostgreSQL 初始化"
echo "========================================"
echo "数据库: $PG_DB"
echo "用户:   $PG_USER"
echo ""

# 1. 创建用户（如果不存在）
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1; then
    echo "[OK] 用户 $PG_USER 已存在"
else
    sudo -u postgres psql -c "CREATE USER $PG_USER WITH PASSWORD '$PG_PASS'"
    echo "[OK] 用户 $PG_USER 已创建，密码: $PG_PASS"
    echo ""
    echo "⚠️  请将密码保存到 .env 文件:"
    echo "    PG_PASSWORD=$PG_PASS"
fi

# 2. 创建数据库（如果不存在）
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'" | grep -q 1; then
    echo "[OK] 数据库 $PG_DB 已存在"
else
    sudo -u postgres psql -c "CREATE DATABASE $PG_DB OWNER $PG_USER"
    echo "[OK] 数据库 $PG_DB 已创建"
fi

# 3. 授权
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $PG_DB TO $PG_USER"
echo "[OK] 权限已授予"

# 4. 授予 schema 权限（确保 polymarket 用户可创建表）
sudo -u postgres psql -d "$PG_DB" -c "GRANT ALL ON SCHEMA public TO $PG_USER"
echo "[OK] Schema public 权限已授予"

# 5. 运行 schema 初始化（以 polymarket 用户身份，确保表 owner 正确）
if [ -f "$SCHEMA_FILE" ]; then
    PGPASSWORD="$PG_PASS" psql -h localhost -U "$PG_USER" -d "$PG_DB" -f "$SCHEMA_FILE"
    echo "[OK] Schema 初始化完成（表 owner = $PG_USER）"
else
    echo "[WARN] Schema 文件不存在: $SCHEMA_FILE"
    echo "       请手动运行: psql -d $PG_DB -f scripts/init_pg_schema.sql"
fi

# 5b. 修复已有表的 ownership（如果之前以 postgres 创建过）
sudo -u postgres psql -d "$PG_DB" -c "
DO \$\$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tableowner != '$PG_USER'
  LOOP
    EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO $PG_USER';
  END LOOP;
  FOR r IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema='public'
  LOOP
    EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequence_name) || ' OWNER TO $PG_USER';
  END LOOP;
END\$\$;
GRANT ALL ON ALL TABLES IN SCHEMA public TO $PG_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $PG_USER;
"
echo "[OK] 表/序列 ownership 已修复"

# 6. 配置 pg_hba.conf 允许本地密码认证
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if ! grep -q "$PG_USER" "$PG_HBA" 2>/dev/null; then
    echo "# VagusPoly V2 — local password auth" | sudo tee -a "$PG_HBA" > /dev/null
    echo "local   $PG_DB   $PG_USER   md5" | sudo tee -a "$PG_HBA" > /dev/null
    echo "host    $PG_DB   $PG_USER   127.0.0.1/32   md5" | sudo tee -a "$PG_HBA" > /dev/null
    echo "host    $PG_DB   $PG_USER   ::1/128        md5" | sudo tee -a "$PG_HBA" > /dev/null
    sudo systemctl reload postgresql
    echo "[OK] pg_hba.conf 已更新并重载"
fi

echo ""
echo "========================================"
echo "  初始化完成!"
echo "========================================"
echo ""
echo "请在 .env 中配置以下变量:"
echo "  PG_HOST=localhost"
echo "  PG_PORT=5432"
echo "  PG_DATABASE=$PG_DB"
echo "  PG_USER=$PG_USER"
echo "  PG_PASSWORD=<上面生成的密码>"
echo ""
echo "或使用 DATABASE_URL:"
echo "  DATABASE_URL=postgresql://$PG_USER:<密码>@localhost:5432/$PG_DB"
