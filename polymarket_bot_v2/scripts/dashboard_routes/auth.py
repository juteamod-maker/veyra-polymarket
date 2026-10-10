"""
dashboard_routes/auth.py — T474: TOTP / dashboard_key 认证已废弃。

T474（2026-04-27）起：管理员通过 Privy 登录获取 user_session cookie，role=admin
或 super_admin 即享有管理员权限。本模块保留为空 router 以便兼容旧的 import 链。
"""

from fastapi import APIRouter

router = APIRouter()
