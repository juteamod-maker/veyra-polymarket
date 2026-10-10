# 06 · Security Model

The full security policy is in the root [SECURITY.md](../SECURITY.md). This guide gives
a summary.

## 1. Identity model

- `user_session` is the sole user identity channel, HMAC-SHA256-signed, derived from
  `USER_SESSION_SECRET`.
- The legacy `DASHBOARD_KEY` / TOTP / `?key=` bypasses were removed and are explicitly
  rejected.
- Privy login: the access token is verified server-side; wallets, embedded wallet and
  e-mail come **only from the Privy API response** — the client-supplied
  `user.linkedAccounts` is ignored.

## 2. MFA (two-step verification)

- Login MFA is enforced by **server-side TOTP**: users with `totp_enabled` must provide
  a valid 6-digit TOTP code to log in.
- The `mfa_verified` session marker is HMAC-signed and cannot be forged.
- The request-body `mfa_verified` boolean is ignored.

## 3. Authorisation boundaries

- Global-state mutations (`/api/emergency-freeze`, `/api/weather-cities*`,
  `/api/evolution/trigger`, `/api/risk/resume`, `/api/map/markers*`, `/api/copy-tasks*`)
  require admin / super_admin.
- Copytrader V2 statements are scoped by `owner_wallet_address`.
- Fund actions resolve the wallet from the session, not from the request body.

## 4. Injection & secure coding

- SQL is fully parameterised; dynamic identifiers come only from fixed whitelists or
  integer validation.
- No `shell=True` / `eval` / `exec` / `pickle.load` / `yaml.load`.
- JSON embedded in `<script>` passes through `escape_script_json`, which escapes `</`.
- Outbound URLs use hardcoded hosts; user input only reaches query/path segments.
- Cookies (`user_session` / `privy-*-token` / `mfa_verified`) are
  `Secure + HttpOnly + SameSite`.
- The session secret has no hardcoded fallback; if unset, an ephemeral random key is
  used and a CRITICAL log is emitted.

## 5. On-chain constraints

- On-chain constants come only from `src/utils/pm_v2_constants.py`.
- SAFE nonce queries use the owner EOA.
- Safe transaction construction reuses unified helpers.
- `[Relayer 4xx]` diagnostic logs are kept.

## 6. Vulnerability reporting

Do not disclose publicly. Report privately via GitHub Security Advisories or e-mail;
see [SECURITY.md](../SECURITY.md).
