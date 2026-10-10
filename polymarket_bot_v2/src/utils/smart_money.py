"""
smart_money.py — 聪明钱包追踪与信号模块
职责：
  1. 从 Polymarket Leaderboard 发现各赛道 Top PnL 钱包
  2. 查询指定市场的持仓大户（/holders API）
  3. 匹配聪明钱包 → 生成 smart_money_context 注入 AI prompt
  4. 钱包交易监控 → 巨鲸拆单检测（5 分钟记忆聚合）

API 端点（全部公开，无需认证）:
  - GET /positions?user=<wallet>       — 钱包当前活跃持仓
  - GET /closed-positions?user=<wallet> — 钱包已平仓持仓（含 realizedPnl）
  - GET /trades?user=<wallet>          — 钱包近期交易
  - GET /value?user=<wallet>           — 钱包总价值
  - GET /holders?market=<conditionId>  — 指定市场 top 持仓者（按 token 分）
  - Leaderboard: Next.js SSR 路由（需动态 buildId）
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
POLYMARKET_BASE = "https://polymarket.com"
_REQUEST_TIMEOUT = 15
_RATE_LIMIT_SLEEP = 0.5  # 两次 API 调用之间最小间隔（秒）

# ── 缓存路径 ─────────────────────────────────────────
_CACHE_DIR = Path(__file__).parent.parent.parent / "data"
_CACHE_FILE = _CACHE_DIR / "smart_money_cache.json"


class SmartMoneyTracker:
    """聪明钱包追踪器：发现、监控、信号聚合。"""

    def __init__(self) -> None:
        self._cache: dict = self._load_cache()
        self._last_request_ts: float = 0.0
        # 合并配置中的手动钱包 + leaderboard 自动发现钱包
        self._wallets: dict[str, list[dict]] = {}  # category → [{address, name, pnl}]
        self._init_wallets()

    # ══════════════════════════════════════════════════════════════════
    # 公开接口
    # ══════════════════════════════════════════════════════════════════

    def get_smart_context_for_market(self, market: dict) -> str:
        """
        给定 scanner 输出的 market dict，查询该市场的聪明钱包持仓信号。
        返回可直接注入 AI prompt 的文本块；如无信号则返回空字符串。

        核心逻辑：
        1. 用 /holders?market=<conditionId> 获取市场 top holders
        2. 匹配已知聪明钱包
        3. 聚合信号 → 格式化输出
        """
        if not get("smart_money.enabled"):
            return ""

        condition_id = market.get("conditionId", "")
        if not condition_id:
            return ""

        try:
            holders_data = self._fetch_holders(condition_id)
            if not holders_data:
                return ""

            # 构建已知聪明钱包地址集合（所有赛道合并）
            known_wallets = self._get_all_known_wallets()
            if not known_wallets:
                return ""

            # 在 holders 中匹配聪明钱包
            matches = []
            for token_group in holders_data:
                for holder in token_group.get("holders", []):
                    wallet_addr = holder.get("proxyWallet", "").lower()
                    if wallet_addr in known_wallets:
                        matches.append(
                            {
                                "name": known_wallets[wallet_addr]["name"],
                                "pnl": known_wallets[wallet_addr].get("pnl", 0),
                                "amount": holder.get("amount", 0),
                                "outcome_index": holder.get("outcomeIndex", 0),
                            }
                        )

            if not matches:
                return ""

            return self._format_smart_context(matches, market)

        except Exception as e:
            logger.warning(f"[SmartMoney] 获取市场聪明钱包信号失败: {e}")
            return ""

    def get_smart_money_alignment(self, market: dict, outcome: str) -> dict:
        """
        检查聪明钱包是否在指定市场持有与 outcome 同方向的仓位。

        Args:
            market: scanner 输出的 market dict（需含 conditionId）
            outcome: 投注方向 "YES" 或 "NO"

        Returns:
            {
                "has_smart_positions": bool,
                "aligned": bool,
                "aligned_count": int,
                "opposed_count": int,
                "total_aligned_amount": float,
            }
        """
        result = {
            "has_smart_positions": False,
            "aligned": False,
            "aligned_count": 0,
            "opposed_count": 0,
            "total_aligned_amount": 0.0,
        }

        if not get("smart_money.enabled"):
            return result

        condition_id = market.get("conditionId", "")
        if not condition_id:
            return result

        try:
            holders_data = self._fetch_holders(condition_id)
            if not holders_data:
                return result

            known_wallets = self._get_all_known_wallets()
            if not known_wallets:
                return result

            # outcome → outcomeIndex 映射: YES=0, NO=1
            target_idx = 0 if outcome.upper() == "YES" else 1

            for token_group in holders_data:
                for holder in token_group.get("holders", []):
                    wallet_addr = holder.get("proxyWallet", "").lower()
                    if wallet_addr not in known_wallets:
                        continue

                    result["has_smart_positions"] = True
                    holder_idx = holder.get("outcomeIndex", 0)
                    amount = float(holder.get("amount", 0))

                    if holder_idx == target_idx:
                        result["aligned_count"] += 1
                        result["total_aligned_amount"] += amount
                    else:
                        result["opposed_count"] += 1

            if result["aligned_count"] > result["opposed_count"]:
                result["aligned"] = True

            return result
        except Exception as e:
            logger.warning(f"[SmartMoney] alignment 检查失败: {e}")
            return result

    def get_market_holder_analysis(self, condition_id: str) -> dict:
        """
        分析市场全体持仓者分布（当聪明钱包不在此市场时的 fallback）。

        Returns:
            {
                "total_holders": int,
                "yes_holders": int,
                "no_holders": int,
                "yes_total_amount": float,
                "no_total_amount": float,
                "top5_concentration_pct": float,
                "consensus": str,  # "YES" / "NO" / "neutral"
            }
        """
        result = {
            "total_holders": 0,
            "yes_holders": 0,
            "no_holders": 0,
            "yes_total_amount": 0.0,
            "no_total_amount": 0.0,
            "top5_concentration_pct": 0.0,
            "consensus": "neutral",
        }

        if not condition_id:
            return result

        try:
            holders_data = self._fetch_holders(condition_id)
            if not holders_data:
                return result

            all_amounts: list[float] = []

            for token_group in holders_data:
                for holder in token_group.get("holders", []):
                    idx = holder.get("outcomeIndex", 0)
                    amount = float(holder.get("amount", 0))
                    all_amounts.append(amount)

                    if idx == 0:
                        result["yes_holders"] += 1
                        result["yes_total_amount"] += amount
                    else:
                        result["no_holders"] += 1
                        result["no_total_amount"] += amount

            result["total_holders"] = result["yes_holders"] + result["no_holders"]

            # 前5大户集中度
            if all_amounts:
                all_amounts.sort(reverse=True)
                total = sum(all_amounts)
                if total > 0:
                    top5 = sum(all_amounts[:5])
                    result["top5_concentration_pct"] = round(top5 / total, 4)

            # 共识判断（按金额）
            if result["yes_total_amount"] > result["no_total_amount"] * 1.5:
                result["consensus"] = "YES"
            elif result["no_total_amount"] > result["yes_total_amount"] * 1.5:
                result["consensus"] = "NO"

            return result
        except Exception as e:
            logger.warning(f"[SmartMoney] holder 分析失败: {e}")
            return result

    def refresh_leaderboard(self, force: bool = False) -> None:
        """
        刷新 leaderboard 钱包列表。
        默认缓存 refresh_interval_hours（6h），force=True 强制刷新。
        """
        if not get("smart_money.enabled"):
            return

        interval_hours = float(get("smart_money.refresh_interval_hours") or 6)
        last_refresh = self._cache.get("leaderboard_ts", 0)
        now = time.time()

        if not force and (now - last_refresh) < interval_hours * 3600:
            logger.debug("[SmartMoney] Leaderboard 缓存未过期，跳过刷新")
            return

        categories = get("smart_money.categories") or ["weather", "sports"]
        top_n = int(get("smart_money.top_n_wallets") or 5)
        min_pnl = float(get("smart_money.min_pnl_usd") or 5000)

        for category in categories:
            try:
                wallets = self._discover_top_wallets(category, top_n, min_pnl)
                if wallets:
                    self._cache.setdefault("leaderboard", {})[category] = wallets
                    logger.info(
                        f"[SmartMoney] {category} leaderboard: 发现 {len(wallets)} 个钱包"
                    )
            except Exception as e:
                logger.warning(f"[SmartMoney] 刷新 {category} leaderboard 失败: {e}")

        self._cache["leaderboard_ts"] = now
        self._save_cache()
        self._init_wallets()  # 重新合并手动+自动钱包

    def get_wallet_recent_trades(
        self, wallet_address: str, limit: int = 20
    ) -> list[dict]:
        """获取钱包近期交易记录（用于巨鲸拆单检测）。"""
        self._rate_limit()
        try:
            resp = requests.get(
                f"{DATA_API}/trades",
                params={"user": wallet_address, "limit": limit},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"[SmartMoney] 获取钱包交易失败: {e}")
            return []

    def detect_whale_accumulation(
        self,
        wallet_address: str,
        window_minutes: int = 5,
        threshold_usdc: float = 500,
    ) -> list[dict]:
        """
        巨鲸拆单检测：5 分钟记忆聚合算法。
        检测同一钱包在 window_minutes 内对同一市场的累计交易额超过 threshold_usdc。
        返回触发的市场列表。

        参考：参考学习资料.md 中的鲸鱼订单拆分检测策略。
        """
        trades = self.get_wallet_recent_trades(wallet_address, limit=50)
        if not trades:
            return []

        now_ts = int(time.time())
        window_seconds = window_minutes * 60

        # 按 (conditionId, side) 聚合
        aggregated: dict[str, dict] = {}
        for trade in trades:
            ts = trade.get("timestamp", 0)
            if now_ts - ts > window_seconds:
                continue

            key = f"{trade.get('conditionId', '')}_{trade.get('side', '')}"
            if key not in aggregated:
                aggregated[key] = {
                    "conditionId": trade.get("conditionId", ""),
                    "side": trade.get("side", ""),
                    "title": trade.get("title", ""),
                    "slug": trade.get("slug", ""),
                    "eventSlug": trade.get("eventSlug", ""),
                    "total_size": 0.0,
                    "total_usdc": 0.0,
                    "trade_count": 0,
                    "avg_price": 0.0,
                    "prices": [],
                }
            agg = aggregated[key]
            size = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
            agg["total_size"] += size
            agg["total_usdc"] += size * price
            agg["trade_count"] += 1
            agg["prices"].append(price)

        # 筛选超过阈值的
        triggered = []
        for agg in aggregated.values():
            if agg["total_usdc"] >= threshold_usdc:
                if agg["prices"]:
                    agg["avg_price"] = sum(agg["prices"]) / len(agg["prices"])
                del agg["prices"]
                triggered.append(agg)
                logger.info(
                    f"[SmartMoney] 🐋 巨鲸拆单检测: {agg['side']} "
                    f"${agg['total_usdc']:.0f} ({agg['trade_count']}笔) "
                    f"| {agg['title'][:50]}"
                )

        return triggered

    # ══════════════════════════════════════════════════════════════════
    # 内部方法
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _addr_display_name(address: str) -> str:
        """从钱包地址生成简短显示名: 0x751a...9ea1"""
        if len(address) >= 10:
            return address[:6] + "..." + address[-4:]
        return address

    def _init_wallets(self) -> None:
        """合并手动配置钱包 + leaderboard 自动发现钱包。"""
        self._wallets = {}
        # 手动配置的钱包 —— 新格式: 纯地址字符串数组
        manual = get("smart_money.manual_wallets") or []
        if isinstance(manual, list):
            # 新格式：["0xabc...", "0xdef..."] → 全赛道通用
            for item in manual:
                if isinstance(item, str) and item.startswith("0x"):
                    addr = item.lower()
                    self._wallets.setdefault("_global", [])
                    self._wallets["_global"].append(
                        {
                            "address": addr,
                            "name": self._addr_display_name(addr),
                            "pnl": 0,
                            "source": "manual",
                        }
                    )
        elif isinstance(manual, dict):
            # 兼容旧格式：{category: [{name, address, pnl}]}
            for category, wallet_list in manual.items():
                self._wallets.setdefault(category, [])
                for w in wallet_list:
                    if isinstance(w, str):
                        addr = w.lower()
                        self._wallets[category].append(
                            {
                                "address": addr,
                                "name": self._addr_display_name(addr),
                                "pnl": 0,
                                "source": "manual",
                            }
                        )
                    elif isinstance(w, dict):
                        self._wallets[category].append(
                            {
                                "address": w.get("address", "").lower(),
                                "name": w.get("name", ""),
                                "pnl": w.get("pnl", 0),
                                "source": "manual",
                            }
                        )

        # Leaderboard 自动发现的钱包
        lb_data = self._cache.get("leaderboard", {})
        for category, wallet_list in lb_data.items():
            self._wallets.setdefault(category, [])
            existing_addrs = {w["address"] for w in self._wallets[category]}
            for w in wallet_list:
                addr = w.get("address", "").lower()
                if addr not in existing_addrs:
                    self._wallets[category].append(
                        {
                            "address": addr,
                            "name": w.get("name", ""),
                            "pnl": w.get("pnl", 0),
                            "source": "leaderboard",
                        }
                    )
                    existing_addrs.add(addr)

        total = sum(len(v) for v in self._wallets.values())
        if total > 0:
            logger.debug(
                f"[SmartMoney] 钱包池: {total} 个钱包, 赛道: {list(self._wallets.keys())}"
            )

    def _get_all_known_wallets(self) -> dict[str, dict]:
        """返回所有已知聪明钱包的扁平化字典 {address_lower: {name, pnl, category}}。"""
        result = {}
        for category, wallet_list in self._wallets.items():
            for w in wallet_list:
                addr = w["address"].lower()
                if addr not in result:
                    result[addr] = {
                        "name": w["name"],
                        "pnl": w.get("pnl", 0),
                        "category": category,
                    }
        return result

    def _fetch_holders(self, condition_id: str) -> list[dict]:
        """
        GET /holders?market=<conditionId>
        返回格式: [{"token": "...", "holders": [{proxyWallet, amount, outcomeIndex, name, ...}]}]
        """
        self._rate_limit()
        try:
            resp = requests.get(
                f"{DATA_API}/holders",
                params={"market": condition_id},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return []
        except requests.RequestException as e:
            logger.warning(f"[SmartMoney] /holders 请求失败: {e}")
            return []

    def _discover_top_wallets(
        self, category: str, top_n: int = 5, min_pnl: float = 5000
    ) -> list[dict]:
        """
        从 Polymarket Leaderboard 发现指定赛道的 Top PnL 钱包。
        使用 Next.js SSR 数据路由（需动态 buildId）。
        """
        build_id = self._get_build_id()
        if not build_id:
            logger.warning("[SmartMoney] 无法获取 buildId，跳过 leaderboard 发现")
            return []

        self._rate_limit()
        url = f"{POLYMARKET_BASE}/_next/data/{build_id}/en/leaderboard/{category}.json"
        try:
            resp = requests.get(
                url,
                params={"slug": category, "period": "all"},
                timeout=_REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 404:
                # buildId 过期，强制刷新
                logger.info("[SmartMoney] buildId 过期，尝试刷新")
                self._cache.pop("build_id", None)
                self._cache.pop("build_id_ts", None)
                build_id = self._get_build_id(force=True)
                if not build_id:
                    return []
                url = f"{POLYMARKET_BASE}/_next/data/{build_id}/en/leaderboard/{category}.json"
                self._rate_limit()
                resp = requests.get(
                    url,
                    params={"slug": category, "period": "all"},
                    timeout=_REQUEST_TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"[SmartMoney] Leaderboard 请求失败: {e}")
            return []

        # 解析 Next.js pageProps 中的 dehydratedState
        wallets = []
        try:
            queries = (
                data.get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
            )
            # 找到 PnL 排名查询（Q1）
            # Q0 和 Q1 都含 pnl 字段，但 Q0 按 volume 排序、Q1 按 PnL 排序
            # 策略：收集所有含 pnl 的候选，选首条 pnl 最高的（即 PnL 排名）
            pnl_ranking = []
            pnl_candidates = []
            for q in queries:
                state_data = q.get("state", {}).get("data", [])
                if not state_data:
                    continue
                if (
                    isinstance(state_data, list)
                    and state_data
                    and "pnl" in state_data[0]
                ):
                    pnl_candidates.append(state_data)
            if pnl_candidates:
                pnl_ranking = max(
                    pnl_candidates,
                    key=lambda d: float(d[0].get("pnl", 0)),
                )

            for entry in pnl_ranking[:top_n]:
                pnl = float(entry.get("pnl", 0))
                if pnl < min_pnl:
                    continue
                wallets.append(
                    {
                        "address": entry.get("proxyWallet", "").lower(),
                        "name": entry.get("name", "") or entry.get("pseudonym", ""),
                        "pnl": pnl,
                        "volume": float(entry.get("volume", 0)),
                    }
                )
        except (KeyError, TypeError, IndexError) as e:
            logger.warning(f"[SmartMoney] 解析 leaderboard 数据失败: {e}")

        return wallets

    def _get_build_id(self, force: bool = False) -> str:
        """
        获取 Polymarket Next.js buildId。
        三级降级: 缓存(2h) → HTML提取(__NEXT_DATA__ JSON + 正则) → config兜底
        """
        # Level 1: 检查缓存（2h TTL，Polymarket 频繁部署，6h 太长导致 stale buildId）
        if not force:
            cached_id = self._cache.get("build_id", "")
            cached_ts = self._cache.get("build_id_ts", 0)
            if cached_id and (time.time() - cached_ts) < 7200:  # 2h
                return cached_id

        # Level 2: 从 leaderboard 页面 HTML 提取
        self._rate_limit()
        try:
            resp = requests.get(
                f"{POLYMARKET_BASE}/leaderboard",
                timeout=_REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()

            build_id = ""
            # 优先解析 <script id="__NEXT_DATA__"> JSON（更可靠）
            script_match = re.search(
                r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if script_match:
                try:
                    next_data = json.loads(script_match.group(1))
                    build_id = next_data.get("buildId", "")
                except json.JSONDecodeError:
                    pass

            # 回退到全文正则搜索
            if not build_id:
                m = re.search(r'"buildId"\s*:\s*"([^"]+)"', resp.text)
                if m:
                    build_id = m.group(1)

            if build_id:
                self._cache["build_id"] = build_id
                self._cache["build_id_ts"] = time.time()
                self._save_cache()
                logger.info(f"[SmartMoney] buildId 已更新: {build_id[:20]}...")
                return build_id

        except requests.RequestException as e:
            logger.warning(f"[SmartMoney] 获取 buildId 失败: {e}")

        # Level 3: 使用过期缓存
        stale = self._cache.get("build_id", "")
        if stale:
            logger.warning("[SmartMoney] 网络异常，降级使用过期缓存 buildId")
            return stale

        # Level 4: config.json 配置的兜底 buildId
        fallback = get("smart_money.fallback_build_id", "")
        if fallback:
            logger.warning(f"[SmartMoney] 使用 config 兜底 buildId: {fallback[:20]}...")
            return fallback

        return ""

    def _format_smart_context(self, matches: list[dict], market: dict) -> str:
        """将聪明钱包持仓匹配结果格式化为 AI prompt 文本。"""
        outcomes = ["Yes", "No"]
        try:
            raw_outcomes = market.get("outcomes", "")
            if isinstance(raw_outcomes, str):
                outcomes = json.loads(raw_outcomes)
        except (json.JSONDecodeError, TypeError):
            pass

        lines = ["⚡ 聪明钱包信号（Smart Money）:"]
        yes_count = 0
        no_count = 0
        total_amount = 0.0

        for m in matches:
            idx = m.get("outcome_index", 0)
            direction = outcomes[idx] if idx < len(outcomes) else f"index_{idx}"
            if idx == 0:
                yes_count += 1
            else:
                no_count += 1
            total_amount += m.get("amount", 0)

            pnl_str = f" (总PnL: ${m['pnl']:,.0f})" if m.get("pnl") else ""
            lines.append(
                f"  - {m['name']}{pnl_str}: 持有 {direction} {m['amount']:.0f} 股"
            )

        # 共识方向
        if yes_count > no_count:
            consensus = outcomes[0] if outcomes else "YES"
        elif no_count > yes_count:
            consensus = outcomes[1] if len(outcomes) > 1 else "NO"
        else:
            consensus = "分歧"

        lines.append(
            f"  共识: {consensus} (聪明钱包 {len(matches)} 个, 总持仓 {total_amount:.0f} 股)"
        )

        return "\n".join(lines)

    def _rate_limit(self) -> None:
        """简单速率限制：两次请求之间至少间隔 _RATE_LIMIT_SLEEP 秒。"""
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < _RATE_LIMIT_SLEEP:
            time.sleep(_RATE_LIMIT_SLEEP - elapsed)
        self._last_request_ts = time.time()

    def _load_cache(self) -> dict:
        """从磁盘加载缓存。"""
        try:
            if _CACHE_FILE.exists():
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SmartMoney] 加载缓存失败: {e}")
        return {}

    def _save_cache(self) -> None:
        """将缓存写入磁盘。"""
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"[SmartMoney] 保存缓存失败: {e}")
