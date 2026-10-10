#!/usr/bin/env bash

set -euo pipefail

VPS_HOST="${VPS_HOST:-}"
VPS_DIR="/opt/polymarket_bot_v2"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PACK_FILE="/tmp/vaguspoly_v2_deploy.tar.gz"
DASHBOARD_SERVICE="polymarket-dashboard-v2"
COPYTRADER_SERVICE="polymarket-copytrader-v2"
ADMIN_V3_SERVICE="polymarket-admin-v3"

# T901: Admin V3 是私有模块，公开仓库不包含它。缺失时自动跳过所有 admin 相关
# 步骤，使同一份部署脚本在开源发行版（仅前台）中依然可用；存在时行为与原先一致。
HAS_ADMIN_V3=0
if [ -d "$(cd "$(dirname "$0")/.." && pwd)/src/services/admin_v3" ]; then
    HAS_ADMIN_V3=1
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[V2-DEPLOY]${NC} $*"; }
warn() { echo -e "${YELLOW}[V2-WARN]${NC} $*"; }
err()  { echo -e "${RED}[V2-ERROR]${NC} $*"; exit 1; }

# Deployment target is injected via the VPS_HOST environment variable.
# It is intentionally NOT hardcoded so the public repository never carries
# an internal SSH alias.
require_host() {
    if [ -z "$VPS_HOST" ]; then
        err "VPS_HOST is not set. Export it before deploying, e.g.: export VPS_HOST=my-vps"
    fi
}

check_syntax() {
    log "Running syntax checks"
    cd "$LOCAL_DIR"
    python3 -m compileall -q src/ scripts/web_dashboard.py scripts/dashboard_routes || \
        err "Python syntax check failed"
    python3 scripts/ci_guard.py --frontend-only || err "ci_guard failed (Jinja brace typo)"
    log "Running production JS no-comment gate (T924)"
    local _js_files
    _js_files=$(find scripts/frontend/static/js scripts/static/js -maxdepth 5 -name '*.js' -type f \
        \! -name '*.bak.*' \
        \! -name 'polymarket_sdk_bundle.js' \
        \! -path '*/vendor/*' \
        \! -path '*/node_modules/*' \
        2>/dev/null | sort)
    if [ -z "$_js_files" ]; then
        warn "No production JS files matched for no-comment gate"
    else
        # shellcheck disable=SC2086
        python3 scripts/check_no_js_comments.py $_js_files || \
            err "T924 gate FAILED: production JS contains comments. Run scripts/strip_js_comments.py --write before deploy."
    fi
}

build_admin_v3_spa() {
    local spa_dir="$LOCAL_DIR/admin_v3_spa"
    if [ ! -f "$spa_dir/package.json" ]; then
        warn "admin_v3_spa/package.json not found, skip SPA build"
        return
    fi
    if ! command -v npm >/dev/null 2>&1; then
        err "npm is required to build Admin V3 SPA"
    fi
    log "Building Admin V3 SPA dist"
    (
        cd "$spa_dir"
        npm ci --silent --no-audit --no-fund
        npm run build
    )
}

