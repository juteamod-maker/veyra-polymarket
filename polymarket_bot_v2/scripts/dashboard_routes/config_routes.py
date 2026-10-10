"""
dashboard_routes/config_routes.py — 配置管理路由
包含: GET/PATCH /api/config, POST /api/toggle-dry-run
"""

import json
import logging
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    CONFIG_PATH,
    backup_config,
    get,
    get_request_identity,
    logger,
)
from src.services.copytrader_v2.config import (
    CONFIG_PATH as COPYTRADER_V2_CONFIG_PATH,
    load_config as load_copytrader_v2_config,
    update_config as update_copytrader_v2_config,
)

router = APIRouter()
_COPYTRADER_SECTION = "copytrader_v2"
_V2_WEATHER_SECTIONS = {
    "weather_cities",
    "weather_ensemble",
    "weather_event_map",
}


def _load_main_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/api/config-docs")
async def get_config_docs(section: str = Query("")):
    """
    获取 config_docs.json 中的参数说明。
    若提供 section，返回该 section 的展平式 {相对路径: 说明文字} 字典，
    供仪表盘参数说明 tooltip/展开显示使用。
    """
    docs_path = os.path.join(os.path.dirname(CONFIG_PATH), "config_docs.json")
    try:
        with open(docs_path, "r", encoding="utf-8") as f:
            all_docs = json.load(f)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if not section:
        return {"docs": all_docs}

    result: dict = {}

    # 1. Section-level description string
    if section in all_docs:
        val = all_docs[section]
        if isinstance(val, str):
            result["__section__"] = val
        elif isinstance(val, dict):
            for k, v in val.items():
                if not k.startswith("_") and isinstance(v, str):
                    result[k] = v

    # 2. Prefixed entries:  section.subpath → string or sub-dict
    prefix = section + "."
    for full_key, val in all_docs.items():
        if not full_key.startswith(prefix):
            continue
        rel_path = full_key[len(prefix) :]  # e.g. 'momentum_guard'
        if isinstance(val, str):
            # e.g. 'copytrader_v2.hard_max_bet_usdc' = 'doc...'
            result[rel_path] = val
        elif isinstance(val, dict):
            # T77: expose _description or description as the header doc for the rel_path key,
            # so the dashboard can show a human-readable label for nested sub-objects.
            _desc = val.get("_description") or val.get("description")
            if isinstance(_desc, str):
                result[rel_path] = _desc
            for subk, subv in val.items():
                if not subk.startswith("_") and isinstance(subv, str):
                    result[rel_path + "." + subk] = subv

    return {"section": section, "docs": result}


@router.get("/api/config")
async def get_config(section: str = Query("")):
    """
    获取配置。可选 section 参数过滤顶层 key（如 ?section=copytrader_v2）。
    """
    if section:
        if section == _COPYTRADER_SECTION:
            return {"section": section, "data": load_copytrader_v2_config()}
        if section in _V2_WEATHER_SECTIONS:
            cfg = _load_main_config()
            val = cfg.get(section)
            if val is None:
                return JSONResponse(
                    {"error": f"section '{section}' not found"}, status_code=404
                )
            return {"section": section, "data": val}
        return JSONResponse(
            {"error": f"section '{section}' has been removed from V2 dashboard"},
            status_code=410,
        )
    cfg = _load_main_config()
    data = {_COPYTRADER_SECTION: load_copytrader_v2_config()}
    for sec in sorted(_V2_WEATHER_SECTIONS):
        if sec in cfg:
            data[sec] = cfg[sec]
    return {"data": data}


