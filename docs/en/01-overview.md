# 01 · Product & Business Logic

## 1. Positioning

Veyra.Trade is a multi-user Polymarket trading and operations platform — not a
single-machine bot script. It connects "market discovery → account authorization →
trade execution → asset settlement → operations governance" into one auditable loop.

## 2. Three runtime surfaces

| Surface | Entry | Audience | Responsibilities |
|---|---|---|---|
| User site + Dashboard API | `polymarket_bot_v2/scripts/web_dashboard.py` | Regular users | Rendering, pre/post-trade APIs, sessions & permissions, i18n |
| Copytrader V2 sidecar | `polymarket_bot_v2/src/services/copytrader_v2/daemon.py` | System | Copy-trade execution, independent lifecycle, resource isolation |
| Admin V3 console (private) | `src/services/admin_v3/` (not published) | Ops / risk / admins | Config governance, points & fees, CMS, observability |

## 3. Core business flows

1. **Acquisition & registration** — users register/log in, may attach a referral code
   and receive registration points per rules.
2. **Discovery & ordering** — filter markets on discover/event pages, submit orders;
   the system validates, handles nonce/signing and relayer submission.
3. **Funds & assets** — deposit / withdraw / redeem are high-risk paths with
   observable logs and failure diagnostics.
4. **Referral & points** — multi-level referral (L1/L2/L3); rewards are idempotent,
   loop-safe and abuse-resistant.
5. **Operations governance** — admins configure points, fees, copy and runtime state;
   all privileged actions are auditable.

## 4. Design principles

1. **Real capabilities first** — no fake buttons, fake saves, or fake state; every
   control talks to a real API.
2. **Tenant isolation first** — sensitive data is only read from the session context.
3. **Auditability first** — deployment, config and on-chain failure diagnostics are traceable.
4. **V2 boundary first** — no fallback to the legacy V1 single-user runtime.

## 5. How operators earn (Polymarket Builder Program)

This project is built on the [Polymarket Builder Program](https://docs.polymarket.com/programs/builders/overview):
your site routes users' orders into the Polymarket order book, and every matched order
carries your Builder Code, so fees, rebates and referral rewards are attributed to you.

| Stream | Mechanism | You earn |
|---|---|---|
| **Builder fee** | Matched orders carry your `bytes32` Builder Code; the fee is credited to your Builder account | Taker ≤ **1.00%**, Maker ≤ **0.50%** (self-configured within limits, additive to platform fees) |
| **Referral** | A share of the net trading fees your referred users generate | Direct **10%**, indirect **5%**, paid daily in pUSD |
| **Taker rebate** | Your users' taker volume accrues weighted volume, climbing tiers for daily rebates | Bronze → Obsidian, 7 tiers |
| **Copytrader / premium** | Smart Money copy trading and automated strategies | Subscription / add-on pricing you set |
| **Admin V3 + hosting** | Private console and managed deployment | Commercial licence / hosting fee (see [07](./07-admin-console.md)) |

> **Fees go directly to you**: set `POLY_BUILDER_CODE` in `.env` to enable attribution.
> Rate limits and rules are set by Polymarket — see
> [Builder Fees](https://docs.polymarket.com/programs/builders/fees) and the official docs.

## 6. Business / partnership

This open-source release covers the user site, API, copytrader and strategy engine
(Apache-2.0). The **Admin V3 console is a private module** — obtain it via the
sponsor tier or a commercial licence; see [07](./07-admin-console.md).
