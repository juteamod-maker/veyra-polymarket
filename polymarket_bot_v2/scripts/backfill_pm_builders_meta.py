"""
T516 — Backfill pm_builders_meta with auto-generated descriptions/tags
       for every builder present in pm_builders_snapshot.

Behavior:
  - Reads top-N builders across all time periods from pm_builders_snapshot.
  - For each builder_key NOT already in pm_builders_meta (or with empty
    description), inserts an auto-generated meta row.
  - NEVER overwrites manually edited rows (description != '').
  - Tags & description are derived from verified flag + volume/active-user tier.
    - website left blank — admin can fill in via Admin V3 Frontend Management
    -> Ecosystem Nav -> Polymarket Builders Meta Editor.

Usage:
  python -m scripts.backfill_pm_builders_meta
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Allow `python scripts/backfill_pm_builders_meta.py` execution from project root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.dashboard_routes._shared import db_connect  # noqa: E402


# T523 P1 #521 — curated website map for well-known PM builders.
# Verified manually; takes priority over heuristic probing below.
CURATED_WEBSITES: dict[str, str] = {
    "polymarket":   "https://polymarket.com",
    "betmoar":      "https://betmoar.fun",
    "limitless":    "https://limitless.exchange",
    "novig":        "https://novig.us",
    "kalshi":       "https://kalshi.com",
    "manifold":     "https://manifold.markets",
    "blast":        "https://blast.io",
    "smarkets":     "https://smarkets.com",
    "stake":        "https://stake.com",
    "myriad":       "https://app.myriad.markets",
    "predictit":    "https://www.predictit.org",
    "metaculus":    "https://www.metaculus.com",
    "azuro":        "https://azuro.org",
    "thales":       "https://thalesmarket.io",
    "overtime":     "https://overtimemarkets.xyz",
    "augur":        "https://augur.net",
    "gnosis":       "https://gnosis.io",
    "zeitgeist":    "https://zeitgeist.pm",
    "futuur":       "https://futuur.com",
    "drift":        "https://drift.trade",
    "hyperliquid":  "https://hyperliquid.xyz",
    "1inch":        "https://1inch.io",
    "uniswap":      "https://uniswap.org",
    "sushi":        "https://sushi.com",
    "phantom":      "https://phantom.app",
    "rabby":        "https://rabby.io",
    "metamask":     "https://metamask.io",
    "frame":        "https://frame.sh",
    "coinbase":     "https://www.coinbase.com",
    "binance":      "https://www.binance.com",
    "okx":          "https://www.okx.com",
    "bybit":        "https://www.bybit.com",
    "kraken":       "https://www.kraken.com",
    "robinhood":    "https://robinhood.com",
    "uma":          "https://uma.xyz",
    "chainlink":    "https://chain.link",
    "arbitrum":     "https://arbitrum.io",
    "optimism":     "https://www.optimism.io",
    "base":         "https://base.org",
    "polygon":      "https://polygon.technology",
}


def _probe_url(url: str, timeout: float = 1.5) -> bool:
    """Return True iff a HEAD/GET to ``url`` resolves with 2xx/3xx."""
    try:
        import requests  # local import to keep cold-start light
        resp = requests.head(url, allow_redirects=True, timeout=timeout,
                             headers={"User-Agent": "VeYra-Bot/2.0 (+https://veyra.trade)"})
        if resp.status_code < 400:
            return True
        # Some sites disallow HEAD; retry GET (small range)
        resp = requests.get(url, allow_redirects=True, timeout=timeout,
                            headers={"User-Agent": "VeYra-Bot/2.0 (+https://veyra.trade)",
                                     "Range": "bytes=0-1"}, stream=True)
        return resp.status_code < 400
    except Exception:
        return False


def _heuristic_website(name: str) -> str | None:
    """Probe a small set of realistic TLDs concurrently for ``name``.

    Returns the first URL that resolves (HTTP 2xx/3xx), or None.
    Honest: never fabricate; if nothing resolves we leave the column empty.
    """
    import re as _re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    slug = _re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not slug or len(slug) < 3:
        return None
    candidates = [
        f"https://{slug}.com",
        f"https://{slug}.io",
        f"https://{slug}.xyz",
        f"https://{slug}.app",
        f"https://www.{slug}.com",
    ]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_probe_url, u, 1.5): u for u in candidates}
        for fut in as_completed(futs):
            try:
                if fut.result():
                    return futs[fut]
            except Exception:
                continue
    return None


def _backfill_websites(conn, only_empty: bool = True, max_probe: int = 400) -> tuple[int, int]:
    """Fill pm_builders_meta.website column. Returns (curated_n, probed_n)."""
    from concurrent.futures import ThreadPoolExecutor
    rows = conn.execute(
        "SELECT builder_key, display_name, website FROM pm_builders_meta ORDER BY builder_key"
    ).fetchall()
    curated_n = 0
    probed_n = 0
    now = datetime.now(timezone.utc).isoformat()

    # Pass 1: curated map (fast, no network).
    probe_targets: list[tuple[str, str]] = []  # (key, display_name)
    for r in rows:
        key = r["builder_key"]
        name = (r["display_name"] or key or "").strip()
        cur_site = (r["website"] or "").strip()
        if cur_site and only_empty:
            continue
        slug = (name or key).lower()
        url = CURATED_WEBSITES.get(slug) or CURATED_WEBSITES.get(slug.replace("-", "").replace("_", ""))
        if url:
            conn.execute(
                "UPDATE pm_builders_meta SET website=%s, updated_at=%s WHERE builder_key=%s",
                (url, now, key),
            )
            curated_n += 1
            continue
        probe_targets.append((key, name or key))
    conn.commit()

    # Pass 2: heuristic probe in parallel — keeps total under ~60s for 250 keys.
    if probe_targets:
        targets = probe_targets[:max_probe]
        with ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(lambda kv: (kv[0], _heuristic_website(kv[1])), targets))
        for key, url in results:
            if url:
                conn.execute(
                    "UPDATE pm_builders_meta SET website=%s, updated_at=%s WHERE builder_key=%s",
                    (url, now, key),
                )
                probed_n += 1
        conn.commit()
    return curated_n, probed_n


def _description_for(name: str, verified: bool, volume: float, active_users: int) -> str:
    """Generate a deterministic, honest placeholder description."""
    tier_word = (
        "large-scale"
        if volume >= 5e8
        else "established"
        if volume >= 5e7
        else "active"
        if volume >= 5e6
        else "emerging"
    )
    base = (
        f"{name} is a{' verified' if verified else 'n'} {tier_word} Polymarket builder "
        f"routing prediction-market trading volume on the platform."
    )
    if active_users >= 1000:
        base += f" Serves {active_users:,}+ unique traders."
    return base


def _tags_for(verified: bool, volume: float, active_users: int) -> str:
    tags = ["polymarket"]
    if verified:
        tags.append("verified")
    if volume >= 5e7:
        tags.append("high-volume")
    elif volume >= 5e6:
        tags.append("active")
    else:
        tags.append("emerging")
    if active_users >= 1000:
        tags.append("popular")
    return ",".join(tags)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    skipped_existing = 0
    updated_empty = 0

    with db_connect() as conn:
        # Make sure target table exists (idempotent).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_builders_meta (
                id           SERIAL PRIMARY KEY,
                builder_key  TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                description  TEXT NOT NULL DEFAULT '',
                website      TEXT NOT NULL DEFAULT '',
                tags         TEXT NOT NULL DEFAULT '',
                featured     BOOLEAN NOT NULL DEFAULT FALSE,
                sort_weight  INTEGER NOT NULL DEFAULT 100,
                updated_at   TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )

        # Pull the richest snapshot rows: prefer ALL > MONTH > WEEK > DAY,
        # but for each builder_key just pick the row with greatest volume.
        cur = conn.execute(
            """
            SELECT builder_key,
                   MAX(builder_name) AS name,
                   bool_or(verified) AS verified,
                   MAX(volume)       AS volume,
                   MAX(active_users) AS active_users
            FROM pm_builders_snapshot
            GROUP BY builder_key
            ORDER BY MAX(volume) DESC
            """
        )
        rows = cur.fetchall()
        print(f"[backfill] {len(rows)} distinct builders found in pm_builders_snapshot")

        for r in rows:
            key = r["builder_key"]
            name = (r["name"] or key) or ""
            verified = bool(r["verified"])
            volume = float(r["volume"] or 0)
            active_users = int(r["active_users"] or 0)

            # Check existing meta row
            existing = conn.execute(
                "SELECT description FROM pm_builders_meta WHERE builder_key=%s",
                (key,),
            ).fetchone()

            desc = _description_for(name, verified, volume, active_users)
            tags = _tags_for(verified, volume, active_users)
            display_name = name

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO pm_builders_meta
                        (builder_key, display_name, description, website, tags,
                         featured, sort_weight, updated_at, created_at)
                    VALUES (%s, %s, %s, '', %s, FALSE, 100, %s, %s)
                    ON CONFLICT (builder_key) DO NOTHING
                    """,
                    (key, display_name, desc, tags, now, now),
                )
                inserted += 1
            else:
                cur_desc = (existing["description"] or "").strip()
                if not cur_desc:
                    conn.execute(
                        """
                        UPDATE pm_builders_meta
                           SET display_name = COALESCE(NULLIF(display_name, ''), %s),
                               description  = %s,
                               tags         = CASE WHEN tags = '' THEN %s ELSE tags END,
                               updated_at   = %s
                         WHERE builder_key  = %s
                        """,
                        (display_name, desc, tags, now, key),
                    )
                    updated_empty += 1
                else:
                    skipped_existing += 1

    print(f"[backfill] inserted={inserted} updated_empty={updated_empty} "
          f"skipped_existing(manual)={skipped_existing}")

    # T523 P1 #521 — second pass: fill website column.
    with db_connect() as conn:
        curated_n, probed_n = _backfill_websites(conn, only_empty=True)
    print(f"[backfill-websites] curated={curated_n} probed_resolved={probed_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