# ---------------------------------------------------------------------------
# 打包（T865：严格剔除所有非生产文件，禁止 test/bak/md/example/_prod_sync 等流入 VPS）
# ---------------------------------------------------------------------------
pack() {
    log "Packing workspace"
    cd "$LOCAL_DIR"
    tar czf "$PACK_FILE" \
        --exclude='.venv' \
        --exclude='logs' \
        --exclude='data' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='*.db' \
        --exclude='*.egg-info' \
        --exclude='.env' \
        --exclude='.env.example' \
        --exclude='.env.*.example' \
        --exclude='.gitignore' \
        --exclude='.git' \
        --exclude='.vscode' \
        --exclude='.idea' \
        --exclude='.DS_Store' \
        --exclude='.pytest_cache' \
        --exclude='.mypy_cache' \
        --exclude='.ruff_cache' \
        --exclude='.prettierignore' \
        --exclude='pytest.ini' \
        --exclude='tests' \
        --exclude='_archive_v1' \
        --exclude='_prod_sync_*' \
        --exclude='*.md' \
        --exclude='*.bak' \
        --exclude='*.bak.*' \
        --exclude='*.bak_*' \
        --exclude='*.backup' \
        --exclude='*.swp' \
        --exclude='*.tmp' \
        --exclude='*.orig' \
        --exclude='admin_v3_spa/node_modules' \
        --exclude='scripts/frontend/static/js/runtime/polymarket_sdk_bundle/node_modules' \
        --exclude='scripts/frontend/static/js/runtime/polymarket_sdk_bundle/package-lock.json' \
        --exclude='scripts/frontend/static/js/runtime/polymarket_sdk_bundle/src' \
        --exclude='scripts/frontend/static/js/runtime/polymarket_sdk_bundle/package.json' \
        --exclude='scripts/frontend/static/js/runtime/polymarket_sdk_bundle/.gitignore' \
        --exclude='admin_v3_spa/src' \
        --exclude='admin_v3_spa/index.html' \
        --exclude='admin_v3_spa/package.json' \
        --exclude='admin_v3_spa/package-lock.json' \
        --exclude='admin_v3_spa/tsconfig.json' \
        --exclude='admin_v3_spa/tsconfig.app.json' \
        --exclude='admin_v3_spa/tsconfig.node.json' \
        --exclude='admin_v3_spa/vite.config.ts' \
        --exclude='admin_v3_spa/uno.config.ts' \
        --exclude='config/backups' \
        --exclude='config/config.json' \
        --exclude='config/copytrader_v2.json' \
        --exclude='config/config.json.vps*' \
        --exclude='config/config.json.local_backup' \
        --exclude='scripts/run_tests.sh' \
        --exclude='scripts/setup_postgres_local.sh' \
        --exclude='scripts/setup_postgresql.sh' \
        --exclude='scripts/ui_design_pipeline.sh' \
        --exclude='scripts/frontend/.design-pipeline' \
        .
    local size
    size=$(du -sh "$PACK_FILE" | cut -f1)
    log "Packed $PACK_FILE ($size)"
    verify_pack
}

# ---------------------------------------------------------------------------
# T865：包内容审计 — 部署前阻断任何冗余/非生产文件
# ---------------------------------------------------------------------------
verify_pack() {
    log "Verifying pack contents (T865 cleanliness gate)"
    local listing
    listing=$(tar tzf "$PACK_FILE")
    local bad
    bad=$(echo "$listing" | grep -E '(^|/)(tests/|pytest\.ini$|\.pytest_cache/|_prod_sync_|_archive_v1/|run_tests\.sh$|setup_postgres_local\.sh$|setup_postgresql\.sh$|ui_design_pipeline\.sh$|\.design-pipeline/|\.env\.example$|\.prettierignore$)|\.bak($|[._])|\.md$|\.swp$|\.orig$|\.backup$' || true)
    if [ -n "$bad" ]; then
        echo "$bad" | sed 's/^/[V2-DEPLOY-BAD] /'
        err "Pack contains forbidden non-production files (see above). Aborting deploy."
    fi
    log "Pack clean — no forbidden files detected"
}

# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
upload() {
    log "Uploading package"
    scp "$PACK_FILE" "${VPS_HOST}:/tmp/vaguspoly_v2_deploy.tar.gz"
}

