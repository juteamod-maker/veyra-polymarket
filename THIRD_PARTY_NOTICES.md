# Third-Party Notices

This repository redistributes third-party software. Each component remains under its
own licence; the terms below are provided for attribution. **Read the flagged items
in §3 before republishing this repository.**

---

## 1. Python dependencies

Declared in [`polymarket_bot_v2/requirements.txt`](./polymarket_bot_v2/requirements.txt).
They are **not** vendored — they are installed from PyPI and keep their own licences:

`requests`, `openai`, `schedule`, `python-dotenv`, `py-clob-client-v2`,
`py-builder-relayer-client` (installed from GitHub `Polymarket/py-builder-relayer-client`),
`psycopg2-binary`, `rich`, `yfinance`, `eccodes`, `fastapi`, `uvicorn`, `gunicorn`,
`jinja2`, `itsdangerous`, `python-multipart`, `pyotp`, `qrcode`, `PyJWT`,
`cryptography`, `websockets`, `pytest`, `pytest-cov`.

> `eccodes` is Apache-2.0 but links against ECMWF's **ecCodes** C library, which is
> Apache-2.0 as well. `py-builder-relayer-client` is installed from a moving git ref
> (`@main`) — pin a commit before shipping a release.

## 2. Front-end libraries committed under `scripts/frontend/static/js/vendor/`

Licence markers verified inside the distributed files:

| File | Upstream | Version | Licence |
|---|---|---|---|
| `chart.umd.min.js` | Chart.js | – | MIT |
| `chartjs-adapter-date-fns.bundle.min.js` | chartjs-adapter-date-fns | – | MIT |
| `htmx.min.js` | htmx | – | BSD-2-Clause |
| `leaflet.min.js` | Leaflet | – | BSD-2-Clause |
| `lucide.min.js` | Lucide | – | ISC |
| `marked.min.js` | marked | – | MIT |
| `purify.min.js` | DOMPurify | – | Apache-2.0 |

Also `scripts/static/css/tailwind.min.css` (Tailwind CSS, MIT).

> GSAP was previously bundled here (landing-page hero animation). It was removed in
> T902: the GreenSock Standard Licence is **not OSI-approved**, so the landing-page
> animation was rewritten with the Web Animations API (`landing.js` ships a small
> in-house `gsap`-compatible shim). No GSAP code remains in this repository.

## 3. The animated scene bundles

### 3.1 `scripts/frontend/static/seaz/` — the "pirate ship" preview scene

`assets/index-dYjrTTEm.js` (≈1.1 MB) is a **pre-built, minified bundle** rendering the
interactive 3D scene (captain's ship + islands + water, exposed as `window.__seazScene`)
used by the `/preview` page (`templates/preview.html`). The scene is **procedural** — no
image, model or texture assets are committed, so there are no third-party art assets to
clear.

Third-party components identified inside the bundle, with the notices embedded in it:

| Component | Notice found in the bundle | Licence |
|---|---|---|
| **three.js** (r184) | `Copyright 2010-2026 Three.js Authors` | MIT |
| **React / react-dom** | `Copyright (c) Meta Platforms, Inc. and affiliates.` | MIT |
| `@react-three/fiber`, `react-spring`, `react-use-measure` | `SPDX-License-Identifier: MIT` | MIT |

**Provenance of the scene code itself.** The build leftover `seaz/index.html` was
originally an unmodified export whose HTML title was `My Google AI Studio App` (still
visible in the git history of that file), i.e. the scene was generated with **Google AI
Studio** and then modified in this project — its own strings use `preview.seaz.*` i18n
keys. That leftover also references asset hashes that no longer exist and is excluded
from the public export.

⚠️ Two items to settle before publishing §3.1:

1. **Confirm your rights to the generated scene.** Google's Generative AI terms do not
   have Google claim ownership of your output, but you remain responsible for it — check
   the terms in force at generation time.
2. **AI-generated code can inadvertently reproduce third-party code.** Every identifiable
   third-party component here is MIT (three.js, React and friends), which is compatible
   with Apache-2.0, but the bundle ships **without its source**, so a reviewer cannot
   audit it. Recommended: commit the source project alongside the built bundle, or keep
   the attribution above.

### 3.2 `scripts/frontend/static/js/runtime/polymarket_sdk_bundle.js`

A minified build of `@polymarket/clob-client-v2`, `@polymarket/builder-signing-sdk`,
`@polymarket/builder-relayer-client`, `viem`, `ethers`, `ox`, `abitype`, `@noble/*` and
`axios` (≈2.1 MB). viem / ethers / abitype / tslib are MIT; **Polymarket's own SDK
licences must be confirmed.** The 8 MB source map that embedded their original
TypeScript sources has been removed from version control.

### 3.3 `scripts/frontend/static/js/vendor/mfa_runtime_bundle.mjs`

A minified build of the **Privy** client SDK (`@privy-io/*`, ≈4.5 MB). Privy's SDK licence
could not be verified from the bundle itself.

## 4. ⚠️ Action required before publishing

| Item | Problem | Suggested action |
|---|---|---|
| `static/seaz/` scene bundle | Ships without source; AI-generated provenance. | Commit the source project, or keep §3.1 and settle the two items there. |
| `runtime/polymarket_sdk_bundle.js` | Polymarket's SDK licence unconfirmed. | Confirm it, or build the bundle at install time from pinned npm versions. |
| `vendor/mfa_runtime_bundle.mjs` | Privy SDK licence unconfirmed. | Confirm it, or load the SDK from a pinned npm dependency at build time. |

> GSAP (GreenSock Standard Licence, non-OSI) was the only non-OSI component bundled
> in this repository and has been removed entirely (T902). The remaining flagged items
> are MIT-based with unconfirmed vendor SDK licences.

## 5. Data

`polymarket_bot_v2/data/oddsapi_snapshots/` and `polymarket_bot_v2/data/pm_tags/`
are **generated runtime caches**, not source. They have been removed from version
control because they contain third-party commercial data (OddsAPI/OddsPapi market
snapshots) whose **redistribution is likely restricted by the provider's terms of
service**. Do not commit them to a public fork.

## 6. Trademarks

"Polymarket", "Privy" and any other product names are trademarks of their respective
owners. Nothing here grants a trademark licence. This project is not affiliated with
or endorsed by Polymarket or Privy.