@router.patch("/api/config")
async def update_config(request: Request):
    """
    修改配置参数。请求体 JSON 格式：
    {"updates": {"copytrader_v2.default_base_usdc": 5.0, ...}}
    支持点分路径，自动备份后写入。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    updates = body.get("updates")
    if not isinstance(updates, dict) or not updates:
        return JSONResponse(
            {"error": "need 'updates' dict with key-value pairs"}, status_code=400
        )

    ALLOWED_SECTIONS = {_COPYTRADER_SECTION, *_V2_WEATHER_SECTIONS}
    BLOCKED_KEYS = {
        "dry_run",
        "executor",
        "api_key",
        "private_key",
        "secret",
        "proxy_address",
        "clob_api_url",
        "base_url",
        "token",
        "password",
    }

    errors = []
    for key_path in updates:
        top = key_path.split(".")[0]
        leaf = key_path.split(".")[-1].lower()
        if top not in ALLOWED_SECTIONS:
            errors.append(f"'{key_path}': section '{top}' not in allowed list")
        if any(bk in leaf for bk in BLOCKED_KEYS):
            errors.append(f"'{key_path}': contains blocked keyword")
    if errors:
        return JSONResponse(
            {"error": "blocked keys", "details": errors}, status_code=403
        )

    try:
        backup_path = backup_config()
    except Exception as e:
        return JSONResponse({"error": f"backup failed: {e}"}, status_code=500)

    try:
        main_cfg = _load_main_config()
        copytrader_updates = {
            key_path.split(".", 1)[1]: new_val
            for key_path, new_val in updates.items()
            if key_path.startswith(_COPYTRADER_SECTION + ".")
        }
        applied = {}
        if copytrader_updates:
            applied.update(update_copytrader_v2_config(copytrader_updates))

        for key_path, new_val in updates.items():
            top = key_path.split(".")[0]
            if top not in _V2_WEATHER_SECTIONS:
                continue

            keys = key_path.split(".")
            target = main_cfg
            for k in keys[:-1]:
                if isinstance(target, dict) and k in target:
                    target = target[k]
                else:
                    errors.append(f"'{key_path}': path not found")
                    break
            else:
                leaf_key = keys[-1]
                if isinstance(target, dict) and leaf_key in target:
                    old_val = target[leaf_key]
                    if old_val is not None and not isinstance(new_val, type(old_val)):
                        if isinstance(old_val, (int, float)) and isinstance(
                            new_val, (int, float)
                        ):
                            new_val = type(old_val)(new_val)
                        else:
                            errors.append(
                                f"'{key_path}': type mismatch "
                                f"(old={type(old_val).__name__}, new={type(new_val).__name__})"
                            )
                            continue
                    target[leaf_key] = new_val
                    applied[key_path] = {"old": old_val, "new": new_val}
                else:
                    errors.append(f"'{key_path}': key '{leaf_key}' not found")

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(main_cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")

        if not applied:
            return JSONResponse(
                {"error": "no changes applied", "details": errors}, status_code=400
            )

        logger.info(
            f"[ConfigAPI] V2 配置已更新: {list(applied.keys())} | backup={backup_path} | copytrader_target={COPYTRADER_V2_CONFIG_PATH}"
        )

    except Exception as e:
        return JSONResponse({"error": f"update failed: {e}"}, status_code=500)

    return {
        "status": "ok",
        "applied": applied,
        "errors": errors if errors else None,
        "backup": backup_path,
        "note": "config热加载将在下一轮生效",
    }


@router.post("/api/toggle-dry-run")
async def toggle_dry_run(request: Request):
    """
    [已禁用] dry_run 切换接口已永久关闭。

    安全策略：dry_run 只能通过以下方式修改：
            1. 手动编辑 VPS 上的 /opt/polymarket_bot_v2/.env 中的 DRY_RUN 变量
      2. 重启服务后生效

    禁用原因：通过 API 或脚本改变 dry_run 曾导致真实持仓成为孤儿单（2026-03-23事故）。
    """
    logger.warning(
        "[DryRunToggle] ⚠️ 拒绝 dry_run 切换请求: 此 API 已永久禁用。"
        " 请手动编辑 VPS .env 文件中的 DRY_RUN 变量。"
    )
    return JSONResponse(
        {
            "error": "此接口已永久禁用",
            "reason": "dry_run 只能通过手动编辑 VPS .env 文件修改，不允许 API 远程切换",
            "how_to_change": "SSH 到 VPS → nano /opt/polymarket_bot_v2/.env → 修改 DRY_RUN=true/false → sudo systemctl restart polymarket-bot-v2",
        },
        status_code=410,  # 410 Gone - 永久移除
    )


@router.get("/api/smart_capital/preview")
async def smart_capital_preview(balance: float = Query(10.0)):
    """
    T70: SmartCapitalEngine 实时预览接口。

    根据给定余额返回当前匹配的 Tier、各赛道最大下注额和暂停状态。
    只读，不修改配置。
    """
    if balance <= 0:
        return JSONResponse({"error": "balance must be > 0"}, status_code=400)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    sc_cfg = cfg.get("smart_capital", {})
    # T134追加: SCE 是系统基建层，永远启用，不再检查 enabled

    tiers = sc_cfg.get("tiers") or []
    matched_tier: dict = tiers[-1] if tiers else {}
    for t in tiers:
        try:
            if balance <= float(t.get("max_balance", 999999)):
                matched_tier = t
                break
        except (TypeError, ValueError):
            continue

    # hard_max_bet_usdc 从 copytrader_v2 隔离配置读取
    hard_max = float(load_copytrader_v2_config().get("hard_max_bet_usdc", 25.0))
    max_bet_pct = float(matched_tier.get("max_bet_pct", 0.08))
    mults: dict = matched_tier.get("category_multipliers") or {}

    cat_results: dict = {}
    for cat in ["weather", "sports", "crypto", "default"]:
        try:
            mult = float(mults.get(cat, mults.get("default", 1.0)))
        except (TypeError, ValueError):
            mult = 1.0
        usdc = min(balance * max_bet_pct * mult, hard_max)
        cat_results[cat] = {"multiplier": round(mult, 4), "max_usdc": round(usdc, 4)}

    return {
        "enabled": True,
        "balance": balance,
        "tier_name": matched_tier.get("name", "unknown"),
        "max_bet_pct": max_bet_pct,
        "max_bet_pct_pct": f"{max_bet_pct * 100:.1f}%",
        "hard_max_usdc": hard_max,
        "categories": cat_results,
        "pause": sc_cfg.get("pause", {}),
    }


@router.get("/api/emergency-freeze")
async def get_emergency_freeze():
    """T99: 查询当前紧急冻结状态。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        frozen = bool(cfg.get("risk", {}).get("emergency_freeze", False))
        return {"frozen": frozen, "status": "frozen" if frozen else "normal"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/emergency-freeze")
async def set_emergency_freeze(request: Request):
    """T99: 设置/解除紧急冻结。
    Body: {"freeze": true/false}
    效果：修改 risk.emergency_freeze，config 热加载在下个周期生效（约8分钟内）。
    权限：T901 起要求 admin / super_admin（全局交易开关不可由普通用户操作）。
    注意：此操作不需要 TOTP 验证（快速保护用途）。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    freeze = body.get("freeze")
    if not isinstance(freeze, bool):
        return JSONResponse(
            {"error": "body must contain bool field 'freeze'"}, status_code=400
        )

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return JSONResponse({"error": f"Failed to read config: {e}"}, status_code=500)

    backup_path = backup_config()

    if "risk" not in cfg:
        cfg["risk"] = {}
    cfg["risk"]["emergency_freeze"] = freeze

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return JSONResponse({"error": f"Failed to write config: {e}"}, status_code=500)

    action = "⚡ 紧急冻结已启动" if freeze else "✅ 紧急冻结已解除"
    logger.warning(f"[EmergencyFreeze] {action} (freeze={freeze})")
    return {
        "status": "ok",
        "frozen": freeze,
        "action": action,
        "backup": backup_path,
        "note": "config热加载将在下一轮生效（约8分钟内）",
    }
