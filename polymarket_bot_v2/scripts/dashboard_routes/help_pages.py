"""
help_pages.py — 系统帮助手册管理路由

提供：
    - GET  /api/help-pages/{slug}   返回 Markdown（公共帮助页游客可读）
  - PUT  /api/help-pages/{slug}   保存 Markdown（仅 admin / super_admin）

T287 首个 slug = 'go'（指导手册页 /v2/go）。

存储：PostgreSQL 表
    system_help_pages(
        slug PK,
        title TEXT,
        title_i18n JSONB,
        markdown TEXT,
        markdown_i18n JSONB,
        updated_at,
        updated_by
    )。
首次读取时若不存在，自动 seed 默认 /go 内容（详尽的 VagusPoly 用户指导手册）。
"""

from __future__ import annotations

import json
import logging
import re
import asyncio  # T590 wave 4
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import db_connect, get_request_identity

logger = logging.getLogger("help_pages")
router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LANG_RE = re.compile(r"^[A-Za-z0-9-]{2,10}$")
_PUBLIC_HELP_SLUGS = frozenset({"go", "faq", "about", "privacy", "terms"})

# ── 默认 /go 指导手册 Markdown（首次安装时 seed） ─────────────────
_DEFAULT_GO_MARKDOWN = r"""# VagusPoly 使用指导手册

欢迎来到 **VagusPoly 自动化交易系统**。本页是针对尚未完成 Polymarket 资金钱包关联的新用户的完整入门说明。
请耐心阅读，按步骤完成授权后方可使用系统提供的交易与数据服务。

> ⚠️ 系统不接受任何手动输入的钱包地址。**资金钱包必须由系统通过 Privy + Gnosis Safe 自动派生**，这是本系统的核心安全策略，任何绕过方式均会被风控拦截。

## 一、系统工作模式简介

VagusPoly 基于 Polymarket 合约（Polygon 主网），采用 **Privy + Gnosis Safe 双层钱包架构**：

- **主钱包（main_wallet）**：Privy 托管的 EOA 签名钱包，仅用于下单签名，**不持有任何资金**。
- **资金钱包（funder_address）**：系统根据主钱包在链上确定性推导出的 Gnosis Safe 代理合约地址，所有 USDC 资金、持仓、结算全部在 Safe 内完成。
- **Builder Relayer**：Safe 部署由系统通过 Builder 中继 gasless 完成，用户无需持有 MATIC 或手动部署合约。

这种架构带来的核心好处：

1. 用户的签名私钥由 Privy 以硬件级加密方式托管，系统**永远不接触私钥**；
2. 资金与签名权限物理分离，即使 Privy 会话泄漏，攻击者也无法直接提走 Safe 内资金；
3. 所有订单成交、资金流动均在链上可审计。

## 二、注册与登录

### 步骤 1 — 选择登录方式

在首页 `/Index` 页点击「使用 Privy 登录并绑定钱包」按钮，会弹出 Privy 授权窗口。支持：

- **钱包登录**：MetaMask / Rainbow / Coinbase Wallet 等 Web3 钱包，推荐有链上操作经验的用户使用。
- **Email 登录**：输入邮箱接收 6 位验证码，Privy 会自动为你创建一个嵌入式钱包（Embedded EOA）。
- **未来扩展**：Google / X / Farcaster 授权（规划中）。

> 📌 **每次访问 `/Index` 页都会强制重新授权，系统不保存也不恢复任何旧会话**，这是为了防止用户卡在"已登录但未关联 PM"的死循环。

### 步骤 2 — 完成 PM 钱包绑定

授权成功后：

- 若本次主钱包已经绑定过 Polymarket 资金钱包 → 直接进入 `/v2` 仪表盘；
- 若本次主钱包是首次登录 → 系统会将你引导到本页 `/v2/go`，因为你还没有可用的资金钱包。

## 三、从 /go 页出发该做什么

### 3.1 立即完成绑定流程

点击页面顶部的「**授权关联钱包**」按钮回到 `/Index`，用同一个主钱包重新授权一次。系统会在 `/api/user/privy/verify` 中：

1. 读取你的 Privy EOA 地址作为 `main_wallet`；
2. 使用 CREATE2 算法确定性推导出你的 Gnosis Safe 地址作为 `funder_address`；
3. 若 Safe 合约尚未部署，系统会自动通过 Builder Relayer gasless 部署；
4. 派生该 Safe 的 CLOB L2 API 凭证并加密入库（不以明文形式存储）；
5. 绑定关系写入 `user_pm_profiles` 表后，你的账户即刻可用。

### 3.2 向 Safe 资金钱包充值

绑定完成后，在仪表盘「钱包信息」卡片会显示两个地址：

- 签名钱包（Privy EOA）
- 资金钱包（Gnosis Safe）**← 往这个地址充值原生 USDC（Polygon 链）**

建议首次充值 20~50 USDC 作为测试，确认功能正常后再追加资金。

> 💡 **充值注意事项**：
> - 仅支持 **原生 USDC**（0x3c49…）；
> - 务必使用 **Polygon 主网**，跨链或 ETH 主网转账会造成资金丢失；
> - 首次充值到新 Safe 会触发 Safe Proxy 自动部署（Polymarket 合约原生支持，无需额外操作）。

## 四、允许的页面与功能

未绑定 PM 钱包时：

| 页面 | 允许访问 | 说明 |
|---|---|---|
| `/Index` | ✅ | 登录入口 |
| `/v2/go` | ✅ | 本页指导手册 |
| `/v2/*` 其他 | ❌ | 会被 `pm_guard` 弹窗阻挡 |

绑定成功后：

| 页面 | 功能 |
|---|---|
| `/v2` | Dashboard 总览：资产、PnL、活跃持仓、通知 |
| `/v2/user` | 个人资料、钱包绑定详情、订阅套餐 |
| `/v2/trades` | 交易记录、订单历史、手动下单 |
| `/v2/config` | 积分、手续费、策略配置 |
| `/v2/refer` | 推荐好友、佣金分成 |

## 五、风控与资金安全

系统设计遵循 **"本金安全 > 可持续正收益 > 运维简单"** 的优先级，执行如下保护：

- **每日最大亏损 50%** 自动暂停交易，余额≤\$50 时降至 25%，绝对下限 \$5；
- **累计亏损 40%** 自动暂停，小余额降至 50%，绝对下限 \$10；
- **单笔投注不超过账户余额的 3%**；
- **Ensemble 信号**：至少 2 个 AI 模型共识 + EV ≥ 阈值才触发买入；
- **资金钱包私钥用户自己掌握**（Privy 托管），系统侧只有签名请求权限。

## 六、常见问题（FAQ）

### Q1：我可以手动输入我的 Polymarket 钱包地址吗？
**不可以**。从 T287 版本起，系统**永久关闭**手动地址绑定功能，只接受 Privy + Safe 自动派生。

### Q2：如果我换了一个浏览器，需要重新绑定吗？
不需要。PM 钱包绑定是按主钱包地址维度保存在数据库中，只要用同一个主钱包（或同一 Email）重新登录 Privy，系统会自动恢复绑定关系。

### Q3：Email 登录的 Privy 嵌入式钱包安全吗？
安全。Privy 使用 MPC（多方计算）技术把私钥切片托管在可信环境中，单点泄漏无法还原完整私钥。即便如此，**资金仍然存放在 Gnosis Safe 而非 Privy EOA**，双层防护。

### Q4：为什么每次打开 /Index 都要重新点登录按钮？
这是刻意为之的安全设计。自动恢复会话会导致"主钱包已授权但未关联 PM → 跳转到仪表盘 → 被 pm_guard 挡住 → 无法退出"的死循环。强制重新授权使流程线性清晰。

### Q5：我的 Safe 合约显示未部署，如何手动部署？
无需手动部署。向该 Safe 地址充入 1 USDC 就会自动触发部署，Polymarket 合约原生支持。gas 由 Builder Relayer 代付。

## 七、联系与反馈

- 系统状态页：`/v2`
- 技术支持：请通过管理后台 → 「用户工单」提交（仅绑定成功后可用）
- 紧急故障：超级管理员会在弹窗内发布系统维护通知

---

祝你交易顺利。所有功能开启，从关联你的 Polymarket 资金钱包开始。

*最后更新：本段内容由超级管理员在管理后台可视化编辑。*
"""

