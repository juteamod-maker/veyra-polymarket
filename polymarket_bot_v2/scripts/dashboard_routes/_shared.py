"""
_shared.py — 仪表盘共享常量、工具函数、DB helpers
所有 route 模块从这里导入，避免循环依赖。
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.config_loader import backup_config, get  # noqa: F401

# ── 路径 ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent  # polymarket_bot_v2/
DB_PATH = ROOT / "data" / "polymarket.db"
LOG_DIR = ROOT / "logs"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CONFIG_PATH = ROOT / "config" / "config.json"
ENV_PATH = ROOT / ".env"

# ── 认证 ──────────────────────────────────────────────────────────────
# T474/T479: TOTP / DASHBOARD_KEY 旁路认证已废弃。仅 user_session cookie 是合法身份通道。
# T479: DASHBOARD_KEY 全面下线，user_session HMAC 仅从 USER_SESSION_SECRET 派生。
USER_SESSION_SECRET = os.environ.get("USER_SESSION_SECRET", "")

logger = logging.getLogger("web_dashboard")


# ── IP 永久黑名单（持久化 JSON）────────────────────────────────────

BLACKLIST_PATH = ROOT / "data" / "ip_blacklist.json"
SECURITY_LOG_PATH = ROOT / "data" / "security_events.json"
_MAX_SECURITY_EVENTS = 500  # 保留最近 N 条安全事件


class IPBlacklist:
    """持久化 IP 黑名单 + 白名单（支持子网匹配）+ 安全事件日志。"""

    _VALID_MODES = ("exact", "/24", "/16")

    def __init__(self, path: Path = BLACKLIST_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "blacklist" in raw:
                    # 兼容旧格式：将 whitelist 中的纯 IP 字符串升级为 dict
                    wl = raw.get("whitelist", [])
                    migrated = []
                    for entry in wl:
                        if isinstance(entry, str):
                            migrated.append({"ip": entry, "mode": "exact"})
                        elif isinstance(entry, dict) and "ip" in entry:
                            migrated.append(entry)
                    raw["whitelist"] = migrated
                    return raw
            except (json.JSONDecodeError, OSError):
                logger.warning("[Security] 黑名单文件损坏，重建")
        return {"blacklist": {}, "whitelist": [], "auto_ban_threshold": 3}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("[Security] 黑名单写入失败: %s", e)

    @staticmethod
    def _ip_matches_entry(ip: str, entry: dict) -> bool:
        """检查 IP 是否匹配白名单条目（精确 / /24 / /16）。"""
        try:
            addr = ipaddress.ip_address(ip)
            entry_ip = entry.get("ip", "")
            mode = entry.get("mode", "exact")
            if mode == "exact":
                return ip == entry_ip
            elif mode == "/24":
                network = ipaddress.ip_network(f"{entry_ip}/24", strict=False)
                return addr in network
            elif mode == "/16":
                network = ipaddress.ip_network(f"{entry_ip}/16", strict=False)
                return addr in network
        except (ValueError, TypeError):
            pass
        return False

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            return ip in self._data["blacklist"]

    def is_whitelisted(self, ip: str) -> bool:
        with self._lock:
            wl = self._data.get("whitelist", [])
            for entry in wl:
                if self._ip_matches_entry(ip, entry):
                    mode = entry.get("mode", "exact")
                    if mode != "exact":
                        logger.debug(
                            "[Security] IP %s 匹配白名单 %s (%s模式)",
                            ip,
                            entry.get("ip"),
                            mode,
                        )
                    return True
            return False

    def add(self, ip: str, reason: str, source: str = "manual") -> bool:
        with self._lock:
            if self._is_whitelisted_nolock(ip):
                return False
            if ip in self._data["blacklist"]:
                return False
            self._data["blacklist"][ip] = {
                "reason": reason,
                "source": source,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self._save()
        _log_security_event("blacklist_add", ip, reason, source)
        logger.warning("[Security] IP %s 加入永久黑名单: %s (%s)", ip, reason, source)
        return True

    def _is_whitelisted_nolock(self, ip: str) -> bool:
        """不加锁版本（供内部已持锁方法调用）。"""
        wl = self._data.get("whitelist", [])
        for entry in wl:
            if self._ip_matches_entry(ip, entry):
                return True
        return False

    def remove(self, ip: str) -> bool:
        with self._lock:
            if ip not in self._data["blacklist"]:
                return False
            del self._data["blacklist"][ip]
            self._save()
        _log_security_event("blacklist_remove", ip, "手动移除", "manual")
        logger.info("[Security] IP %s 从黑名单移除", ip)
        return True

    def list_all(self) -> dict:
        with self._lock:
            return dict(self._data["blacklist"])

    def add_whitelist(self, ip: str, mode: str = "/24") -> bool:
        if mode not in self._VALID_MODES:
            mode = "/24"
        with self._lock:
            wl = self._data.setdefault("whitelist", [])
            # 检查是否已存在相同 IP
            for entry in wl:
                if entry.get("ip") == ip:
                    return False
            new_entry = {
                "ip": ip,
                "mode": mode,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            wl.append(new_entry)
            # 清理黑名单中所有被新白名单条目覆盖的 IP（支持子网匹配）
            # 例如添加 10.1.0.0/16 白名单时，10.1.x.x 全部从黑名单移除
            removed_ips = [
                bl_ip
                for bl_ip in list(self._data["blacklist"].keys())
                if self._ip_matches_entry(bl_ip, new_entry)
            ]
            for bl_ip in removed_ips:
                del self._data["blacklist"][bl_ip]
                logger.info(
                    "[Security] IP %s 因命中白名单 %s (%s模式) 自动从黑名单移除",
                    bl_ip,
                    ip,
                    mode,
                )
            self._save()
        _log_security_event("whitelist_add", ip, f"加入白名单 (模式: {mode})", "manual")
        if removed_ips:
            for bl_ip in removed_ips:
                _log_security_event(
                    "blacklist_remove",
                    bl_ip,
                    f"白名单 {ip}/{mode} 覆盖自动移除",
                    "auto_whitelist",
                )
        logger.info("[Security] IP %s 加入白名单 (模式: %s)", ip, mode)
        return True

    def remove_whitelist(self, ip: str) -> bool:
        with self._lock:
            wl = self._data.setdefault("whitelist", [])
            for i, entry in enumerate(wl):
                if entry.get("ip") == ip:
                    wl.pop(i)
                    self._save()
                    return True
            return False

    def get_whitelist(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("whitelist", []))

    def get_auto_ban_threshold(self) -> int:
        with self._lock:
            return self._data.get("auto_ban_threshold", 3)

    def set_auto_ban_threshold(self, n: int) -> None:
        with self._lock:
            self._data["auto_ban_threshold"] = max(1, n)
            self._save()


def escape_script_json(value: Any) -> str:
    """把已序列化的 JSON 字符串安全化，以便嵌入 <script> 标签。

    T901 安全修复：json.dumps 不会转义 '<'，因此上游文本（市场标题、
    LLM 翻译等）中若出现 '</script>' 就会提前闭合脚本标签并造成 XSS。
    这里把 '</' 转义为 '<\\/'：JSON 语义完全等价，但无法再闭合标签。
    """
    return str(value if value is not None else "").replace("</", "<\\/")


# ── 服务端 TOTP MFA（T902）─────────────────────────────────────────────
# 后台 admin_v3 已实现同一套 TOTP 原语；此处为「前台用户登录」自包含实现，
# 不依赖 admin_v3（后台模块已私有、不发布）。依赖 pyotp / qrcode（均在 requirements.txt）。

try:
    import pyotp as _pyotp
    import qrcode as _qrcode
except Exception:  # pragma: no cover - 依赖缺失时仅禁用 TOTP 端点
    _pyotp = None  # type: ignore[assignment]
    _qrcode = None  # type: ignore[assignment]

_MFA_TOTP_ISSUER = "VeyraTrade"
_MFA_COOKIE = "mfa_verified"
_MFA_COOKIE_TTL = 30 * 60


def generate_totp_secret() -> str:
    """Base32 TOTP secret（Google Authenticator 兼容）。"""
    if _pyotp is None:
        raise RuntimeError("pyotp is not installed")
    return _pyotp.random_base32()


def totp_provisioning_uri(secret: str, account: str) -> str:
    """otpauth:// URI。"""
    if _pyotp is None:
        raise RuntimeError("pyotp is not installed")
    return _pyotp.TOTP(secret).provisioning_uri(
        name=account, issuer_name=_MFA_TOTP_ISSUER
    )


