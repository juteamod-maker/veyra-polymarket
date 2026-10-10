"""
translator.py — 事件标题 + 子市场问题 多语言翻译服务（T333 重构 / T863 切换 DeepSeek）

设计：
- 主模型: deepseek-v4-flash（DeepSeek 官方 API）
- 缓存: PostgreSQL event_translations (slug, lang) 复合主键 + 进程内 LRU (max 2000)
- 接口:
    translate_event(slug, lang, source_title, source_description='', *, source_markets=None)
        -> {"title": "...", "markets": {market_slug: translated_question, ...}, "model": "..."}
    get_cached(slug, lang) -> 同上结构 / None
- 透明: 翻译失败/key 缺失时返回 None，调用方应回落到原文
- 源语言为英文（Polymarket 原文）；lang='en' 直接返回 None（调用方用原文）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

from src.utils.config_loader import get
from src.utils.database import db_connect

logger = logging.getLogger(__name__)

# T863: 切换为 DeepSeek 官方 API，统一使用 deepseek-v4-flash
from src.utils.llm_client import (
    DEFAULT_LLM_MODEL as _DS_DEFAULT_MODEL,
    chat_completion_json as _ds_chat_json,
    llm_api_key_set as _ds_key_set,
)

# 翻译模型链（按顺序尝试）；元素归一化由 llm_client.normalize_model 处理
_DEFAULT_MODELS = [_DS_DEFAULT_MODEL]

# 进程内 LRU
_MEM_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_MEM_CACHE_MAX = 2000
_MEM_LOCK = threading.Lock()

# DDL（首次调用时懒建）
_TABLE_READY = False
_TABLE_LOCK = threading.Lock()


def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _TABLE_LOCK:
        if _TABLE_READY:
            return
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_translations (
                        slug         TEXT NOT NULL,
                        lang         TEXT NOT NULL,
                        title        TEXT NOT NULL DEFAULT '',
                        description  TEXT NOT NULL DEFAULT '',
                        markets_json TEXT NOT NULL DEFAULT '{}',
                        model        TEXT NOT NULL DEFAULT '',
                        created_at   DOUBLE PRECISION NOT NULL,
                        updated_at   DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (slug, lang)
                    )
                    """
                )
                # T333: 向后兼容 ALTER — 旧表追加 markets_json 列
                try:
                    conn.execute(
                        "ALTER TABLE event_translations ADD COLUMN IF NOT EXISTS markets_json TEXT NOT NULL DEFAULT '{}'"
                    )
                except Exception:
                    pass
            _TABLE_READY = True
        except Exception as exc:
            logger.warning("translator: ensure table failed: %s", exc)


def _mem_get(key: str) -> Optional[dict]:
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            _MEM_CACHE.move_to_end(key)
            return _MEM_CACHE[key]
    return None


def _mem_put(key: str, value: dict) -> None:
    with _MEM_LOCK:
        _MEM_CACHE[key] = value
        _MEM_CACHE.move_to_end(key)
        while len(_MEM_CACHE) > _MEM_CACHE_MAX:
            _MEM_CACHE.popitem(last=False)


def get_cached(slug: str, lang: str) -> Optional[dict]:
    """读取已缓存翻译；不触发翻译。"""
    if not slug or not lang or lang == "en":
        return None
    key = f"{slug}|{lang}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    _ensure_table()
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT title, description, markets_json, model FROM event_translations WHERE slug=%s AND lang=%s",
                (slug, lang),
            ).fetchone()
        if row:
            try:
                markets = json.loads(row["markets_json"] or "{}")
                if not isinstance(markets, dict):
                    markets = {}
            except Exception:
                markets = {}
            data = {
                "title": row["title"] or "",
                "description": row["description"] or "",
                "markets": markets,
                "model": row["model"] or "",
            }
            _mem_put(key, data)
            return data
    except Exception as exc:
        logger.debug("translator: db read failed: %s", exc)
    return None