# T407: Language variants for help pages
_DEFAULT_GO_MARKDOWN_ZH = _DEFAULT_GO_MARKDOWN

_DEFAULT_GO_MARKDOWN_EN = r"""# VeYra User Guide

Welcome to the **VeYra Polymarket Trading System**. This page provides complete onboarding instructions for new users who haven't yet completed Polymarket wallet linking.
Please read carefully and follow each step to authorize your account before using the trading and data services.

> ⚠️ The system does not accept manually entered wallet addresses. **Funding wallets must be automatically derived by the system via Privy + Gnosis Safe**, which is the core security strategy of this system.

## I. System Overview

VeYra uses **Privy + Gnosis Safe dual-wallet architecture** for Polymarket trading on Polygon mainnet:

- **Main Wallet**: Privy-managed EOA, signs orders only, holds no funds.
- **Funding Wallet**: Deterministically derived Gnosis Safe, holds all USDC and positions.
- **Builder Relayer**: Gasless Safe deployment via Builder infrastructure.

Benefits:

1. Private keys never touch our servers (Privy hardware encryption).
2. Funds physically separated from signing permissions.
3. All on-chain activity is auditable.

## II. Getting Started

### Authorization

Click "Authorize Wallet" on `/Index` and choose:
- Wallet login (MetaMask, Rainbow, etc.)
- Email login (auto-creates embedded wallet via Privy)

Every visit requires re-auth (prevents session limbo states).

### Fund Linking

After first authorization, return to this page and click "Authorize" again. System will:
1. Derive your Gnosis Safe from your main wallet
2. Deploy Safe gaslessly if needed
3. Store encrypted L2 credentials
4. Link complete → ready to trade

### Deposit Funds

Send native USDC on Polygon to your Safe address (shown in Dashboard):
- Start with 20-50 USDC to test
- First deposit auto-deploys Safe Proxy

## III. Available Features

**Before binding**: /Index, /v2/go only

**After binding**: Dashboard, User Profile, Trades, Config, Referrals

## IV. Safety

- Daily max loss 50% (25% for small balances)
- Cumulative loss 40% auto-pauses
- Max bet 3% of balance
- Requires ensemble AI consensus
- You control Safe private key via Privy

## V. FAQ

**Can I use a manual wallet?** — No, only Privy + Safe derivation.

**Switch browsers?** — No rebind needed, same wallet address restores binding.

**Is embedded wallet safe?** — Yes, Privy uses MPC encryption. Funds are in Safe anyway.

**Why re-auth every visit?** — Prevents dead-loop states.

**Deploy Safe manually?** — No, one USDC sends auto-deploys.

## VI. Support

- System: `/v2`
- Tickets: Admin Console → User Tickets
- Critical: Super admin popups

---

Start trading by linking your wallet.

*Last updated: Admin-edited in console.*
"""