install_on_vps() {
    log "Installing on VPS"
    ssh "$VPS_HOST" bash <<EOF
set -euo pipefail

VPS_DIR="$VPS_DIR"
DASHBOARD_SERVICE="$DASHBOARD_SERVICE"
COPYTRADER_SERVICE="$COPYTRADER_SERVICE"
ADMIN_V3_SERVICE="$ADMIN_V3_SERVICE"
CFG_MAIN_TMP="/tmp/v2_config_json_preserve.bak"
CFG_COPY_TMP="/tmp/v2_copytrader_json_preserve.bak"
FRONTEND_BANNERS_TMP="/tmp/v2_frontend_banners_preserve.bak"
I18N_DIR_TMP="/tmp/v2_i18n_preserve"

if [ -f "$VPS_DIR/config/config.json" ]; then
    cp "$VPS_DIR/config/config.json" "\$CFG_MAIN_TMP"
fi
if [ -f "$VPS_DIR/config/copytrader_v2.json" ]; then
    cp "$VPS_DIR/config/copytrader_v2.json" "\$CFG_COPY_TMP"
fi
if [ -f "$VPS_DIR/config/frontend_banners.json" ]; then
    cp "$VPS_DIR/config/frontend_banners.json" "\$FRONTEND_BANNERS_TMP"
fi

mkdir -p "$VPS_DIR/logs" "$VPS_DIR/data"
tar xzf /tmp/vaguspoly_v2_deploy.tar.gz -C "$VPS_DIR"
rm -f /tmp/vaguspoly_v2_deploy.tar.gz

# T865: 部署后立即清理生产目录中任何历史遗留的非生产文件，确保 /opt/polymarket_bot_v2 整洁。
echo "[V2-DEPLOY] T865 prod cleanliness sweep"
cd "$VPS_DIR"
rm -rf tests _archive_v1 _prod_sync_* .pytest_cache .mypy_cache .ruff_cache
rm -rf scripts/frontend/.design-pipeline
rm -f pytest.ini .prettierignore .env.example .gitignore
rm -f scripts/run_tests.sh scripts/setup_postgres_local.sh scripts/setup_postgresql.sh scripts/ui_design_pipeline.sh
find . -path ./.venv -prune -o -path ./data -prune -o -path ./logs -prune -o \
    \( -type f \( -name '*.md' -o -name '*.bak' -o -name '*.bak.*' -o -name '*.bak_*' \
       -o -name '*.backup' -o -name '*.orig' -o -name '*.swp' -o -name '*.tmp' \
       -o -name '*.pyc' -o -name '*.pyo' \) \) \
    -print0 2>/dev/null | xargs -0 -r rm -f
find . -path ./.venv -prune -o -type d -name '__pycache__' -print0 2>/dev/null | xargs -0 -r rm -rf
cd - >/dev/null

# Cleanup legacy V1 static artifacts that may remain on VPS from older releases.
rm -f \
    "$VPS_DIR/scripts/static/analysis.html" \
    "$VPS_DIR/scripts/static/betslist.html" \
    "$VPS_DIR/scripts/static/copytraders.html" \
    "$VPS_DIR/scripts/static/weather_map.html" \
    "$VPS_DIR/scripts/static/wallet_bind.html" \
    "$VPS_DIR/scripts/static/users.html" \
    "$VPS_DIR/scripts/static/system.html" \
    "$VPS_DIR/scripts/static/strategy.html" \
    "$VPS_DIR/scripts/static/security.html" \
    "$VPS_DIR/scripts/static/polymarket.html" \
    "$VPS_DIR/scripts/static/overview.html" \
    "$VPS_DIR/scripts/static/ordermonitor.html" \
    "$VPS_DIR/scripts/static/map.html" \
    "$VPS_DIR/scripts/static/logs.html" \
    "$VPS_DIR/scripts/static/js/data.js"

# Cleanup retired legacy Admin V3 dashboard UI assets (T646 SPA cutover).
rm -f \
    "$VPS_DIR/src/services/admin_v3/templates/dashboard.html" \
    "$VPS_DIR/src/services/admin_v3/templates/dashboard_legacy.html"
rm -rf \
    "$VPS_DIR/src/services/admin_v3/templates/pages" \
    "$VPS_DIR/src/services/admin_v3/static/js/admin_v3"

if [ -f "\$CFG_MAIN_TMP" ]; then
    mkdir -p "$VPS_DIR/config"
    mv "\$CFG_MAIN_TMP" "$VPS_DIR/config/config.json"
fi
if [ -f "\$CFG_COPY_TMP" ]; then
    mkdir -p "$VPS_DIR/config"
    mv "\$CFG_COPY_TMP" "$VPS_DIR/config/copytrader_v2.json"
fi
if [ -f "\$FRONTEND_BANNERS_TMP" ]; then
    mkdir -p "$VPS_DIR/config"
    mv "\$FRONTEND_BANNERS_TMP" "$VPS_DIR/config/frontend_banners.json"
fi

if [ ! -d "$VPS_DIR/.venv" ]; then
    python3 -m venv "$VPS_DIR/.venv"
fi

"$VPS_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$VPS_DIR/.venv/bin/pip" install --quiet -r "$VPS_DIR/requirements.txt"

# T485: 幂等重放 Postgres schema + 强制全部 public 表归 polymarket 用户所有
if [ -f "$VPS_DIR/scripts/init_pg_schema.sql" ]; then
    echo "[V2-DEPLOY] applying init_pg_schema.sql (idempotent)"
    sudo -u postgres psql -d polymarket_v2 -v ON_ERROR_STOP=0 -q -f "$VPS_DIR/scripts/init_pg_schema.sql" >/tmp/v2_schema.log 2>&1 || true
    tail -5 /tmp/v2_schema.log || true
    sudo -u postgres psql -d polymarket_v2 -v ON_ERROR_STOP=1 -q <<'PGEOF' || echo "[V2-DEPLOY-WARN] grant pass failed"
DO \$\$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tableowner <> 'polymarket'
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO polymarket', r.tablename);
  END LOOP;
  FOR r IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema='public'
  LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO polymarket', r.sequence_name);
  END LOOP;
END\$\$;
GRANT ALL ON SCHEMA public TO polymarket;
GRANT ALL ON ALL TABLES IN SCHEMA public TO polymarket;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO polymarket;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO polymarket;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO polymarket;
PGEOF
fi

sudo cp "$VPS_DIR/polymarket-dashboard-v2.service" /etc/systemd/system/
sudo cp "$VPS_DIR/polymarket-copytrader-v2.service" /etc/systemd/system/
if [ "$HAS_ADMIN_V3" = "1" ]; then
    sudo cp "$VPS_DIR/polymarket-admin-v3.service" /etc/systemd/system/
fi

sudo systemctl stop polymarket-bot-v2 2>/dev/null || true
sudo systemctl disable polymarket-bot-v2 2>/dev/null || true
sudo rm -f /etc/systemd/system/polymarket-bot-v2.service
sudo systemctl stop polymarket-bot 2>/dev/null || true
sudo systemctl disable polymarket-bot 2>/dev/null || true
sudo rm -f /etc/systemd/system/polymarket-bot.service
sudo systemctl stop polymarket-dashboard 2>/dev/null || true
sudo systemctl disable polymarket-dashboard 2>/dev/null || true
sudo rm -f /etc/systemd/system/polymarket-dashboard.service
sudo rm -rf /opt/polymarket_bot 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable "$DASHBOARD_SERVICE"
sudo systemctl enable "$COPYTRADER_SERVICE"
if [ "$HAS_ADMIN_V3" = "1" ]; then
    sudo systemctl enable "$ADMIN_V3_SERVICE"
fi

sudo chown -R ubuntu:ubuntu "$VPS_DIR/"
chmod -R 755 "$VPS_DIR/logs" "$VPS_DIR/data" 2>/dev/null || true

sudo systemctl restart "$DASHBOARD_SERVICE"
sudo systemctl restart "$COPYTRADER_SERVICE"
if [ "$HAS_ADMIN_V3" = "1" ]; then
    sudo systemctl restart "$ADMIN_V3_SERVICE"
fi

sleep 3
sudo systemctl is-active --quiet "$DASHBOARD_SERVICE" && echo "Dashboard: running" || echo "Dashboard: failed"
sudo systemctl is-active --quiet "$COPYTRADER_SERVICE" && echo "Copytrader: running" || echo "Copytrader: failed"
if [ "$HAS_ADMIN_V3" = "1" ]; then
    sudo systemctl is-active --quiet "$ADMIN_V3_SERVICE" && echo "AdminV3: running" || echo "AdminV3: failed"
fi
EOF
}

