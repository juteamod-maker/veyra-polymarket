# 02 · Architecture & Boundaries

## 1. Services & ports

| Unit | Entry point | Port | Notes |
|---|---|---|---|
| `polymarket-dashboard-v2` | `scripts.web_dashboard:app` | `8080` (public, behind a gateway) | User site + Dashboard API |
| `polymarket-copytrader-v2` | `python3 -m src.services.copytrader_v2.daemon` | — (daemon) | Copy-trade sidecar |
| `polymarket-admin-v3` | `src.services.admin_v3.app:app` | `127.0.0.1:8081` (local only) | Private console, not in this release |

## 2. Code boundaries

| Path | Responsibility |
|---|---|
| `scripts/web_dashboard.py` | User-site entry, mounts routers via `include_router` |
| `scripts/dashboard_routes/` | User-side route modules (auth / user / discover / copytrader / redeem_withdraw, …) |
| `scripts/frontend/templates/` + `scripts/frontend/static/js/` | User-facing templates and runtime JS |
| `src/services/copytrader_v2/` | Copy-trade sidecar |
| `src/` | Strategy engine, data sync, on-chain utilities |

## 3. Config boundaries

| Config | Path |
|---|---|
| General config | `config/config.json` (git-ignored; see `config/config_docs.json`) |
| Copytrader config | `config/copytrader_v2.json` |
| Environment template | `.env.example` |
| i18n dictionaries | `scripts/frontend/static/i18n/*.json` |

## 4. Forbidden legacy entry points

- `main.py`
- `polymarket-bot*.service` (V1 single-user services)
- the legacy Logs Tab and legacy `/api/logs`
- any V1 single-user auto-trading fallback

## 5. On-chain & security constraints

1. On-chain constants come only from `src/utils/pm_v2_constants.py`.
2. SAFE nonce queries use the owner EOA.
3. Safe transaction construction reuses unified helpers — no ad-hoc forks.
4. Relayer 4xx diagnostic logs are kept for troubleshooting.

## 6. Change strategy

1. Change the runtime entry points and core routes first, then the UI.
2. Any change touching permissions / funds / referral rewards must update the docs
   and security notes.
3. Avoid cross-layer overhauls; land one verifiable increment at a time.