_DEFAULT_HELP_MARKDOWN_ZH = {
    "faq": "# 常见问题\n\n本页用于发布 VeYra 常见问题与操作说明。\n\n> 该内容可在管理后台 frontend/help 中按语言编辑。",
    "about": "# 关于 VeYra\n\nVeYra 专注于预测市场的实时数据、风险控制与执行体验。\n\n> 该内容可在管理后台 frontend/help 中按语言编辑。",
    "privacy": "# 隐私政策\n\n我们重视用户隐私与数据安全，相关条款会持续更新。\n\n> 该内容可在管理后台 frontend/help 中按语言编辑。",
    "terms": "# 服务条款\n\n使用 VeYra 服务即表示你同意当前服务条款与风险提示。\n\n> 该内容可在管理后台 frontend/help 中按语言编辑。",
}

_DEFAULT_HELP_MARKDOWN_EN = {
    "faq": "# FAQ\n\nThis page publishes VeYra frequently asked questions and usage notes.\n\n> Content can be edited per language in Admin frontend/help.",
    "about": "# About VeYra\n\nVeYra focuses on real-time prediction-market data, risk controls, and execution UX.\n\n> Content can be edited per language in Admin frontend/help.",
    "privacy": "# Privacy Policy\n\nWe value user privacy and data security; these terms may be updated over time.\n\n> Content can be edited per language in Admin frontend/help.",
    "terms": "# Terms of Service\n\nBy using VeYra services, you agree to the current terms and risk disclosures.\n\n> Content can be edited per language in Admin frontend/help.",
}