def totp_qr_svg(uri: str) -> str:
    """内联 SVG（无外部资源，适合前端直接渲染）。"""
    if _qrcode is None:
        return ""
    qr = _qrcode.QRCode(border=2, box_size=8)
    qr.add_data(uri)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    box = 8
    pad = 16
    size = n * box + pad * 2
    cells = []
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                cells.append(
                    f'<rect x="{pad + x * box}" y="{pad + y * box}" width="{box}" height="{box}" fill="#000"/>'
                )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#fff"/>' + "".join(cells) + "</svg>"
    )


def verify_totp(secret: str, code: str) -> bool:
    """校验 6 位 TOTP 码（±1 时间窗）。"""
    if _pyotp is None or not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    try:
        return bool(_pyotp.TOTP(secret).verify(code, valid_window=1))
    except Exception:
        return False


def mfa_cookie_sign(wallet_address: str) -> str:
    """为 MFA 已验证状态签发不可伪造的 cookie 值。"""
    key = _user_session_hmac_key()
    payload = f"{wallet_address}:mfa_verify_v1"
    mac = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{wallet_address}.{mac}"


def mfa_cookie_verify(token: str, wallet_address: str) -> bool:
    """校验 MFA cookie（防篡改；失败返回 False）。"""
    if not token or not wallet_address:
        return False
    try:
        want_wallet, mac = token.rsplit(".", 1)
    except ValueError:
        return False
    if want_wallet != wallet_address:
        return False
    expected = mfa_cookie_sign(wallet_address)
    return hmac.compare_digest(token, expected)


