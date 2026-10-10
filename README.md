# Veyra.Trade V2 (维拉)

> **Open-source multi-user prediction market platform built on Polymarket.** Deploy your
> own independent prediction market with high-precision data, Smart Money tracking, and
> automated strategies for weather, sports, and crypto.
>
> **基于 Polymarket 的开源多用户预测市场平台。** 支持独立部署与运营，内置高精度数据、
> Smart Money 跟踪与自动化策略（覆盖天气、体育、加密等）。

**White-label prediction market infrastructure on Polymarket — deploy it, brand it, and
earn trading fees.**
白标预测市场基础设施：部署它、冠上你的品牌、赚取交易手续费。

<div align="center">

[网站 Site](https://veyra.trade) · [演示 Demo](https://veyra.trade) ·
[文档 Docs](./docs) · [X/Twitter](https://x.com/VeyraTrade)

</div>

> **Status:** actively developed. Read [SECURITY.md](./SECURITY.md) before deploying —
> this software moves real funds on-chain and must never be run with default or
> placeholder credentials.

---

## 💰 How operators earn · 站长赚钱逻辑

Veyra routes your users' orders into Polymarket's order book. That is not just
plumbing — it is the revenue engine. This project is built on top of the
[Polymarket Builder Program](https://docs.polymarket.com/programs/builders/overview):

| 赚钱方式 | 机制 | 你赚多少 |
|---|---|---|
| **Builder 手续费** Builder fee | 每笔经你站点成交的订单附带你的 `bytes32` Builder Code，Polymarket 撮合后把手续费计入你的 Builder 账户 | Taker 最高 **1.00%**，Maker 最高 **0.50%**（在限额内自行设置，叠加在平台费之上） |
| **推荐计划** Referral | 你推荐的用户产生的净交易手续费分成 | 直推 **10%**、间推 **5%**，每日以 pUSD 结算 |
| **Taker 返利** Taker rebate | 你的用户做 taker 交易累积加权交易量，升级档位每日返佣 | Bronze → Obsidian 共 7 档，按量逐级提升 |
| **跟投 / 高级功能** Copytrader & premium | 面向不想手动盯盘的用户提供 Smart Money 跟投与自动化策略 | 订阅 / 增值服务定价由你决定 |
| **Admin V3 + 托管** Console & hosting | 面向大型运营方的私有运营台与代部署服务 | 商业授权 / 托管费（见 §10） |

> **关键点：手续费直接归你。** 你只需要把 `POLY_BUILDER_CODE` 配进 `.env`，平台即自动为
> 每笔订单附加归因代码。费率上限与规则由 Polymarket 设定，详见
> [Builder Fees](https://docs.polymarket.com/programs/builders/fees)。这些规则可能随时
> 调整，请以 Polymarket 官方文档为准。

### 为什么值得做 · Why run this

- 预测市场月成交额已达 **百亿美元级**，而大量需求来自单一市场之外——你正是本地市场的运营者。
- 你**不用重新做市、不用自建流动性**：系统镜像 Polymarket 的实时订单流，开箱即有深度。
- 用户经你站点交易**免 Gas**（走 relayer），转化阻力远低于传统链上产品。
- 已有高精度数据（天气集合、体育赔率）、Smart Money 跟踪与自动化策略，**内容即获客**。

## Who is this for

| 站长 / 运营方 | 你能用它做什么 |
|---|---|
| **独立站长 & 自媒体** | 把读者的观点变成可交易产品；嵌入实时市场；赚手续费而不是只卖广告 |
| **加密社区 / KOL** | 一键上自己的品牌预测市场，把粉丝转化成交易用户与跟投订阅 |
| **量化团队 & 交易博主** | 用现成的 Smart Money 跟踪与 Copytrader 工具沉淀用户与交易量 |
| **交易所 / 经纪 & 金融媒体** | 新增一个对手盘没有的资产类别，做区域市场先发者 |

---

## 1. What this is

Veyra.Trade V2 is a self-hosted, multi-user trading platform for [Polymarket](https://polymarket.com):

> ### ⚠️ What this repository contains
>
> This is the **open-source distribution (user-facing site only)**.
>
> **Included:** the user site + JSON API, the copytrader sidecar, the strategy/signal
> engine, the front-end, and the deployment tooling for those.
>
> **Not included:** the **Admin V3 operator console** (`admin_v3_spa/` +
> `src/services/admin_v3/`). It is a separate, privately licensed module. The user site
> does not import it — the repository works fully without it, and the deploy script
> detects its absence and skips the admin steps. See
> [§10 Obtaining the Admin V3 console](#10-obtaining-the-admin-v3-console).
>
> **Also removed from this distribution:** the archived V1 code, internal design
> scratch files, and all generated runtime state (caches, IP allow/deny lists and
> security events, which contain real addresses).
>
> **Brand assets are not included.** The VeYra logo and mascot artwork
> (`scripts/static/Veyra.png`, `Vgril_left.png`, …) are deliberately left out — the
> templates reference them, so substitute your own images before deploying. This
> also means the licence above grants you no rights to the VeYra name or artwork.


| Component | Path | Description |
|---|---|---|
| **User site + Dashboard API** | `scripts/web_dashboard.py` | FastAPI app (default port `8080`) serving the marketing site, the user dashboard and the JSON API. |
| **Admin console (Admin V3)** — *not in this distribution* | `admin_v3_spa/` + `src/services/admin_v3/routes_admin.py` | Separate FastAPI app + Vue 3 SPA with its own `admin_v3` Postgres schema, password + TOTP login and RBAC. See §10. |
| **Copytrader sidecar** | `src/services/copytrader_v2/daemon.py` | Independent process that mirrors trades for per-tenant copy tasks. |
| **Strategy / signal engine** | `src/`, `scripts/dashboard_routes/` | Weather-ensemble and sports/market signal generation, risk control, redeem/withdraw flows. |

### Runtime services

```
polymarket-dashboard-v2.service
polymarket-copytrader-v2.service
polymarket-admin-v3.service    # only in the full/private deployment
```

---

## 2. Architecture at a glance

```
Browser ──HTTPS──> reverse proxy ──> web_dashboard.py (FastAPI :8080)
                                          │
                                          ├─ user_session cookie (HMAC)  ← sole identity channel
                                          ├─ dashboard_routes/*.py        (JSON API)
                                          ├─ PostgreSQL (public schema)   (users, bets, points, copy_tasks…)
                                          └─ Privy (embedded wallets, MFA) + Polymarket CLOB v2

Browser ──HTTPS──> admin-v3 (:8081) ──> routes_admin.py ──> PostgreSQL (admin_v3 schema, fully isolated)
```

**Identity model.** `user_session` is an HMAC-signed cookie derived from
`USER_SESSION_SECRET`. Privy access tokens are verified server-side against the
Privy JWKS public key (`config/privy_verification_key.pem`). A user's wallet is
resolved **only** from the Privy API response — never from client-supplied data.

---

## 3. Requirements

- Python 3.12+
- PostgreSQL 14+
- Node.js 20+ (only needed to build the private `admin_v3_spa/` console; not required for this distribution)
- A [Privy](https://privy.io) app (App ID + App Secret + authorization key)
- Polymarket CLOB v2 API credentials (`py-clob-client-v2`)
- Optional: Telegram bot for notifications, RapidAPI/OddsAPI key for sports odds

---

## 4. Configuration

All configuration is **environment-first**. Copy the template and fill it in:

```bash
cp polymarket_bot_v2/.env.example polymarket_bot_v2/.env
```

`.env` is git-ignored and must **never** be committed. Key variables:

| Variable | Purpose |
|---|---|
| `USER_SESSION_SECRET` | HMAC seed for the `user_session` cookie. **Required** — the app falls back to an ephemeral random key (and logs CRITICAL) if unset. |
| `PG_HOST` / `PG_PORT` / `PG_DATABASE` / `PG_USER` / `PG_PASSWORD` | PostgreSQL connection. |
| `PRIVY_APP_ID` / `PRIVY_APP_SECRET` | Privy server SDK credentials. |
| `PRIVY_AUTHORIZATION_KEY_V2` | Wallet-auth signing key (`wallet-auth:<base64 PKCS8>` or raw PEM). |
| `RELAYER_API_KEY` / `RELAYER_API_KEY_ADDRESS` | Polymarket relayer key for gasless Safe transactions. |
| `POLY_PRIVATE_KEY` / `POLY_API_KEY` / `POLY_SECRET` / `POLY_PASSPHRASE` | Polymarket CLOB builder/relayer signing wallet. |
| `POLY_BUILDER_CODE` | Your `bytes32` Polymarket **Builder Code** — the attribution that routes trading fees/rebates to you (see “How operators earn”). |
| `ADMIN_V3_JWT_SECRET` | Admin console session signing secret. The admin app refuses to start if it is missing or too short. |
| `PM_USE_V2` | Must be `1`. V2 cutover toggle. |
| `DEEPSEEK_API_KEY*` / `OPENROUTER_API_KEY` | LLM providers (round-robin, optional). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications (optional). |

Strategy parameters live in `config/config.json` (git-ignored; ship your own copy)
and are documented in `config/config_docs.json`.

> **V1 is gone.** `main.py`, the `polymarket-bot*` systemd units and the old
> single-user bot lifecycle are removed. Do not reintroduce them.

---

## 5. Local development

```bash
cd polymarket_bot_v2

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. initialise Postgres schemas
bash scripts/setup_postgres_local.sh
psql -d polymarket_v2 -f scripts/init_pg_schema.sql

# 2. run the user site / API
uvicorn scripts.web_dashboard:app --host 127.0.0.1 --port 8080 --proxy-headers
```

That is all that is needed — this distribution runs the user site on its own.

### Admin console (only if you have the private module)

```bash
# 3. run the admin console (separate app)
uvicorn src.services.admin_v3.app:app --host 127.0.0.1 --port 8081 --proxy-headers
```

```bash
cd admin_v3_spa
npm ci
npm run build      # emits dist/ consumed by src/services/admin_v3/app.py
```

---

## 6. Tests

```bash
cd polymarket_bot_v2
pytest -q                      # see pytest.ini for paths/markers
pytest -q tests/test_user_privy_token_priority.py   # focused auth regression tests
```

Many tests require a live PostgreSQL database and a populated `config/config.json`;
without them they fail to connect (this is expected in a bare checkout).

---

## 7. Deployment

The only supported deployment entry point is the repo's own script:

```bash
bash polymarket_bot_v2/scripts/deploy_vps.sh update
```

It rsyncs the tree to the configured host, restarts the systemd units present in the
checkout (the `polymarket-admin-v3` unit is skipped automatically when the private admin
module is absent) and runs post-deploy health checks. Never edit code directly on the
server.

`scripts/deploy_vps_v2.sh` reads its target from `VPS_HOST` — set it to your own
SSH alias before deploying.

---

## 8. Security

Please read [SECURITY.md](./SECURITY.md). Highlights:

- `user_session` is the **only** user identity channel; the legacy `DASHBOARD_KEY`
  / TOTP / `?key=` bypasses are removed.
- Identity is derived exclusively from the Privy API response, never from the
  request body.
- Login MFA is **server-side TOTP** (Google Authenticator compatible); the
  `mfa_verified` marker is HMAC-signed and the client `mfa_verified` boolean is ignored.
- Admin console routes are all gated by `Depends(current_admin)` / `require_permission`.
- All SQL uses bound parameters; identifiers come from fixed whitelists.
- Secrets are read from the environment only.

Report vulnerabilities privately — see [SECURITY.md](./SECURITY.md).

---

## 9. Third-party notices

This repository redistributes some third-party front-end bundles. See
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) — in particular §3.1 (the
Three.js "pirate ship" preview scene) — before redistributing.

---

## 10. Obtaining the Admin V3 console

The operator console is **not** part of this distribution. It is offered separately,
under its own private licence — it is **not** covered by this repository's
Apache-2.0 licence, and this repository grants no rights to it.

**How to get it**, in order of preference:

1. **Sponsor tier (recommended).** Sponsor the project on GitHub Sponsors / Polar at the
   *Operator* tier and you are then invited to the private admin repository (monthly) or
   receive a one-time download link together with a written licence. This is automated,
   issues a receipt/invoice, and lets the author revoke access if a key leaks.
2. **Managed hosting / setup service.** Teams that would rather not self-host can buy a
   paid deployment; the admin console is included with it.
3. **Direct contact.** If neither fits, contact the author — X/Twitter:
   [@VeyraTrade](https://x.com/VeyraTrade). Replies may be slow, so prefer option 1.

> The admin console moves real money and can write `.env`. It is handed out with a
> written licence and a support channel rather than posted publicly, so that operators
> get setup help and the author keeps a record of who holds a copy.

Reusing the strategy engine, the API or the front-end in your own product is welcome —
subject to Apache-2.0. Reusing the admin console is a separate conversation.

## 11. Licence

Copyright 2026 [VeyraTrade](https://x.com/VeyraTrade) ([@VeyraTrade](https://x.com/VeyraTrade)).

Licensed under the **Apache License, Version 2.0** (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the License
at <http://www.apache.org/licenses/LICENSE-2.0>.

See [LICENSE](./LICENSE) for the full text and [NOTICE](./NOTICE) for the attribution
and trademark notices.

> Bundled third-party front-end files and their licences are listed in
> [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md). The Privy and Polymarket SDK
> bundles also need their terms confirmed before you redistribute them.
