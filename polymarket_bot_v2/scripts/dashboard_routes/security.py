"""
security.py — Dashboard 安全管理 API
IP 黑名单 / 白名单 / 临时封禁列表 / 安全事件日志 / 自动升级配置
"""

import ipaddress
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (get_security_events,
                                              ip_blacklist, ip_rate_limiter)

router = APIRouter(prefix="/api/security", tags=["security"])
logger = logging.getLogger("web_dashboard")


def _validate_ip(ip: str) -> bool:
    """校验 IP 地址格式。"""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


# ── 黑名单 CRUD ──────────────────────────────────────────────────────


@router.get("/blacklist")
async def get_blacklist():
    """获取所有永久黑名单 IP。"""
    return {"blacklist": ip_blacklist.list_all()}


@router.post("/blacklist")
async def add_to_blacklist(request: Request):
    """手动添加 IP 到永久黑名单。"""
    body = await request.json()
    ip = body.get("ip", "").strip()
    reason = body.get("reason", "手动添加").strip()
    if not ip or not _validate_ip(ip):
        return JSONResponse({"error": "无效的 IP 地址"}, status_code=400)
    if ip_blacklist.is_whitelisted(ip):
        return JSONResponse(
            {"error": f"IP {ip} 在白名单中，无法加入黑名单"}, status_code=400
        )
    ok = ip_blacklist.add(ip, reason, source="manual")
    if not ok:
        return JSONResponse({"error": f"IP {ip} 已在黑名单中"}, status_code=409)
    return {"ok": True, "ip": ip, "reason": reason}


@router.delete("/blacklist/{ip}")
async def remove_from_blacklist(ip: str):
    """从黑名单移除 IP。"""
    if not _validate_ip(ip):
        return JSONResponse({"error": "无效的 IP 地址"}, status_code=400)
    ok = ip_blacklist.remove(ip)
    if not ok:
        return JSONResponse({"error": f"IP {ip} 不在黑名单中"}, status_code=404)
    return {"ok": True, "ip": ip}


# ── 白名单 CRUD ──────────────────────────────────────────────────────


@router.get("/whitelist")
async def get_whitelist():
    """获取白名单 IP 列表（含匹配模式）。"""
    return {"whitelist": ip_blacklist.get_whitelist()}


@router.post("/whitelist")
async def add_to_whitelist(request: Request):
    """添加 IP 到白名单（支持匹配模式：exact / /24 / /16）。"""
    body = await request.json()
    ip = body.get("ip", "").strip()
    mode = body.get("mode", "/24").strip()
    if not ip or not _validate_ip(ip):
        return JSONResponse({"error": "无效的 IP 地址"}, status_code=400)
    if mode not in ("exact", "/24", "/16"):
        return JSONResponse({"error": "无效的匹配模式"}, status_code=400)
    ok = ip_blacklist.add_whitelist(ip, mode=mode)
    if not ok:
        return JSONResponse({"error": f"IP {ip} 已在白名单中"}, status_code=409)
    return {"ok": True, "ip": ip, "mode": mode}


@router.delete("/whitelist/{ip}")
async def remove_from_whitelist(ip: str):
    """从白名单移除 IP。"""
    if not _validate_ip(ip):
        return JSONResponse({"error": "无效的 IP 地址"}, status_code=400)
    ok = ip_blacklist.remove_whitelist(ip)
    if not ok:
        return JSONResponse({"error": f"IP {ip} 不在白名单中"}, status_code=404)
    return {"ok": True, "ip": ip}


# ── 临时封禁列表 ──────────────────────────────────────────────────────


@router.get("/temp-bans")
async def get_temp_bans():
    """获取当前临时封禁的 IP 列表。"""
    return {"temp_bans": ip_rate_limiter.get_temp_bans()}


@router.post("/promote-to-blacklist")
async def promote_to_blacklist(request: Request):
    """将临时封禁的 IP 升级为永久黑名单。"""
    body = await request.json()
    ip = body.get("ip", "").strip()
    if not ip or not _validate_ip(ip):
        return JSONResponse({"error": "无效的 IP 地址"}, status_code=400)
    ok = ip_blacklist.add(ip, "从临时封禁手动升级", source="manual_promote")
    if not ok:
        return JSONResponse({"error": f"IP {ip} 已在黑名单中"}, status_code=409)
    return {"ok": True, "ip": ip}


# ── 安全事件日志 ──────────────────────────────────────────────────────


@router.get("/events")
async def get_events(limit: int = 50):
    """获取最近 N 条安全事件。"""
    limit = min(max(1, limit), 500)
    return {"events": get_security_events(limit)}


# ── 自动升级配置 ──────────────────────────────────────────────────────


@router.get("/config")
async def get_security_config():
    """获取安全配置。"""
    return {
        "auto_ban_threshold": ip_blacklist.get_auto_ban_threshold(),
        "max_fails": ip_rate_limiter.max_fails,
        "block_sec": ip_rate_limiter.block_sec,
        "window_sec": ip_rate_limiter.window_sec,
    }


@router.patch("/config")
async def update_security_config(request: Request):
    """更新安全配置。"""
    body = await request.json()
    if "auto_ban_threshold" in body:
        val = int(body["auto_ban_threshold"])
        if val < 1 or val > 100:
            return JSONResponse(
                {"error": "auto_ban_threshold 范围 1-100"}, status_code=400
            )
        ip_blacklist.set_auto_ban_threshold(val)
    return {"ok": True, "auto_ban_threshold": ip_blacklist.get_auto_ban_threshold()}


# ── 综合概览 ──────────────────────────────────────────────────────────


@router.get("/overview")
async def security_overview():
    """安全概览：黑名单数 + 临时封禁数 + 白名单数 + 最近事件。"""
    bl = ip_blacklist.list_all()
    wl = ip_blacklist.get_whitelist()
    tb = ip_rate_limiter.get_temp_bans()
    events = get_security_events(10)
    return {
        "blacklist_count": len(bl),
        "whitelist_count": len(wl),
        "temp_ban_count": len(tb),
        "auto_ban_threshold": ip_blacklist.get_auto_ban_threshold(),
        "recent_events": events,
    }
