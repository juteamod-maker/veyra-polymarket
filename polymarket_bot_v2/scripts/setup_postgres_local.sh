#!/bin/bash
# setup_postgres_local.sh — 本地开发环境 PostgreSQL 初始化脚本
# 用法: sudo bash scripts/setup_postgres_local.sh
#
# 功能:
#   1. 创建 vaguspoly_v2_dev 数据库（本地开发用）
#   2. 创建 vaguspoly 数据库用户
#   3. 授权
#
# 前提: PostgreSQL 16+ 已安装并运行（pg_isready 检查通过）

set -e

DB_USER="vaguspoly"
DB_NAME_DEV="vaguspoly_v2_dev"
DB_PASSWORD="vaguspoly_dev_local"

echo "[setup_postgres_local] 检查 PostgreSQL 服务..."
if ! pg_isready -q; then
  echo "ERROR: PostgreSQL 未运行。请先启动: sudo systemctl start postgresql"
  exit 1
fi

echo "[setup_postgres_local] 创建用户 ${DB_USER}（如已存在则跳过）..."
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" \
  | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"

echo "[setup_postgres_local] 创建开发数据库 ${DB_NAME_DEV}（如已存在则跳过）..."
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME_DEV}'" \
  | grep -q 1 \
  || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME_DEV}"

echo "[setup_postgres_local] 授权..."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME_DEV} TO ${DB_USER};"
sudo -u postgres psql -d "${DB_NAME_DEV}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER};"

echo ""
echo "✅ 本地 PostgreSQL 初始化完成！"
echo ""
echo "连接信息（写入 V2 .env 中的 DATABASE_URL）:"
echo "  DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME_DEV}"
echo ""
echo "验证连接:"
echo "  psql postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME_DEV} -c '\l'"