def _log_security_event(
    event_type: str, ip: str, detail: str, source: str = ""
) -> None:
    """追加安全事件到持久化日志。"""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "ip": ip,
        "detail": detail,
        "source": source,
    }
    try:
        events: list = []
        if SECURITY_LOG_PATH.exists():
            raw = SECURITY_LOG_PATH.read_text(encoding="utf-8").strip()
            if raw:
                events = json.loads(raw)
                if not isinstance(events, list):
                    events = []
        events.append(entry)
        events = events[-_MAX_SECURITY_EVENTS:]
        SECURITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECURITY_LOG_PATH.write_text(
            json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("[Security] 安全事件日志写入失败: %s", exc)


def get_security_events(limit: int = 50) -> list[dict]:
    """读取最近 N 条安全事件。"""
    if not SECURITY_LOG_PATH.exists():
        return []
    try:
        raw = SECURITY_LOG_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        events = json.loads(raw)
        if not isinstance(events, list):
            return []
        return events[-limit:][::-1]  # 最新在前
    except (json.JSONDecodeError, OSError):
        return []


# 全局黑名单实例
ip_blacklist = IPBlacklist()


# ── IP Rate Limiter（防穷举爆破 + 自动升级黑名单）──────────────────────


class IPRateLimiter:
    """内存级 IP 速率限制器，临时封禁达阈值自动升级为永久黑名单。"""

    def __init__(
        self, max_fails: int = 10, window_sec: int = 300, block_sec: int = 900
    ) -> None:
        self.max_fails = max_fails
        self.window_sec = window_sec
        self.block_sec = block_sec
        self._records: dict[str, dict] = {}
        self._lock = threading.Lock()

    def is_blocked(self, ip: str) -> bool:
        """检查 IP 是否在封禁期（不含永久黑名单，黑名单由中间件优先检查）。"""
        with self._lock:
            self._cleanup()
            rec = self._records.get(ip)
            if not rec:
                return False
            return rec.get("blocked_until", 0) > time.time()

    def record_fail(self, ip: str) -> bool:
        """记录一次认证失败，返回是否触发封禁。临时封禁次数达阈值自动加入永久黑名单。"""
        now = time.time()
        with self._lock:
            if ip not in self._records:
                self._records[ip] = {
                    "fails": [],
                    "blocked_until": 0,
                    "ban_count": 0,
                }
            rec = self._records[ip]
            rec["fails"] = [t for t in rec["fails"] if now - t < self.window_sec]
            rec["fails"].append(now)
            if len(rec["fails"]) >= self.max_fails:
                rec["blocked_until"] = now + self.block_sec
                rec["fails"] = []
                rec["ban_count"] = rec.get("ban_count", 0) + 1
                ban_count = rec["ban_count"]
                logger.warning(
                    "[Security] IP %s 被临时封禁 %d 秒（第 %d 次封禁）",
                    ip,
                    self.block_sec,
                    ban_count,
                )
                _log_security_event(
                    "temp_ban",
                    ip,
                    f"第 {ban_count} 次临时封禁（{self.max_fails} 次认证失败）",
                )
                # 自动升级：临时封禁次数达阈值 → 永久黑名单
                threshold = ip_blacklist.get_auto_ban_threshold()
                if ban_count >= threshold and not ip_blacklist.is_whitelisted(ip):
                    ip_blacklist.add(
                        ip,
                        f"自动升级: {ban_count} 次临时封禁",
                        source="auto",
                    )
                return True
            else:
                fail_count = len(rec["fails"])
                if fail_count % 3 == 0:
                    _log_security_event(
                        "auth_fail",
                        ip,
                        f"认证失败累计 {fail_count}/{self.max_fails}",
                    )
            return False

    def get_temp_bans(self) -> list[dict]:
        """返回当前所有临时封禁的 IP 信息。"""
        now = time.time()
        result = []
        with self._lock:
            for ip, rec in self._records.items():
                blocked_until = rec.get("blocked_until", 0)
                if blocked_until > now:
                    result.append(
                        {
                            "ip": ip,
                            "remaining_sec": int(blocked_until - now),
                            "ban_count": rec.get("ban_count", 0),
                            "fail_count": len(rec.get("fails", [])),
                        }
                    )
        return result

    def clear_ip(self, ip: str) -> bool:
        """清除指定 IP 的临时限流记录。"""
        if not ip:
            return False
        with self._lock:
            existed = ip in self._records
            if existed:
                del self._records[ip]
            return existed

    def _cleanup(self) -> None:
        """清理过期记录，防止内存泄漏。"""
        now = time.time()
        expired = [
            ip
            for ip, rec in self._records.items()
            if rec.get("blocked_until", 0) < now
            and all(now - t >= self.window_sec for t in rec.get("fails", []))
        ]
        for ip in expired:
            del self._records[ip]


# 全局实例：5分钟内3次失败 → 临时封禁15分钟；临时封禁3次 → 永久黑名单
ip_rate_limiter = IPRateLimiter(max_fails=3, window_sec=300, block_sec=900)


# ── User Session helpers (T257 SaaS 多用户认证；T474 后为唯一身份通道) ─
USER_SESSION_DAYS = 7
_USER_SESSION_HMAC_KEY = None
_user_session_lock = threading.Lock()


def _user_session_hmac_key() -> bytes:
    """派生 user_session HMAC key (USER_SESSION_SECRET)。"""
    global _USER_SESSION_HMAC_KEY
    if _USER_SESSION_HMAC_KEY is None:
        _USER_SESSION_HMAC_KEY = hashlib.sha256(
            (USER_SESSION_SECRET + ":user_session_v1").encode()
        ).digest()
    return _USER_SESSION_HMAC_KEY


def _ensure_user_tables() -> None:
    """确保 users 和 user_sessions 表存在（幂等）。"""
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                wallet_address TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'manual_user',
                display_name TEXT,
                avatar_url TEXT,
                email TEXT,
                email_verified INTEGER NOT NULL DEFAULT 0,
                privy_user_id TEXT,
                auth_provider TEXT,
                linked_wallets_json TEXT,
                polymarket_wallet TEXT,
                created_at TEXT NOT NULL,
                last_login_at TEXT,
                source_ip TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'manual_user',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_used_at TEXT,
                source_ip TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_user_sessions_wallet
                ON user_sessions(wallet_address);
            """
        )
        # 向后兼容：旧数据库平滑补列（PostgreSQL 需要 IF NOT EXISTS 避免事务中断）
        for ddl in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_user_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_wallets_json TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_embedded_wallet_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_linked_accounts_json TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_signer_status TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_signer_policies_json TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_signer_error TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privy_signer_updated_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS polymarket_wallet TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified INTEGER NOT NULL DEFAULT 0",
            # T287 Wave 2: Privy EOA + Gnosis Safe 双地址绑定
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS main_wallet TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS funder_address TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_type INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_deployed_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS api_creds_encrypted TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pm_bind_tx TEXT",
            # T287 Wave 3.6 (F): Privy Policy-Based MFA \u672c\u5730\u7f13\u5b58
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_methods JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_last_verified_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_updated_at TIMESTAMP",
            # T902: 服务端 TOTP MFA（前台用户登录唯一强制因子）
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_verified_at TIMESTAMP",
            # Deposit Wallet Migration (May 2026): distinguish 'safe'/'proxy' vs 'deposit_wallet'
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_type TEXT NOT NULL DEFAULT 'safe'",
            # T881 (2026-05-06): default new rows to deposit_wallet path. ALTER COLUMN
            # idempotent for already-migrated columns; skipped silently on first run.
            "ALTER TABLE users ALTER COLUMN wallet_type SET DEFAULT 'deposit_wallet'",
            "ALTER TABLE users ALTER COLUMN signature_type SET DEFAULT 3",
            "CREATE INDEX IF NOT EXISTS idx_users_main_wallet ON users(main_wallet)",
            "CREATE INDEX IF NOT EXISTS idx_users_funder_address ON users(funder_address)",
        ):
            try:
                conn.execute(ddl)
            except (OperationalError, Exception) as e:
                err_msg = str(e).lower()
                if (
                    "already exists" not in err_msg
                    and "duplicate column" not in err_msg
                ):
                    raise


def create_user_session(
    wallet_address: str, role: str = "manual_user", source_ip: str = ""
) -> str:
    """创建用户会话：写入 DB，返回会话 token。高级角色（super_admin/admin）不被降级。"""
    import secrets as _secrets

    _ensure_user_tables()
    token = _secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    from datetime import timedelta

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=USER_SESSION_DAYS)
    ).isoformat()

    with db_connect() as conn:
        # 若用户已有更高角色（super_admin/admin），保留高角色，不降级
        existing = conn.execute(
            "SELECT role FROM users WHERE wallet_address=?", (wallet_address,)
        ).fetchone()
        if existing:
            existing_role = existing["role"] or "manual_user"
            if existing_role in ("super_admin", "admin") and role == "manual_user":
                role = existing_role

        # 更新或创建用户记录
        conn.execute(
            """
            INSERT INTO users(wallet_address, role, created_at, last_login_at, source_ip, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                last_login_at=excluded.last_login_at,
                source_ip=excluded.source_ip,
                updated_at=excluded.updated_at
            """,
            (wallet_address, role, now, now, source_ip, now),
        )
        conn.execute(
            """
            INSERT INTO user_sessions(token, wallet_address, role, created_at, expires_at, last_used_at, source_ip)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (token, wallet_address, role, now, expires_at, now, source_ip),
        )
        conn.commit()

        # T877 / T881 (2026-05-06) — Silent Deposit Wallet provisioning.
        # Pre-derive and persist the deterministic Deposit Wallet address for
        # any non-super_admin user. Runs in the same TX after session insert
        # so /v2/deposit can render the address on first hit without paying
        # derivation cost in the request path.
        #
        # T881: removed the `if not current_pm.strip()` skip guard.
        # The previous logic skipped users whose `polymarket_wallet` had
        # already been populated by an earlier discovery pass (e.g. a leaked
        # legacy Safe address from Polymarket's public profile API), which
        # broke the silent-provisioning promise. We now ALWAYS overwrite
        # with the deterministic deposit wallet for non-privileged roles.
        # super_admin / admin retain their existing binding so we don't
        # disturb the operator's pre-cutover Safe.
        try:
            existing_role = (existing["role"] if existing else None) or role
            existing_role_norm = str(existing_role or "").strip().lower()
            if existing_role_norm in ("super_admin", "admin"):
                logger.info(
                    "[DepositWallet] skipping silent provisioning for "
                    "privileged role=%s wallet=%s",
                    existing_role_norm,
                    wallet_address,
                )
            else:
                from src.utils.pm_deposit_wallet import (
                    derive_deposit_wallet_address,
                )

                derived = derive_deposit_wallet_address(wallet_address)
                logger.info(
                    "[DepositWallet] silent provisioning wallet=%s eoa=%s "
                    "deposit=%s role=%s",
                    wallet_address,
                    wallet_address,
                    derived,
                    existing_role_norm or role,
                )
                conn.execute(
                    """
                    UPDATE users SET
                        polymarket_wallet=?,
                        main_wallet=COALESCE(NULLIF(main_wallet, ''), ?),
                        funder_address=?,
                        signature_type=3,
                        wallet_type='deposit_wallet',
                        updated_at=?
                    WHERE wallet_address=?
                    """,
                    (derived, wallet_address, derived, now, wallet_address),
                )
                conn.execute(
                    """
                    INSERT INTO user_pm_profiles(
                        wallet_address, login_wallet, polymarket_wallet, proxy_wallet,
                        linked_wallets_json, discovery_method, discovery_confidence,
                        profile_json, last_synced_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', 'create2_derived', 100.0, '{}', ?, '', ?, ?)
                    ON CONFLICT(wallet_address) DO UPDATE SET
                        polymarket_wallet=excluded.polymarket_wallet,
                        proxy_wallet=excluded.proxy_wallet,
                        discovery_method=excluded.discovery_method,
                        discovery_confidence=excluded.discovery_confidence,
                        last_synced_at=excluded.last_synced_at,
                        updated_at=excluded.updated_at
                    """,
                    (wallet_address, wallet_address, derived, derived, now, now, now),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001 — silent provisioning is best-effort
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "[DepositWallet] silent provisioning failed wallet=%s err=%s",
                wallet_address,
                e,
            )
    return token


def verify_user_session(token: str) -> dict | None:
    """验证 user_session token，返回 {wallet_address, role} 或 None。"""
    if not token or len(token) < 16:
        return None
    _ensure_user_tables()
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT wallet_address, role, expires_at FROM user_sessions WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            # 过期：删除
            conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            conn.commit()
            return None
        # 更新最后使用时间
        conn.execute(
            "UPDATE user_sessions SET last_used_at=? WHERE token=?",
            (now, token),
        )
        conn.commit()
    return {"wallet_address": row["wallet_address"], "role": row["role"]}


def revoke_user_session(token: str) -> bool:
    """撤销用户会话（登出）。"""
    if not token:
        return False
    _ensure_user_tables()
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
        conn.commit()
        return cur.rowcount > 0


def get_request_identity(request: Any) -> dict | None:
    """T474: 单一身份通道 — user_session cookie。role 决定权限。

    super_admin/admin 角色用户即享有管理员权限；manual_user 仅普通用户权限。
    """
    token = request.cookies.get("user_session", "")
    if token:
        user_info = verify_user_session(token)
        if user_info:
            role = user_info.get("role") or "manual_user"
            is_admin = role in ("admin", "super_admin")
            return {
                "authenticated": True,
                "role": role,
                "wallet_address": user_info.get("wallet_address"),
                "is_admin": is_admin,
            }

    return None


# ── DB helpers ────────────────────────────────────────────────────────
# 使用 PostgreSQL 连接池（替代原 sqlite3 直连）
from src.utils.database import OperationalError  # noqa: E402
from src.utils.database import connect as _pg_connect  # noqa: E402


def db_connect():
    """Dashboard 路由 DB 连接上下文管理器（PostgreSQL）。"""
    return _pg_connect()


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _get_system_info() -> dict:
    """系统资源概览。"""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0].rstrip(":")] = int(parts[1])
            total = mem.get("MemTotal", 0) / 1024
            avail = mem.get("MemAvailable", 0) / 1024
            info["mem_total_mb"] = round(total)
            info["mem_used_mb"] = round(total - avail)
            info["mem_pct"] = (
                round((total - avail) / total * 100, 1) if total > 0 else 0
            )
    except Exception:
        pass
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            info["load_1m"] = float(parts[0])
            info["load_5m"] = float(parts[1])
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True, timeout=3
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            info["disk_total"] = parts[1]
            info["disk_used"] = parts[2]
            info["disk_pct"] = parts[4]
    except Exception:
        pass
    return info


