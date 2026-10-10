"""T604 — 后台 i18n 直编 API。

提供 admin 直接编辑 `scripts/frontend/static/i18n/{lang}.json` 的最小 CRUD：
- GET  /api/admin/i18n/files                  → 列出所有可编辑语种
- GET  /api/admin/i18n/file?lang=en           → 返回该语种 flat key→value 字典
- POST /api/admin/i18n/file/upsert            → {lang, key, value} 写入单条键值
- POST /api/admin/i18n/file/delete            → {lang, key} 删除单条键
- POST /api/admin/i18n/file/replace           → {lang, dict} 整体替换（高级，原子写入）

写入策略：
- 仅允许 `[A-Za-z0-9_.\\-]+` 的 key（防路径穿越/注入）
- 仅允许已存在的 lang 文件名（白名单 = 现有目录列表）
- 写入前 fsync + 原子替换（tmp + os.replace），避免 partial write
- 写入后调用 i18n 内存缓存失效（如有）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import get_request_identity, logger

router = APIRouter()

_I18N_DIR = (
    Path(__file__).resolve().parent.parent / "frontend" / "static" / "i18n"
).resolve()
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MAX_VALUE_LEN = 4000
_MAX_KEY_LEN = 200


def _require_admin(request: Request) -> bool:
    ident = get_request_identity(request)
    return bool(ident and ident.get("is_admin"))


def _list_lang_files() -> list[str]:
    if not _I18N_DIR.is_dir():
        return []
    out = []
    for p in sorted(_I18N_DIR.iterdir()):
        if p.is_file() and p.suffix == ".json":
            out.append(p.stem)
    return out


def _resolve_lang_path(lang: str) -> Path | None:
    """白名单解析：lang 必须在现有文件清单内才返回路径。"""
    if not lang or not isinstance(lang, str):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", lang):
        return None
    candidates = set(_list_lang_files())
    if lang not in candidates:
        return None
    p = (_I18N_DIR / f"{lang}.json").resolve()
    # 防路径穿越：必须仍在 _I18N_DIR 下
    try:
        p.relative_to(_I18N_DIR)
    except ValueError:
        return None
    return p


def _read_json(p: Path) -> dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("i18n JSON root must be object")
    return data


def _atomic_write_json(p: Path, data: dict[str, Any]) -> None:
    """tmp + fsync + os.replace 原子写入，避免 partial。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _invalidate_caches() -> None:
    """让前端 SSR / 动态字典立即看到改动。"""
    try:
        from scripts.dashboard_routes import frontend_routes as fr

        # frontend_routes 的 _I18N_DICT_CACHE 以 mtime 比对，理论上写入后下次 read
        # 自动 miss；这里再显式清一次防止时钟分辨率边界问题。
        if hasattr(fr, "_I18N_DICT_CACHE"):
            fr._I18N_DICT_CACHE.clear()
    except Exception as e:
        logger.warning(f"[i18n-admin] frontend cache clear skipped: {e}")


@router.get("/api/admin/i18n/files")
async def list_files(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    langs = _list_lang_files()
    return {"ok": True, "langs": langs, "count": len(langs)}


@router.get("/api/admin/i18n/file")
async def get_file(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    lang = str(request.query_params.get("lang") or "").strip()
    p = _resolve_lang_path(lang)
    if not p:
        return JSONResponse({"ok": False, "error": "lang_not_found"}, status_code=404)

    def _do():
        return _read_json(p)

    try:
        data = await asyncio.to_thread(_do)
    except Exception as e:
        logger.warning(f"[i18n-admin] read {lang}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "lang": lang, "dict": data, "count": len(data)}


@router.post("/api/admin/i18n/file/upsert")
async def upsert_key(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    lang = str(body.get("lang") or "").strip()
    key = str(body.get("key") or "").strip()
    value = body.get("value")
    if value is None:
        value = ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "value_must_be_string"}, status_code=400
            )
    p = _resolve_lang_path(lang)
    if not p:
        return JSONResponse({"ok": False, "error": "lang_not_found"}, status_code=404)
    if not key or len(key) > _MAX_KEY_LEN or not _KEY_RE.match(key):
        return JSONResponse({"ok": False, "error": "invalid_key"}, status_code=400)
    if len(value) > _MAX_VALUE_LEN:
        return JSONResponse({"ok": False, "error": "value_too_long"}, status_code=400)

    def _do():
        data = _read_json(p)
        data[key] = value
        _atomic_write_json(p, data)
        return data

    try:
        data = await asyncio.to_thread(_do)
        _invalidate_caches()
        logger.info(f"[i18n-admin] upsert {lang}::{key} ({len(value)} chars)")
    except Exception as e:
        logger.warning(f"[i18n-admin] upsert {lang}::{key} failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "lang": lang, "key": key, "count": len(data)}


@router.post("/api/admin/i18n/file/delete")
async def delete_key(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    lang = str(body.get("lang") or "").strip()
    key = str(body.get("key") or "").strip()
    p = _resolve_lang_path(lang)
    if not p:
        return JSONResponse({"ok": False, "error": "lang_not_found"}, status_code=404)
    if not key or not _KEY_RE.match(key):
        return JSONResponse({"ok": False, "error": "invalid_key"}, status_code=400)

    def _do():
        data = _read_json(p)
        existed = key in data
        if existed:
            del data[key]
            _atomic_write_json(p, data)
        return existed, len(data)

    try:
        existed, count = await asyncio.to_thread(_do)
        if existed:
            _invalidate_caches()
            logger.info(f"[i18n-admin] delete {lang}::{key}")
    except Exception as e:
        logger.warning(f"[i18n-admin] delete {lang}::{key} failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "lang": lang, "key": key, "existed": existed, "count": count}


@router.post("/api/admin/i18n/file/replace")
async def replace_dict(request: Request):
    """整体替换 lang 的 key→value 字典（高级；建议慎用）。"""
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    lang = str(body.get("lang") or "").strip()
    new_dict = body.get("dict")
    p = _resolve_lang_path(lang)
    if not p:
        return JSONResponse({"ok": False, "error": "lang_not_found"}, status_code=404)
    if not isinstance(new_dict, dict):
        return JSONResponse({"ok": False, "error": "dict_required"}, status_code=400)
    # Validate every key
    cleaned: dict[str, str] = {}
    for k, v in new_dict.items():
        if not isinstance(k, str) or not _KEY_RE.match(k) or len(k) > _MAX_KEY_LEN:
            return JSONResponse(
                {"ok": False, "error": f"invalid_key:{k!r}"[:200]},
                status_code=400,
            )
        if v is None:
            v = ""
        if not isinstance(v, str):
            v = str(v)
        if len(v) > _MAX_VALUE_LEN:
            return JSONResponse(
                {"ok": False, "error": f"value_too_long:{k}"}, status_code=400
            )
        cleaned[k] = v

    def _do():
        _atomic_write_json(p, cleaned)
        return len(cleaned)

    try:
        count = await asyncio.to_thread(_do)
        _invalidate_caches()
        logger.info(f"[i18n-admin] replace {lang} ({count} keys)")
    except Exception as e:
        logger.warning(f"[i18n-admin] replace {lang} failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "lang": lang, "count": count}
