"""T599 — 动态 i18n 字典聚合接口。

目标架构（i18n_key + 英文兜底）：
- 后台运营条目（菜单 / 横幅 / 生态 / …）只填写 `i18n_key` + 默认英文（label_en / title_en）
  以及可选的其它语言覆盖（label_zh / title_zh / 后续 fr/ja/ko/...）。
- 前端 i18n 静态 JSON（zh-CN.json / en.json）只承载固定 UI 文案；
  后台用户填写的「动态文案」靠本接口注入运行时字典。
- 渲染时 `tCore(i18n_key)` 优先命中合并后的字典：
  current_lang → en → fallback；任一语言缺失自动回落英文，再回落 key 字面值。

聚合源（W1-W2 范围）：
1. app_menus 表 → 提取每行 (i18n_key, label_<lang>) 形成字典条目
2. frontend_banners (config/frontend_banners.json) → 提取
   (title_i18n_key, title_<lang>) / (subtitle_i18n_key, subtitle_<lang>) /
   (label_i18n_key, label_<lang>)

后续可继续接入 ecosystem / homepage / help_pages 等模块。

端点：
  GET /api/i18n/dynamic?lang=xx[,yy]   返回 {lang: {key: text}}
  默认返回当前 cookie/header 推断的单语，并附带 en 兜底。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import db_connect, logger
from scripts.dashboard_routes.frontend_banner_routes import _merged as _banners_merged

router = APIRouter()

# 支持的语种（与 i18n.js SUPPORTED 对齐）。
_SUPPORTED_LANGS: tuple[str, ...] = (
    "zh-CN",
    "en",
    "fr",
    "ja",
    "ko",
    "th",
    "vi",
    "es",
    "pt",
)


def _lang_suffix(lang: str) -> str:
    """把 i18n lang 映射成 DB 字段后缀。

    schema 现有 label_zh / label_en，未来扩列再加映射。当前仅 zh-CN→zh，其它→en。
    """
    s = (lang or "").strip().lower()
    if s.startswith("zh"):
        return "zh"
    return "en"


def _aggregate_menus(lang: str) -> dict[str, str]:
    """从 app_menus 收集 {i18n_key: label_<lang>}。"""
    suffix = _lang_suffix(lang)
    col = f"label_{suffix}"
    out: dict[str, str] = {}
    try:
        with db_connect() as conn:
            cur = conn.execute(
                f"SELECT i18n_key, {col}, label_en, label "
                "FROM app_menus "
                "WHERE i18n_key IS NOT NULL AND TRIM(i18n_key) <> ''"
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning(f"[i18n/dynamic] menus query failed: {exc}")
        return out
    for r in rows:
        if isinstance(r, dict):
            key = (r.get("i18n_key") or "").strip()
            primary = (r.get(col) or "").strip()
            en = (r.get("label_en") or "").strip()
            base = (r.get("label") or "").strip()
        else:
            key = (r[0] or "").strip()
            primary = (r[1] or "").strip()
            en = (r[2] or "").strip()
            base = (r[3] or "").strip()
        if not key:
            continue
        # T601 W6 修复 — 当前语种值优先；缺失时**不写入**，让前端 tCore
        # 回退到静态 i18n JSON（zh-CN.json 等）。否则会用 label_en 强行
        # 覆盖 zh-CN.json 中已有的中文翻译。
        # 仅当 suffix == 'en'（请求英文）时才允许 base 兜底。
        if primary:
            out[key] = primary
        elif suffix == "en":
            val = en or base
            if val:
                out[key] = val
    return out


def _aggregate_banners(lang: str) -> dict[str, str]:
    """从 frontend_banners 收集 (title/subtitle/label) 三组 i18n_key → 文本。"""
    suffix = _lang_suffix(lang)
    out: dict[str, str] = {}
    try:
        merged = _banners_merged() or {}
    except Exception as exc:
        logger.warning(f"[i18n/dynamic] banners merge failed: {exc}")
        return out
    for _slot, b in merged.items():
        if not isinstance(b, dict):
            continue
        for prefix in ("title", "subtitle", "label"):
            key = (b.get(f"{prefix}_i18n_key") or "").strip()
            if not key:
                continue
            primary = (b.get(f"{prefix}_{suffix}") or "").strip()
            en = (b.get(f"{prefix}_en") or "").strip()
            val = primary or (en if suffix != "en" else "")
            if val:
                out[key] = val
    return out


def _aggregate_ecosystem(lang: str) -> dict[str, str]:
    """T600 W4 / T601 W6 — 从 ecosystem_entries 派生 i18n key。

    优先级：
      1. 若行显式提供 name_i18n_key / description_i18n_key（T601 W6 新增列），
         使用该 key —— 让多个实体复用同一翻译条目。
      2. 否则按 slug 派生稳定默认 key：
            ecosystem.<slug>.name
            ecosystem.<slug>.description
    后续在 zh-CN.json / ja.json 等添加同名 key 即可覆盖中/日翻译；
    显式 key 不存在或为空 → 回退到 slug 派生 key。
    """
    out: dict[str, str] = {}
    suffix = _lang_suffix(lang)
    _has_keys = False
    rows: list = []
    try:
        with db_connect() as conn:
            try:
                cur = conn.execute(
                    "SELECT slug, name, description, "
                    "name_i18n_key, description_i18n_key "
                    "FROM ecosystem_entries WHERE enabled = TRUE"
                )
                rows = cur.fetchall()
                _has_keys = True
            except Exception:
                # ALTER TABLE 还没跑（首次部署窗口）→ 退回旧 schema
                cur = conn.execute(
                    "SELECT slug, name, description "
                    "FROM ecosystem_entries WHERE enabled = TRUE"
                )
                rows = cur.fetchall()
                _has_keys = False
    except Exception as exc:
        logger.warning(f"[i18n/dynamic] ecosystem query failed: {exc}")
        return out
    for r in rows:
        if isinstance(r, dict):
            slug = (r.get("slug") or "").strip()
            name = (r.get("name") or "").strip()
            desc = (r.get("description") or "").strip()
            name_key = (r.get("name_i18n_key") or "").strip() if _has_keys else ""
            desc_key = (
                (r.get("description_i18n_key") or "").strip() if _has_keys else ""
            )
        else:
            slug = (r[0] or "").strip()
            name = (r[1] or "").strip()
            desc = (r[2] or "").strip()
            name_key = (r[3] or "").strip() if _has_keys and len(r) > 3 else ""
            desc_key = (r[4] or "").strip() if _has_keys and len(r) > 4 else ""
        if not slug:
            continue
        # T601 W6 修复 — ecosystem_entries 的 name/description 字段是英文兜底，
        # 中文/日文等翻译应通过 zh-CN.json 等静态 JSON 提供。仅在请求英文（en）时
        # 才允许把 DB 中的英文 name 写入 dict；其它语种保持 dict 为空，让前端
        # tCore 命中 zh-CN.json 中的同名 key 显示对应翻译。
        if suffix == "en":
            if name:
                out[name_key or f"ecosystem.{slug}.name"] = name
            if desc:
                out[desc_key or f"ecosystem.{slug}.description"] = desc
    return out


def _aggregate_for_lang(lang: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    merged.update(_aggregate_menus(lang))
    # ecosystem 派生项作为兜底（slug 派生 key，不会与 menu i18n_key 冲突）。
    merged.update(_aggregate_ecosystem(lang))
    # banners 后写入：banner 的 key 命中同名菜单 key 时由 banner 覆盖（更具体）。
    merged.update(_aggregate_banners(lang))
    return merged


@router.get("/api/i18n/dynamic")
async def get_dynamic_i18n(request: Request) -> JSONResponse:
    """返回 {lang: {i18n_key: text}}。

    - `?lang=xx,yy` 指定多语种；缺省时使用 cookie / Accept-Language 推断。
    - 总是附带 `en` 作为全局兜底。
    """
    raw = (request.query_params.get("lang") or "").strip()
    if raw:
        wanted = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        cookie_lang = request.cookies.get("veyra_lang") or ""
        wanted = [cookie_lang] if cookie_lang else []
    # 始终附带 en 作为兜底。
    if "en" not in wanted:
        wanted.append("en")
    # 过滤合法值，去重保序。
    seen: set[str] = set()
    final: list[str] = []
    for w in wanted:
        if w in _SUPPORTED_LANGS and w not in seen:
            seen.add(w)
            final.append(w)
    if not final:
        final = ["en"]

    def _do() -> dict[str, dict[str, str]]:
        return {lang: _aggregate_for_lang(lang) for lang in final}

    try:
        result: dict[str, Any] = await asyncio.to_thread(_do)
    except Exception:
        logger.exception("[i18n/dynamic] aggregate failed")
        return JSONResponse({"ok": False, "error": "aggregate_failed"}, status_code=500)
    return JSONResponse(
        {"ok": True, "langs": final, "dict": result},
        headers={
            # 后台改动后通过缓存破坏 (?v=) 强制刷新；常态浏览器缓存 60s 即可。
            "Cache-Control": "private, max-age=60, stale-while-revalidate=300",
            "Vary": "Cookie",
        },
    )