# ── ENV 管理 helpers ──────────────────────────────────────────────────

_ENV_MANAGEABLE_KEYS = [
    # 交易市场
    "POLY_API_KEY",
    "POLY_SECRET",
    "POLY_PASSPHRASE",
    "POLY_PRIVATE_KEY",
    "POLY_BUILDER_CODE",
    # AI / 辅助 API（T866：DeepSeek 5-key 轮询）
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_0",
    "DEEPSEEK_API_KEY_1",
    "DEEPSEEK_API_KEY_2",
    "DEEPSEEK_API_KEY_3",
    "DEEPSEEK_API_KEY_4",
    "RAPIDAPI_KEY",
    # 通知
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    # PostgreSQL
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
    # Privy / Server Signer (T471 V2)
    "PRIVY_APP_ID",
    "PRIVY_APP_SECRET",
    "PRIVY_AUTHORIZATION_KEY_V2",
    "PRIVY_SERVER_SIGNER_ID",
    "PRIVY_SERVER_SIGNER_POLICY_IDS",
    # Polymarket Relayer
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
    # Runtime flag
    "PM_USE_V2",
]
# T479: DASHBOARD_KEY 已完全下线；USER_SESSION_SECRET 为底层安全种子，不在管理 UI 外露。
_ENV_READONLY_KEYS: set[str] = set()


def _mask_value(val: str) -> str:
    """将密钥值做脱敏处理，保留前2后2字符。"""
    if not val:
        return ""
    if len(val) <= 6:
        return "***"
    return val[:2] + "***" + val[-2:]


def _parse_env_file() -> dict[str, str]:
    """读取 .env 文件，返回 key→value 字典。"""
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        result[key] = value
    return result


def _write_env_file(env_data: dict[str, str]) -> None:
    """原子写入 .env 文件（先写临时文件再 rename）。"""
    import tempfile

    lines = []
    for k, v in env_data.items():
        if any(c in v for c in (" ", "'", '"', "#", "\n")):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k}="{escaped}"')
        else:
            lines.append(f"{k}={v}")
    content = "\n".join(lines) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(ENV_PATH.parent), prefix=".env_tmp_", suffix=""
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(ENV_PATH))
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