def _save(
    slug: str,
    lang: str,
    title: str,
    description: str,
    markets: dict,
    model: str,
) -> None:
    _ensure_table()
    now = time.time()
    try:
        markets_json = json.dumps(markets or {}, ensure_ascii=False)
    except Exception:
        markets_json = "{}"
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO event_translations (slug, lang, title, description, markets_json, model, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug, lang) DO UPDATE
                SET title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    markets_json = EXCLUDED.markets_json,
                    model = EXCLUDED.model,
                    updated_at = EXCLUDED.updated_at
                """,
                (slug, lang, title or "", description or "", markets_json, model or "", now, now),
            )
    except Exception as exc:
        logger.warning("translator: db write failed: %s", exc)
    _mem_put(
        f"{slug}|{lang}",
        {"title": title, "description": description, "markets": markets or {}, "model": model},
    )


def _call_model(
    model: str,
    src_title: str,
    src_markets: list[dict],
    target_lang: str,
) -> Optional[dict]:
    """单次调用 DeepSeek 官方 API 翻译 title + 所有子市场 question。

    src_markets: [{"slug":"mkt-slug","q":"English question"}, ...]
    返回: {"title": "...", "markets": {slug: translated_q, ...}} 或 None
    """
    if not _ds_key_set():
        return None
    lang_label_map = {
        "zh-CN": "Simplified Chinese (简体中文)",
        "zh": "Simplified Chinese",
        "zh-TW": "Traditional Chinese (繁體中文)",
        "es": "Spanish (Español)",
        "es-ES": "Spanish (Español)",
        "ja": "Japanese (日本語)",
        "ko": "Korean (한국어)",
        "ru": "Russian (Русский)",
        "fr": "French (Français)",
        "de": "German (Deutsch)",
        "pt": "Portuguese (Português)",
        "it": "Italian (Italiano)",
        "ar": "Arabic (العربية)",
        "en": "English",
    }
    lang_label = lang_label_map.get(target_lang, target_lang)

    # 限制 markets 数量（避免 prompt 过长），优先保留前 30 个
    src_markets = (src_markets or [])[:30]
    # 过滤掉空 slug/q
    src_markets = [m for m in src_markets if isinstance(m, dict) and m.get("slug") and m.get("q")]

    sys_prompt = (
        f"You are a professional translator for Polymarket prediction-market events. "
        f"Translate ALL given English text into {lang_label}. "
        f"Preserve proper nouns, team names, player names, dates, numbers, percentages, "
        f"currency amounts, and ticker symbols exactly. Keep translations concise and natural "
        f"for native speakers. Do NOT add explanations. "
        f"Return STRICT JSON with this exact shape: "
        f'{{"title": "<translated title>", "markets": [{{"slug": "<keep as-is>", "q": "<translated question>"}}, ...]}}. '
        f"The 'markets' array MUST include every market from the input with its original slug unchanged. "
        f"No markdown, no surrounding code fence."
    )
    user_prompt = json.dumps(
        {
            "title": src_title or "",
            "markets": [{"slug": m["slug"], "q": m["q"]} for m in src_markets],
        },
        ensure_ascii=False,
    )
    content = _ds_chat_json(
        model=model,
        system=sys_prompt,
        user=user_prompt,
        max_tokens=min(2000, 300 + 100 * max(1, len(src_markets))),
        temperature=0.2,
        timeout=30,
    )
    if not content:
        return None
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or "title" not in data:
            return None
        title = str(data.get("title") or "").strip()
        markets_out: dict[str, str] = {}
        raw_markets = data.get("markets")
        if isinstance(raw_markets, list):
            for item in raw_markets:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("slug") or "").strip()
                q = str(item.get("q") or "").strip()
                if slug and q:
                    markets_out[slug] = q
        return {"title": title, "markets": markets_out}
    except Exception as exc:
        logger.debug("translator: model %s parse failed: %s", model, exc)
    return None


def translate_event(
    slug: str,
    lang: str,
    source_title: str,
    source_description: str = "",
    *,
    source_markets: Optional[list[dict]] = None,
    use_cache: bool = True,
) -> Optional[dict]:
    """主入口：翻译 event title + 所有子市场 question。

    参数:
      slug / lang / source_title: 必填
      source_description: 保留向后兼容，实际不再翻译（T333 节省 token）
      source_markets: [{"slug":"xxx","q":"English question"}, ...] — 子市场问题列表
      use_cache: 是否优先读缓存

    返回:
      {"title": str, "description": "", "markets": {market_slug: translated_q}, "model": str}
      或 None（lang=en / key 缺失 / 未启用 / 翻译失败）
    """
    if not slug or not lang or lang == "en":
        return None
    if not source_title and not source_markets:
        return None

    cached_dict = None
    if use_cache:
        cached = get_cached(slug, lang)
        if cached is not None:
            # 若已有 title 但子市场未翻译齐全且有新 slug，需要补翻译
            have_markets = set((cached.get("markets") or {}).keys())
            want_markets = {
                m.get("slug") for m in (source_markets or []) if isinstance(m, dict) and m.get("slug")
            }
            missing = want_markets - have_markets
            if not missing:
                return cached
            # 缓存命中但 markets 不全：仅翻译缺失部分，下方做合并保存
            cached_dict = cached
            logger.debug("translator: cache partial hit slug=%s missing=%d", slug, len(missing))

    if not (get("i18n.enable_translation") or False) and not os.environ.get("VEYRA_I18N_FORCE"):
        # 默认禁用，避免高频翻译开销；由调用方/cron 显式开启
        return None

    cfg_models = get("i18n.translator_models")
    models = list(cfg_models) if isinstance(cfg_models, list) and cfg_models else _DEFAULT_MODELS
    # 当 cache 已有部分翻译时，只把缺失的子市场送入模型；title 走缓存或 source
    have_markets_set = set((cached_dict.get("markets") or {}).keys()) if cached_dict else set()
    pending_markets = [
        {"slug": m.get("slug"), "q": m.get("q") or m.get("question") or ""}
        for m in (source_markets or [])
        if isinstance(m, dict) and m.get("slug") and m.get("slug") not in have_markets_set
    ]
    title_for_call = (cached_dict or {}).get("title") or source_title
    # 多轮循环：每轮最多翻译 30 个，覆盖到全部子市场（避免大事件 60+ markets 永远 pending）
    accumulated_markets = dict((cached_dict or {}).get("markets") or {})
    used_model = None
    out_title = title_for_call
    rounds = 0
    while pending_markets and rounds < 4:
        rounds += 1
        progress = False
        for m in models:
            out = _call_model(m, title_for_call, pending_markets, lang)
            if out and out.get("title"):
                used_model = m
                out_title = out.get("title") or out_title
                new_markets = out.get("markets") or {}
                if new_markets:
                    accumulated_markets.update(new_markets)
                    progress = True
                break
        if not progress:
            break
        # 重算缺失
        pending_markets = [m for m in pending_markets if m["slug"] not in accumulated_markets]
    # 若没有 cache 也没有任何成功翻译 → 调一次确保至少 title
    if not cached_dict and not used_model:
        for m in models:
            out = _call_model(m, source_title, [], lang)
            if out and out.get("title"):
                used_model = m
                out_title = out.get("title")
                break
    if used_model or cached_dict:
        _save(slug, lang, out_title or source_title, "", accumulated_markets, used_model or (cached_dict or {}).get("model") or "")
        return {
            "title": out_title or source_title,
            "description": "",
            "markets": accumulated_markets,
            "model": used_model or (cached_dict or {}).get("model") or "",
        }
    return None


# T333: 后台 fire-and-forget 翻译（避免阻塞 /api/pm/events 响应）
_BG_INFLIGHT: set[str] = set()
_BG_LOCK = threading.Lock()
_BG_SEMAPHORE = threading.BoundedSemaphore(value=3)  # 最多 3 路并发，防止 DeepSeek 限流

# T868: 日内/含日期的 throwaway slug 不进入后台翻译队列
_THROWAWAY_SLUG_HOURLY_RE = re.compile(
    r"-(?:1[0-2]|[1-9])(?:am|pm)(?:-[a-z]{2,3})?(?:$|-)",
    re.IGNORECASE,
)
_THROWAWAY_SLUG_MONTH_RE = re.compile(
    r"-(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)-\d{1,2}-\d{4}(?:$|-)",
    re.IGNORECASE,
)


def _slug_is_throwaway(slug: str) -> bool:
    s = (slug or "").strip().lower()
    if not s:
        return True
    if _THROWAWAY_SLUG_HOURLY_RE.search(s):
        return True
    if _THROWAWAY_SLUG_MONTH_RE.search(s):
        return True
    return False


def translate_async(
    slug: str,
    lang: str,
    source_title: str,
    source_markets: Optional[list[dict]] = None,
) -> bool:
    """启动后台翻译；同一 (slug,lang) 去重，返回 True=已排入队列，False=已在处理或跳过。"""
    if not slug or not lang or lang == "en":
        return False
    if _slug_is_throwaway(slug):
        # T868: throwaway 事件（含明确日期/小时段）不浪费翻译配额
        return False
    key = f"{slug}|{lang}"
    with _BG_LOCK:
        if key in _BG_INFLIGHT:
            return False
        _BG_INFLIGHT.add(key)

    def _worker():
        try:
            if not _BG_SEMAPHORE.acquire(timeout=30):
                return
            try:
                # 强制启用（后台翻译场景）
                os.environ.setdefault("VEYRA_I18N_FORCE", "1")
                translate_event(slug, lang, source_title, "", source_markets=source_markets)
            finally:
                _BG_SEMAPHORE.release()
        except Exception as exc:
            logger.debug("translator: bg worker failed slug=%s: %s", slug, exc)
        finally:
            with _BG_LOCK:
                _BG_INFLIGHT.discard(key)

    t = threading.Thread(target=_worker, name=f"i18n-bg-{slug[:20]}", daemon=True)
    t.start()
    return True


def cache_stats() -> dict:
    _ensure_table()
    rows = []
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT lang, COUNT(*) AS n FROM event_translations GROUP BY lang"
            ).fetchall()
    except Exception:
        pass
    by_lang = {str(r["lang"]): int(r["n"]) for r in rows}
    with _MEM_LOCK:
        mem_size = len(_MEM_CACHE)
    return {"db_by_lang": by_lang, "mem_size": mem_size, "mem_max": _MEM_CACHE_MAX}


def clear_cache(lang: Optional[str] = None) -> int:
    """清空缓存（admin 维护用）。返回删除条数。"""
    _ensure_table()
    n = 0
    try:
        with db_connect() as conn:
            if lang:
                cur = conn.execute("DELETE FROM event_translations WHERE lang=%s", (lang,))
            else:
                cur = conn.execute("DELETE FROM event_translations")
            n = getattr(cur, "rowcount", 0) or 0
    except Exception as exc:
        logger.warning("translator: clear failed: %s", exc)
    with _MEM_LOCK:
        if lang:
            for k in list(_MEM_CACHE.keys()):
                if k.endswith(f"|{lang}"):
                    _MEM_CACHE.pop(k, None)
        else:
            _MEM_CACHE.clear()
    return n
