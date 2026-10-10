# 04 · Getting Started

## 1. Requirements

- Python 3.12+
- PostgreSQL 14+
- A [Privy](https://privy.io) app (App ID / App Secret / authorization key)
- Polymarket CLOB v2 API credentials (`py-clob-client-v2`)
- Optional: Telegram notifications, RapidAPI/OddsAPI sports-odds key

## 2. Configuration

```bash
cd polymarket_bot_v2
cp .env.example .env        # fill in real values; .env is never committed
```

Key variables are listed in the root [README.md](../README.md) §4.

## 3. Local run

```bash
cd polymarket_bot_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# initialise the database schema
bash scripts/setup_postgres_local.sh
psql -d polymarket_v2 -f scripts/init_pg_schema.sql

# start the user site / API (default 8080)
uvicorn scripts.web_dashboard:app --host 127.0.0.1 --port 8080 --proxy-headers
```

> This release does not include the Admin V3 console, so there is no 8081 service to start.

## 4. Tests

```bash
cd polymarket_bot_v2
pytest -q                                   # see pytest.ini for paths and markers
pytest -q tests/test_mfa_totp.py            # server-side TOTP MFA unit tests
```

Some tests require a local PostgreSQL and a populated `config/config.json`; failing to
connect in a bare checkout is expected behaviour.

## 5. Deployment

The deployment entry point is the repo's own script:

```bash
bash polymarket_bot_v2/scripts/deploy_vps.sh update
```

`scripts/deploy_vps_v2.sh` reads the target host alias from `VPS_HOST` — replace it
with your own SSH host. When the private Admin V3 module is absent, the script skips
all admin steps automatically.

## 6. MFA (two-step verification)

User login supports **server-side TOTP** (Google Authenticator compatible):

- Enrol: `POST /api/user/mfa/enroll/start` returns a QR code and secret;
  `POST /api/user/mfa/enroll/confirm` verifies the confirmation code.
- Verify: `POST /api/user/mfa/verify-mark` verifies a 6-digit TOTP code and issues a
  30-minute signed session marker.
- Login: `POST /api/user/privy/verify` carries the TOTP code in `mfa_code`.
- Disable: `POST /api/user/mfa/disable` verifies the current code before disabling.

The front-end login flow pops up a TOTP input; the profile page offers enrol / verify
/ disable entry points.