def _normalize_help_lang(lang: str | None) -> str:
    raw = str(lang or "").strip()
    if not raw:
        return "zh-CN"
    if raw == "zh-CN":
        return "zh-CN"
    if raw.lower().startswith("zh"):
        return "zh-CN"
    if _LANG_RE.match(raw):
        return raw
    return "en"


def _default_help_markdown(slug: str, lang: str) -> str:
    if slug == "go":
        return _DEFAULT_GO_MARKDOWN_ZH if lang == "zh-CN" else _DEFAULT_GO_MARKDOWN_EN
    if lang == "zh-CN":
        return _DEFAULT_HELP_MARKDOWN_ZH.get(slug, "")
    return _DEFAULT_HELP_MARKDOWN_EN.get(slug, "")


def _default_help_title(slug: str, lang: str) -> str:
    if slug == "go":
        return "V e Y r a 指导手册" if lang == "zh-CN" else "V e Y r a User Guide"
    title = str(slug or "").replace("_", " ").replace("-", " ").strip()
    if not title:
        return "Help"
    return " ".join(part[:1].upper() + part[1:] for part in title.split())


def _extract_title_from_markdown(markdown: str) -> str:
    text = str(markdown or "")
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:120]
    return ""


def _ensure_help_pages_table():
    """创建 system_help_pages 表（若不存在），并 seed 默认 /go 内容。"""
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_help_pages (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    title_i18n JSONB NOT NULL DEFAULT '{}'::jsonb,
                    markdown TEXT NOT NULL,
                    markdown_i18n JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_by TEXT
                )
                """)
            cur.execute("""
                ALTER TABLE system_help_pages
                ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT ''
                """)
            cur.execute("""
                ALTER TABLE system_help_pages
                ADD COLUMN IF NOT EXISTS title_i18n JSONB NOT NULL DEFAULT '{}'::jsonb
                """)
            cur.execute("""
                ALTER TABLE system_help_pages
                ADD COLUMN IF NOT EXISTS markdown_i18n JSONB NOT NULL DEFAULT '{}'::jsonb
                """)
            conn.commit()
        except Exception as exc:
            logger.warning("create system_help_pages table failed: %s", exc)
            conn.rollback()
            return
        # seed public slugs
        try:
            for slug in sorted(_PUBLIC_HELP_SLUGS):
                cur.execute(
                    "SELECT slug, title, title_i18n, markdown, markdown_i18n FROM system_help_pages WHERE slug=%s",
                    (slug,),
                )
                row = cur.fetchone()
                default_title_zh = _default_help_title(slug, "zh-CN")
                default_title_en = _default_help_title(slug, "en")
                default_md_zh = _default_help_markdown(slug, "zh-CN")
                default_md_en = _default_help_markdown(slug, "en")
                if not row:
                    cur.execute(
                        "INSERT INTO system_help_pages (slug, title, title_i18n, markdown, markdown_i18n, updated_at, updated_by) VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)",
                        (
                            slug,
                            default_title_zh,
                            json.dumps(
                                {"zh-CN": default_title_zh, "en": default_title_en},
                                ensure_ascii=False,
                            ),
                            default_md_zh,
                            json.dumps(
                                {"zh-CN": default_md_zh, "en": default_md_en},
                                ensure_ascii=False,
                            ),
                            datetime.now(timezone.utc),
                            "system-seed",
                        ),
                    )
                    continue

                data = dict(row)
                title = data.get("title") or default_title_zh
                title_i18n = data.get("title_i18n") or {}
                if isinstance(title_i18n, str):
                    try:
                        title_i18n = json.loads(title_i18n)
                    except Exception:
                        title_i18n = {}
                if not isinstance(title_i18n, dict):
                    title_i18n = {}

                markdown = data.get("markdown") or default_md_zh
                markdown_i18n = data.get("markdown_i18n") or {}
                if isinstance(markdown_i18n, str):
                    try:
                        markdown_i18n = json.loads(markdown_i18n)
                    except Exception:
                        markdown_i18n = {}
                if not isinstance(markdown_i18n, dict):
                    markdown_i18n = {}

                changed = False
                if not title_i18n.get("zh-CN"):
                    title_i18n["zh-CN"] = title
                    changed = True
                if not title_i18n.get("en"):
                    title_i18n["en"] = default_title_en
                    changed = True
                if not markdown_i18n.get("zh-CN"):
                    markdown_i18n["zh-CN"] = markdown
                    changed = True
                if not markdown_i18n.get("en"):
                    markdown_i18n["en"] = default_md_en
                    changed = True
                if changed:
                    cur.execute(
                        "UPDATE system_help_pages SET title_i18n=%s::jsonb, markdown_i18n=%s::jsonb WHERE slug=%s",
                        (
                            json.dumps(title_i18n, ensure_ascii=False),
                            json.dumps(markdown_i18n, ensure_ascii=False),
                            slug,
                        ),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("seed default /go markdown failed: %s", exc)
            conn.rollback()


@router.get("/api/help-pages/slugs")
async def get_help_page_slugs():
    _ensure_help_pages_table()
    try:

        def _db():
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT slug FROM system_help_pages ORDER BY slug ASC")
                return cur.fetchall() or []

        rows = await asyncio.to_thread(_db)
        db_slugs = []
        for r in rows:
            if isinstance(r, dict):
                s = str(r.get("slug") or "").strip()
            else:
                try:
                    s = str((r[0] if r else "") or "").strip()
                except Exception:
                    s = ""
            if s:
                db_slugs.append(s)
        merged = []
        for slug in list(_PUBLIC_HELP_SLUGS) + db_slugs:
            if slug and slug not in merged:
                merged.append(slug)
        return {"slugs": sorted(merged)}
    except Exception as exc:
        logger.exception("get_help_page_slugs failed: %s", exc)
        return JSONResponse({"error": f"db error: {exc}"}, status_code=500)


@router.get("/api/help-pages/{slug}")
async def get_help_page(slug: str, lang: str = "en", request: Request = None):
    """获取指定 slug 的 Markdown 内容。

    - go/faq/about/privacy/terms 游客可读。
    - 其它 slug 维持管理态可读策略。
    """
    if not _SLUG_RE.match(slug or ""):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    lang = _normalize_help_lang(lang)

    _ensure_help_pages_table()

    if slug not in _PUBLIC_HELP_SLUGS:
        identity = get_request_identity(request)
        if not identity:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not identity.get("is_admin"):
            return JSONResponse({"error": "forbidden"}, status_code=403)

    try:

        def _db():
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT slug, title, title_i18n, markdown, markdown_i18n, updated_at, updated_by FROM system_help_pages WHERE slug=%s",
                    (slug,),
                )
                return cur.fetchone()

        row = await asyncio.to_thread(_db)
    except Exception as exc:
        logger.exception("get_help_page %s failed: %s", slug, exc)
        return JSONResponse({"error": f"db error: {exc}"}, status_code=500)

    if not row:
        if slug in _PUBLIC_HELP_SLUGS:
            default_md = _default_help_markdown(slug, lang)
            return {
                "slug": slug,
                "title": _default_help_title(slug, lang),
                "markdown": default_md,
                "updated_at": None,
                "updated_by": None,
                "lang": lang,
            }
        return JSONResponse({"error": "not found"}, status_code=404)

    d = dict(row)
    title_i18n = d.get("title_i18n") or {}
    if isinstance(title_i18n, str):
        try:
            title_i18n = json.loads(title_i18n)
        except Exception:
            title_i18n = {}
    if not isinstance(title_i18n, dict):
        title_i18n = {}
    markdown_i18n = d.get("markdown_i18n") or {}
    if isinstance(markdown_i18n, str):
        try:
            markdown_i18n = json.loads(markdown_i18n)
        except Exception:
            markdown_i18n = {}
    if not isinstance(markdown_i18n, dict):
        markdown_i18n = {}
    markdown = markdown_i18n.get(lang)
    if not markdown and lang == "zh-CN":
        markdown = d.get("markdown") or _default_help_markdown(slug, lang)
    if not markdown:
        markdown = (
            markdown_i18n.get("en")
            or markdown_i18n.get("zh-CN")
            or d.get("markdown")
            or _default_help_markdown(slug, lang)
        )
    title = title_i18n.get(lang)
    if not title and lang == "zh-CN":
        title = d.get("title") or _default_help_title(slug, lang)
    if not title:
        title = (
            title_i18n.get("en")
            or title_i18n.get("zh-CN")
            or d.get("title")
            or _extract_title_from_markdown(markdown)
            or _default_help_title(slug, lang)
        )
    updated_at = d.get("updated_at")
    return {
        "slug": d.get("slug"),
        "title": title or _default_help_title(slug, lang),
        "markdown": markdown or "",
        "updated_at": (
            updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
        ),
        "updated_by": d.get("updated_by"),
        "lang": lang,
    }


@router.put("/api/help-pages/{slug}")
async def put_help_page(slug: str, lang: str = "zh-CN", request: Request = None):
    """更新指定 slug 的 Markdown 内容。需要 admin / super_admin 权限。"""
    if not _SLUG_RE.match(slug or ""):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    lang = _normalize_help_lang(lang)

    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    markdown = body.get("markdown")
    if not isinstance(markdown, str):
        return JSONResponse({"error": "markdown must be string"}, status_code=400)
    title_in = body.get("title")
    if title_in is not None and not isinstance(title_in, str):
        return JSONResponse({"error": "title must be string"}, status_code=400)
    if len(markdown) > 200_000:
        return JSONResponse({"error": "markdown too long (max 200KB)"}, status_code=413)

    title = (
        str(title_in or "").strip()
        or _extract_title_from_markdown(markdown)
        or _default_help_title(slug, lang)
    )[:120]

    _ensure_help_pages_table()

    updated_by = str(identity.get("wallet_address") or "")[:64]
    now = datetime.now(timezone.utc)

    try:

        def _db():
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                INSERT INTO system_help_pages (slug, title, title_i18n, markdown, markdown_i18n, updated_at, updated_by)
                VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title=CASE WHEN %s = 'zh-CN' THEN EXCLUDED.title ELSE system_help_pages.title END,
                    title_i18n=COALESCE(system_help_pages.title_i18n, '{}'::jsonb) || EXCLUDED.title_i18n,
                    markdown=CASE WHEN %s = 'zh-CN' THEN EXCLUDED.markdown ELSE system_help_pages.markdown END,
                    markdown_i18n=COALESCE(system_help_pages.markdown_i18n, '{}'::jsonb) || EXCLUDED.markdown_i18n,
                    updated_at=EXCLUDED.updated_at,
                    updated_by=EXCLUDED.updated_by
                """,
                    (
                        slug,
                        (
                            title
                            if lang == "zh-CN"
                            else _default_help_title(slug, "zh-CN")
                        ),
                        json.dumps({lang: title}, ensure_ascii=False),
                        (
                            markdown
                            if lang == "zh-CN"
                            else _default_help_markdown(slug, "zh-CN")
                        ),
                        json.dumps({lang: markdown}, ensure_ascii=False),
                        now,
                        updated_by,
                        lang,
                        lang,
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_db)
    except Exception as exc:
        logger.exception("put_help_page %s failed: %s", slug, exc)
        return JSONResponse({"error": f"db error: {exc}"}, status_code=500)

    logger.info(
        "help page %s updated by %s lang=%s (%d chars)",
        slug,
        updated_by,
        lang,
        len(markdown),
    )
    return {
        "status": "ok",
        "slug": slug,
        "lang": lang,
        "title": title,
        "length": len(markdown),
        "updated_at": now.isoformat(),
    }