post_deploy_check() {
    log "Post-deploy check"
    ssh "$VPS_HOST" "sudo systemctl is-active $DASHBOARD_SERVICE && echo '$DASHBOARD_SERVICE: running' || echo '$DASHBOARD_SERVICE: NOT running'"
    ssh "$VPS_HOST" "sudo systemctl is-active $COPYTRADER_SERVICE && echo '$COPYTRADER_SERVICE: running' || echo '$COPYTRADER_SERVICE: NOT running'"
    if [ "$HAS_ADMIN_V3" = "1" ]; then
        ssh "$VPS_HOST" "sudo systemctl is-active $ADMIN_V3_SERVICE && echo '$ADMIN_V3_SERVICE: running' || echo '$ADMIN_V3_SERVICE: NOT running'"
        ssh "$VPS_HOST" "curl -fsS http://127.0.0.1:8081/admin-v3/healthz && echo '' || echo '[V2-WARN] admin_v3 healthz failed'"
        ssh "$VPS_HOST" "curl -fsS http://127.0.0.1:8081/admin-v3/spa/ >/dev/null && echo '[V2-INFO] admin_v3_spa reachable' || echo '[V2-WARN] admin_v3_spa unreachable'"
    else
        log "Admin V3 module absent (open-source edition) — skipping admin health checks"
    fi
        # Withdraw tx path env sanity check (non-secret; prints only SET/MISSING)
        ssh "$VPS_HOST" '
set -e
ENV="/opt/polymarket_bot_v2/.env"
if [ ! -f "$ENV" ]; then
    echo "[V2-WARN] withdraw env check skipped: $ENV missing"
else
    mode="official"
    if grep -q "^PM_ALLOW_LEGACY_DIRECT_TX=1" "$ENV"; then
        mode="legacy"
    fi
    echo "[V2-INFO] withdraw tx mode from .env: $mode"
    if [ "$mode" = "official" ]; then
        for k in RELAYER_API_KEY RELAYER_API_KEY_ADDRESS PRIVY_APP_ID PRIVY_APP_SECRET PRIVY_AUTHORIZATION_KEY_V2 PRIVY_SERVER_SIGNER_ID USER_SESSION_SECRET; do
            if grep -q "^${k}=" "$ENV"; then
                echo "[V2-INFO] $k=SET"
            else
                echo "[V2-WARN] $k=MISSING"
            fi
        done
    else
        for k in POLY_TX_PRIVATE_KEY POLY_WITHDRAW_PRIVATE_KEY POLY_PRIVATE_KEY; do
            if grep -q "^${k}=" "$ENV"; then
                echo "[V2-INFO] $k=SET"
                found_key=1
                break
            fi
        done
        if [ -z "${found_key:-}" ]; then
            echo "[V2-WARN] legacy mode enabled but no signing key found (POLY_TX_PRIVATE_KEY/POLY_WITHDRAW_PRIVATE_KEY/POLY_PRIVATE_KEY)"
        fi
    fi
fi
'
    local dry_run_val
    dry_run_val=$(ssh "$VPS_HOST" "grep -o '\"dry_run\":.*' $VPS_DIR/config/config.json | head -1" 2>/dev/null || echo "not found")
    warn "config.json dry_run on VPS: $dry_run_val"
    ssh "$VPS_HOST" "tail -5 $VPS_DIR/logs/dashboard.log 2>/dev/null || echo '(dashboard.log not created yet)'"
    ssh "$VPS_HOST" "tail -5 $VPS_DIR/logs/copytrader_v2.stdout.log 2>/dev/null || echo '(copytrader stdout log not created yet)'"
}
CMD="${1:-update}"

