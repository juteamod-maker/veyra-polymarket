# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via GitHub's *Report a vulnerability* (Security → Advisories) on this
repository, or by e-mail to the maintainer. Include:

- affected file / endpoint and version or commit SHA,
- a minimal reproduction (request, payload, expected vs. actual),
- impact assessment (can funds move? can other tenants' data be read?).

We aim to acknowledge within 72 hours. Please allow a reasonable disclosure window
before publishing details.

---

## Deployment hardening checklist

This software moves real funds on-chain. Before exposing an instance to the internet:

- [ ] `USER_SESSION_SECRET` set to a long random value (≥ 32 bytes). If unset the
      app uses an **ephemeral random** key and logs `CRITICAL` — sessions then break
      on every restart, so this must be configured explicitly.
- [ ] `ADMIN_V3_JWT_SECRET` set (the admin app refuses to start without it).
- [ ] Serve **only over HTTPS**. Cookies (`user_session`, `privy-*-token`,
      `mfa_verified`, `veyra_session`) are issued with `Secure`, so plain HTTP will
      not authenticate.
- [ ] `config/config.json` and `.env` are never committed; both are git-ignored.
- [ ] Never log secret material. Keys are logged only as truncated SHA-256
      fingerprints.
- [ ] Rotate `POLY_PRIVATE_KEY`, `PRIVY_AUTHORIZATION_KEY_V2`, `RELAYER_API_KEY`
      whenever a machine holding them is lost or shared.

---

## Security model (what the code guarantees)

| Area | Guarantee |
|---|---|
| User identity | `user_session`, an HMAC-SHA256 cookie derived from `USER_SESSION_SECRET`. The legacy `DASHBOARD_KEY` / TOTP / `?key=` bypasses were removed and are explicitly rejected. |
| Privy login | The Privy access token is verified server-side against the app's JWKS public key. The user's wallets, embedded-wallet id and e-mail are taken **only** from the Privy API response — client-supplied `user.linkedAccounts` is ignored. |
| MFA (login) | **Server-side TOTP.** Users with `totp_enabled` must provide a valid 6-digit TOTP code (Google Authenticator compatible) at login; the `mfa_verified` session marker is HMAC-signed so it cannot be forged. The request-body `mfa_verified` boolean is ignored. |
| Admin console | Every route carries `Depends(current_admin)` or `Depends(require_permission(...))`. PBKDF2-HMAC-SHA256 (200k iterations), constant-time comparison, mandatory TOTP, account lockout and IP auto-ban. Its own `admin_v3` schema. |
| Global-state mutations | `/api/emergency-freeze`, `/api/weather-cities*`, `/api/evolution/trigger`, `/api/risk/resume`, `/api/map/markers*` and `/api/copy-tasks*` require `admin` / `super_admin` inside the handler (not merely via the middleware prefix list). |
| Tenancy | Copytrader V2 statements are scoped by `owner_wallet_address`; redeem/withdraw endpoints take the caller wallet from the session, never from the body. |
| SQL | Bound parameters throughout; dynamic identifiers come from fixed whitelists or integer validation. |
| Templates | JSON embedded in `<script>` passes through the `escape_script_json` filter, which escapes `</` to prevent `</script>` breakout XSS. |
| Outbound requests | Every outbound URL uses a hardcoded host; user input only reaches query strings / path segments. |

### Known limits

- **MFA** is enforced as server-side TOTP at login; the Privy client-SDK MFA is no
  longer trusted as a login factor because its completion could not be verified
  server-side. See `docs/04-getting-started.md` §6 for the endpoints.
- Rate limiting is per-process in-memory; run a single worker or add a shared store
  for multi-worker deployments.

---

## Supported versions

Only the latest `main` is supported. There are no maintained release branches.
