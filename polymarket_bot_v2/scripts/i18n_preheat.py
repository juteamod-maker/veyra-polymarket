#!/usr/bin/env python3
"""
i18n_preheat.py — 翻译缓存预热脚本

调用方式:
  cd /opt/polymarket_bot_v2 && python3 scripts/i18n_preheat.py [--lang en] [--limit 60]

设计:
- 拉取 Gamma /events?order=volume24hr&active=true&closed=false 的 Top N
- 排除 24h 成交量低于 i18n.preheat_min_volume_24h 的事件
- 排除已被 i18n.preheat_excluded_tags 标记的事件
- 强制开启 i18n.enable_translation（通过 VEYRA_I18N_FORCE 环境变量）
- 跳过已缓存的 (slug, lang) 组合
- 失败模型链自动回退（translator 内置）

退出码:
  0 = 成功（含部分失败但有进展）
  1 = 致命错误（DeepSeek key 缺失、Gamma 不可达等）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# 允许从仓库根运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src.utils.config_loader import get  # noqa: E402
from src.utils.translator import get_cached, translate_event  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("i18n_preheat")

GAMMA = "https://gamma-api.polymarket.com"


def fetch_top_events(limit: int) -> list[dict]:
    params = {
        "limit": limit,
        "offset": 0,
        "order": "volume24hr",
        "ascending": "false",
        "active": "true",
        "closed": "false",
    }
    resp = requests.get(f"{GAMMA}/events", params=params, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    return [e for e in raw if isinstance(e, dict)]


def should_skip(ev: dict, min_vol: float, excluded_tags: set[str]) -> str:
    vol_24h = float(ev.get("volume24hr") or 0)
    if vol_24h < min_vol:
        return f"low_volume_24h={vol_24h:.0f}<{min_vol:.0f}"
    tags = ev.get("tags") or []
    for t in tags:
        slug = (t.get("slug") if isinstance(t, dict) else "") or ""
        if slug.lower() in excluded_tags:
            return f"excluded_tag={slug}"
    if not (ev.get("title") or "").strip():
        return "no_title"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en", help="目标语言（默认 en）")
    parser.add_argument("--limit", type=int, default=0, help="预热条数；0 = 用 config.i18n.preheat_top_n")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描不调翻译")
    args = parser.parse_args()

    from src.utils.llm_client import llm_api_key_set as _llm_api_key_set
    if not _llm_api_key_set() and not args.dry_run:
        logger.error("DEEPSEEK_API_KEY 未设置，无法调用翻译模型")
        return 1

    # 强制启用翻译（preheat 期间）
    os.environ["VEYRA_I18N_FORCE"] = "1"

    limit = int(args.limit or get("i18n.preheat_top_n") or 60)
    min_vol = float(get("i18n.preheat_min_volume_24h") or 5000)
    excluded = set(s.lower() for s in (get("i18n.preheat_excluded_tags") or ["resolved"]))
    lang = args.lang.strip()

    logger.info("preheat lang=%s limit=%d min_vol=%.0f excluded_tags=%s", lang, limit, min_vol, excluded)

    try:
        events = fetch_top_events(limit)
    except Exception as exc:
        logger.error("Gamma /events 拉取失败: %s", exc)
        return 1

    logger.info("拉取 %d 条事件", len(events))

    n_skipped = 0
    n_cached = 0
    n_translated = 0
    n_failed = 0
    t0 = time.time()
    for i, ev in enumerate(events, 1):
        slug = (ev.get("slug") or "").strip().lower()
        if not slug:
            n_skipped += 1
            continue
        reason = should_skip(ev, min_vol, excluded)
        if reason:
            logger.debug("[%d/%d] skip slug=%s (%s)", i, len(events), slug, reason)
            n_skipped += 1
            continue
        cached = get_cached(slug, lang)
        # T333: 若 cached 缺子市场翻译 → 重新跑补齐（translate_event 内部也会检查）
        want_slugs = {
            str(m.get("slug") or "")
            for m in (ev.get("markets") or [])
            if isinstance(m, dict) and m.get("slug") and m.get("question")
        }
        have_slugs = set((cached or {}).get("markets", {}).keys()) if cached else set()
        if cached and want_slugs.issubset(have_slugs):
            n_cached += 1
            continue
        if args.dry_run:
            logger.info("[%d/%d] would translate slug=%s title=%r", i, len(events), slug, (ev.get("title") or "")[:60])
            continue
        try:
            # T333: 连同所有子市场 question 一起翻译（节省 description 的 token）
            src_markets = [
                {"slug": str(m.get("slug") or ""), "q": str(m.get("question") or "")}
                for m in (ev.get("markets") or [])
                if isinstance(m, dict) and m.get("slug") and m.get("question")
            ][:30]
            tr = translate_event(
                slug,
                lang,
                ev.get("title") or "",
                "",
                source_markets=src_markets,
            )
            if tr and tr.get("title"):
                n_translated += 1
                n_markets = len((tr.get("markets") or {}))
                logger.info(
                    "[%d/%d] ✓ slug=%s model=%s title=%r markets=%d",
                    i, len(events), slug, tr.get("model"), tr["title"][:60], n_markets,
                )
            else:
                n_failed += 1
                logger.warning("[%d/%d] ✗ slug=%s 翻译返回空", i, len(events), slug)
        except Exception as exc:
            n_failed += 1
            logger.warning("[%d/%d] ✗ slug=%s exc=%s", i, len(events), slug, exc)
        # 限流: 每条间隔 200ms 防止 DeepSeek 速率限制
        time.sleep(0.2)

    dur = time.time() - t0
    logger.info(
        "完成 lang=%s 总=%d 跳过=%d 已缓存=%d 翻译成功=%d 失败=%d 耗时=%.1fs",
        lang, len(events), n_skipped, n_cached, n_translated, n_failed, dur,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