case "$CMD" in
    syntax)
        check_syntax
        ;;

    update)
        require_host
        check_syntax
        if [ "$HAS_ADMIN_V3" = "1" ]; then
            build_admin_v3_spa
        else
            log "Admin V3 module absent (open-source edition) — skipping SPA build"
        fi
        pack
        upload
        install_on_vps
        post_deploy_check
        log "V2 deployment completed"
        ;;

    status)
        require_host
        ssh "$VPS_HOST" "sudo systemctl status $DASHBOARD_SERVICE --no-pager || true"
        ssh "$VPS_HOST" "sudo systemctl status $COPYTRADER_SERVICE --no-pager || true"
        if [ "$HAS_ADMIN_V3" = "1" ]; then
            ssh "$VPS_HOST" "sudo systemctl status $ADMIN_V3_SERVICE --no-pager || true"
        fi
        ;;

    logs)
        require_host
        if [ "$HAS_ADMIN_V3" = "1" ]; then
            ssh "$VPS_HOST" "sudo journalctl -u $DASHBOARD_SERVICE -u $COPYTRADER_SERVICE -u $ADMIN_V3_SERVICE -n 100 --no-pager"
        else
            ssh "$VPS_HOST" "sudo journalctl -u $DASHBOARD_SERVICE -u $COPYTRADER_SERVICE -n 100 --no-pager"
        fi
        ;;

    dry_check)
        require_host
        ssh "$VPS_HOST" "grep 'dry_run' $VPS_DIR/config/config.json | head -5"
        ;;

    *)
        echo "Usage: $0 {update|syntax|status|logs|dry_check}"
        exit 1
        ;;
esac
