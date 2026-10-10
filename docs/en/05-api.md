# 05 · API & Data Model

## 1. API partitions

| Partition | Entry | Prefix |
|---|---|---|
| User-site API | `scripts/web_dashboard.py` (unified router mount) | `/api/user/*`, `/api/pm/*`, `/api/leaderboard/*`, `/api/public/*` |
| Admin API (private) | `src/services/admin_v3/routes_admin.py` | `/admin-v3/api` (not in this release) |

## 2. Identity & authentication

- Sole user identity channel: `user_session` cookie (HMAC-signed, derived from
  `USER_SESSION_SECRET`).
- Login: `POST /api/user/privy/verify` — the Privy access token is verified server-side;
  identity comes from the Privy API response, never from the client-supplied `user` object.
- MFA: server-side TOTP (see [04](./04-getting-started.md) §6).

## 3. Key endpoints (user side)

| Endpoint | Purpose |
|---|---|
| `POST /api/user/privy/verify` | Login / session establishment (with TOTP second factor) |
| `GET /api/user/mfa/status` | Current MFA status |
| `POST /api/user/mfa/enroll/start` | Start TOTP enrolment (returns QR and secret) |
| `POST /api/user/mfa/enroll/confirm` | Verify confirmation code, complete enrolment |
| `POST /api/user/mfa/verify-mark` | Verify TOTP code, issue 30-minute signed session |
| `POST /api/user/mfa/disable` | Verify current code, disable MFA |
| `GET /api/user/referral` | Referral chain, rewards and pagination |
| `GET /api/pm/*` | Polymarket data and fund actions (deposit / withdraw / redeem / vault) |
| `GET /api/leaderboard/*` | Leaderboards |
| `GET /api/public/*` | Public data |
| `GET /api/copy-tasks` … | Copy tasks (admin operations) |

> The full route list lives in `scripts/dashboard_routes/`. Fund actions (withdraw /
> redeem) resolve the caller wallet from the session and never accept a wallet address
> from the request body.

## 4. Data model overview

| Table | Purpose |
|---|---|
| `users` | User master table (wallet_address PK, role, Privy binding, TOTP, MFA state) |
| `user_sessions` | Sessions (HMAC token, expiry, source IP) |
| `copy_tasks` | Copy-trade tasks (global, created by admins) |
| `point_transactions` / `points_tiers` | Points ledger and tiers |
| `fee_config` / `fee_discount_tiers` | Fees and discounts |
| `user_pm_profiles` | User Polymarket binding profiles |

Schema definitions live in `scripts/init_pg_schema.sql` and the migrations in
`src/utils/db.py`.
