#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
V2_SCRIPT="${SCRIPT_DIR}/deploy_vps_v2.sh"
CMD="${1:-update}"

# Accept the most common typo to avoid no-op deployments in urgent operations.
if [ "$CMD" = "updae" ]; then
    CMD="update"
fi

case "$CMD" in
    update|syntax|status|logs|dry_check)
        exec bash "$V2_SCRIPT" "$CMD"
        ;;
    help|--help|-h)
        cat <<'EOF'
Usage: bash scripts/deploy_vps.sh {update|syntax|status|logs|dry_check}

This wrapper only supports the V2/V3 runtime:
- polymarket-dashboard-v2.service
- polymarket-copytrader-v2.service
- polymarket-admin-v3.service

Removed V1 commands: live, paper, dashboard, start, stop, restart, web-install,
web-start, web-stop, web-restart, web-logs, upload-env, compare_v1.
EOF
        ;;
    *)
        echo "Unsupported command: $CMD" >&2
        echo "V1 bot lifecycle commands were removed. Use: bash scripts/deploy_vps.sh help" >&2
        exit 1
        ;;
esac

