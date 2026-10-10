# 07 · Admin V3 Console (Private Module)

## 1. Why the console is not published

Admin V3 is an **operations console** with the following capabilities, which is why it
is not open:

- Global config governance (strategy params, fees, points rules).
- CMS (menus / banners / home cards / i18n).
- Points adjustment and referral-relationship management.
- Database observability tools.
- **It can write `.env` and environment-level state**, and can move real funds directly.

The console is designed as a **private, separately licensed** module: it is not part of
this open-source release, is not covered by this repository's Apache-2.0 licence, and
this repository grants no rights to it.

## 2. How to obtain it (in order of preference)

1. **Sponsor tier (recommended)** — sponsor the project on GitHub Sponsors / Polar at
   the *Operator* tier and you are invited to the private admin repository (monthly) or
   receive a one-time download link with a written licence. Automated, invoiced,
   revocable.
2. **Managed hosting / setup service** — for teams that don't want to self-host; the
   console is included.
3. **Direct contact** — X/Twitter: [@VeyraTrade](https://x.com/VeyraTrade) (replies may
   be slow; prefer option 1).

## 3. Relationship to the open-source code

- User site, API, copytrader, strategy engine: Apache-2.0 — build on them, subject to
  the licence.
- Admin V3 console: a separate commercial licence, negotiated individually.

## 4. Trademarks

"VeyraTrade", "Veyra.Trade" and the VeYra name and artwork are trademarks of the
project owner, not granted by the licence; see [NOTICE](../NOTICE).
