"""
position_syncer.py — 链上持仓同步模块（v2 — Data API）
职责：从 Polymarket Data API 查询链上真实持仓，
      与本地 paper_bets 双向对账：导入新仓位 + 检测手动退出。

数据流：
  Data API /positions → 对比 paper_bets → 导入新仓位 / 标记手动退出

设计原则：
  - 使用 Data API 查询真实代币余额，而非交易历史聚合（杠绝幽灵仓位）
  - 同步的仓位标记 source='manual'，与自动投注区分
  - 双向对账：链上有&本地无 → 导入，链上无&本地有 → 标记手动退出
  - 幂等安全：INSERT OR IGNORE + condition_id 去重，重复执行不会产生重复记录
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

import requests
from src.utils import db
from src.utils.config_loader import get, is_dry_run

logger = logging.getLogger(__name__)
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


class PositionSyncer:
    """从 Data API 同步链上真实持仓到本地 paper_bets。"""

    def __init__(self, executor):
        """
        Args:
            executor: OrderExecutor 实例（保留接口兼容性）。
        """
        self._executor = executor

    def sync(self) -> list[dict]:
        """执行同步。

        Returns:
            本次新导入的 bet_record 列表（空 = 无新仓位或同步未启用）。
        """
        if not get("sync.enabled"):
            return []

        if is_dry_run():
            return []  # DRY RUN 模式无链上交易可同步

        try:
            return self._do_sync()
        except Exception as e:
            logger.warning(f"[PositionSyncer] 同步失败（不影响主流程）: {e}")
            return []

    # ------------------------------------------------------------------
    # 核心同步逻辑
    # ------------------------------------------------------------------

    def _do_sync(self) -> list[dict]:
        """基于 Data API 的双向对账同步。"""
        # 1. 从 Data API 获取链上真实持仓
        positions = self._fetch_real_positions()
        if positions is None:
            return []

        # 2. 构建链上 condition_id 集合（用于退出检测）
        chain_cids: set[str] = set()
        for pos in positions:
            cid = pos.get("conditionId", "")
            if cid:
                chain_cids.add(cid)

        # 3. 筛选需要导入的新仓位
        tracked_cids = db.get_tracked_condition_ids()
        min_shares = float(get("sync.min_position_shares") or 0.01)

        # 多维度去重：防止链上同一市场以不同 outcome 名称重复导入
        existing_bets = db.get_open_paper_bets()
        existing_qo: dict[tuple[str, str], str] = (
            {}
        )  # (question[:100], outcome) -> bet_id
        existing_slugs: dict[str, str] = {}  # market_slug -> bet_id
        existing_mids: dict[str, str] = {}  # market_id -> bet_id
        for bet in existing_bets:
            q = (bet.get("question") or "")[:100]
            o = (bet.get("outcome") or "").lower()
            if q:
                existing_qo[(q, o)] = bet["bet_id"]
            slug = bet.get("market_slug") or ""
            if slug:
                existing_slugs[slug] = bet["bet_id"]
            mid = str(bet.get("market_id") or "")
            if mid:
                existing_mids[mid] = bet["bet_id"]

        new_positions = []
        _now_utc = datetime.now(timezone.utc)
        for pos in positions:
            size = float(pos.get("size") or 0)
            if size < min_shares:
                continue

            # Phase 55 Fix: 跳过有效持仓量极小的 dust 仓位（脏数据/gas残留）
            # 链上会留存极小金额的代币（market resolved 后的 dust），不应被导入
            _initial_val = float(pos.get("initialValue") or 0)
            _min_usdc_threshold = float(get("sync.min_position_usdc") or 0.10)
            if _initial_val > 0 and _initial_val < _min_usdc_threshold:
                logger.debug(
                    f"[PositionSyncer] 跳过 dust 仓位（initialValue={_initial_val:.4f} "
                    f"< threshold={_min_usdc_threshold}）: {(pos.get('title') or '')[:40]}"
                )
                continue

            # Phase 55 Fix: 跳过已过期市场（end_date > 24h 前），防止 pre-reset 历史仓位被重新导入
            _end_date_str = pos.get("endDate") or pos.get("end_date") or ""
            if _end_date_str:
                try:
                    _end_dt = datetime.fromisoformat(
                        _end_date_str.replace("Z", "+00:00")
                    )
                    if _end_dt.tzinfo is None:
                        _end_dt = _end_dt.replace(tzinfo=timezone.utc)
                    _hours_past = (_now_utc - _end_dt).total_seconds() / 3600
                    if _hours_past > 24:
                        logger.debug(
                            f"[PositionSyncer] 跳过已过期市场（结束于 {_hours_past:.0f}小时前）: "
                            f"{(pos.get('title') or '')[:40]}"
                        )
                        continue
                except Exception:
                    pass  # 日期解析失败 → 保守导入

            cid = pos.get("conditionId", "")
            if cid and cid in tracked_cids:
                continue
            # market_slug 级别去重（同市场不同 outcome 名称也能匹配）
            pos_slug = pos.get("slug") or ""
            slug_matched_id = existing_slugs.get(pos_slug) if pos_slug else None
            if slug_matched_id:
                if cid:
                    db.update_condition_id(slug_matched_id, cid)
                    logger.info(
                        f"[PositionSyncer] slug去重+补填cid: "
                        f"{(pos.get('title') or '')[:40]}..."
                    )
                continue
            # T201: condition_id 作为 market_id 的去重（DB中市场ID可能存储了conditionId）
            if cid and cid in existing_mids:
                mid_matched_id = existing_mids[cid]
                logger.info(
                    f"[PositionSyncer] conditionId-as-mid去重: "
                    f"{(pos.get('title') or '')[:40]}..."
                )
                continue
            # Fallback: 检查 question+outcome 是否已有匹配记录
            title = (pos.get("title") or "")[:100]
            outcome = (pos.get("outcome") or "").lower()
            matched_bet_id = existing_qo.get((title, outcome))
            if matched_bet_id:
                # 旧记录已存在但缺 condition_id → 补填 condition_id
                if cid:
                    db.update_condition_id(matched_bet_id, cid)
                    logger.info(
                        f"[PositionSyncer] 补填 condition_id: {title[:40]}... → {cid[:16]}..."
                    )
                continue
            new_positions.append(pos)

        # 4. 导入新仓位
        imported = []
        for pos in new_positions:
            # Phase 54 Fix: 跳过已结算/可赎回的持仓（redeemable=True）
            # 这类持仓市场已关闭，导入为 chain_sync 会误进入活跌监控，
            # 且下一循环 _detect_manual_exits 会将其误判为手动退出
            if pos.get("redeemable"):
                title_short = (pos.get("title") or "")[:40]
                logger.debug(
                    f"[PositionSyncer] 跳过已结算创台持仓（redeemable）: {title_short}..."
                )
                continue
            bet_record = self._build_bet_from_data_api(pos, existing_bets)
            if bet_record is None:
                continue
            try:
                db.insert_paper_bet(bet_record)
                imported.append(bet_record)
                logger.info(
                    f"[PositionSyncer] 导入链上仓位: {bet_record['question'][:50]}... "
                    f"| {bet_record['outcome']} @ {bet_record['market_price']:.4f} "
                    f"| {bet_record['size_usdc']:.2f} USDC"
                )
            except Exception as e:
                cid = pos.get("conditionId", "?")
                logger.warning(f"[PositionSyncer] 导入失败 {cid[:16]}...: {e}")

        if imported:
            logger.info(f"[PositionSyncer] 本次同步导入 {len(imported)} 个链上仓位")

        # 5. 校准已有 auto 来源仓位的 size_usdc 和 market_price
        self._reconcile_auto_positions(positions)

        # 6. 检测手动退出的仓位
        # Phase 54 Fix: 仅将活跃（非 redeemable）持仓的 CID 传入
        # 已结算的 redeemable 持仓从副名单消失是正常结算流程，不认为是手动退出
        # T100 Fix: 过滤 dust 仓位，链上残留的极微量代币不应阻止手动退出检测。
        # T170 Fix: 使用独立的 dust_threshold_shares 而非 min_order_shares(5.0)。
        # 历史 Bug（T170）：使用 min_order_shares=5.0 作为过滤阈值导致真实小仓位
        # （如 partial fill 1 share）被错误标记为 manual_exit，而实际应由 P0-5 路径处理。
        # 正确设计：dust_threshold_shares（默认0.1）仅过滤 gas 残留，不影响真实仓位。
        _dust_threshold = float(get("sync.dust_threshold_shares") or 0.1)
        active_cids: set[str] = {
            pos.get("conditionId", "")
            for pos in positions
            if not pos.get("redeemable")
            and pos.get("conditionId")
            and float(pos.get("size") or 0) >= _dust_threshold
        }

        # T176 Fix: 防护 Data API 返回空数据导致批量误判手动退出（SUPREME 1）
        # 仅当多个 auto/manual OPEN 仓位存在但 API 返回 0 时才触发保护
        _open_bets = db.get_open_paper_bets()
        _open_auto = [
            b
            for b in _open_bets
            if (b.get("source") or "auto") in ("auto", "manual")
            and b.get("condition_id")
        ]
        if len(_open_auto) >= 3 and len(active_cids) == 0 and len(positions) == 0:
            logger.warning(
                f"[PositionSyncer] ⚠️ Data API 返回 0 个链上仓位，"
                f"但 DB 有 {len(_open_auto)} 个 auto/manual OPEN 仓位 — "
                f"跳过手动退出检测（SUPREME 1 防护）"
            )
        else:
            self._detect_manual_exits(active_cids)

        return imported

    # ------------------------------------------------------------------
    # Data API 查询
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_real_positions() -> list[dict] | None:
        """从 Data API 获取链上真实持仓。

        使用 POLY_RELAYER_ADDRESS（代理/funder 钱包）查询，
        因为 ERC1155 代币由代理钱包持有，而非签名者钱包。

        Returns:
            持仓列表，或 None（API 错误时）。
        """
        funder = os.environ.get("POLY_RELAYER_ADDRESS", "")
        if not funder:
            logger.error(
                "[PositionSyncer] POLY_RELAYER_ADDRESS 环境变量未设置，无法查询链上持仓"
            )
            return None
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": funder.lower(), "sizeThreshold": "0"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                logger.warning(f"[PositionSyncer] Data API 返回非列表: {type(data)}")
                return None
            # 区分活跃 vs 可赎回（已结算）持仓，避免用户误以为数据不一致
            active = [p for p in data if not p.get("redeemable")]
            redeemable = len(data) - len(active)
            if redeemable:
                logger.debug(
                    f"[PositionSyncer] Data API 返回 {len(data)} 个链上持仓"
                    f"（{len(active)} 活跃 + {redeemable} 已结算待赎回）"
                )
            else:
                logger.debug(f"[PositionSyncer] Data API 返回 {len(data)} 个链上持仓")
            return data
        except requests.RequestException as e:
            logger.warning(f"[PositionSyncer] Data API 查询失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 构建 paper_bet 记录（从 Data API 响应）
    # ------------------------------------------------------------------

    def _build_bet_from_data_api(
        self, pos: dict, existing_bets: list[dict] | None = None
    ) -> dict | None:
        """从 Data API 持仓数据构建 paper_bets 记录。

        Data API 响应已包含丰富元数据（title, slug, outcome, avgPrice 等），
        无需额外调用 CLOB get_market() 或 Gamma API。

        Args:
            pos: Data API 单条持仓数据。

        Returns:
            paper_bets dict 或 None（数据不完整时）。
        """
        title = pos.get("title", "")
        condition_id = pos.get("conditionId", "")
        if not title or not condition_id:
            logger.debug(f"[PositionSyncer] 跳过不完整持仓: title={title!r}")
            return None

        slug = pos.get("slug", "")
        event_slug = pos.get("eventSlug", "")
        outcome = pos.get("outcome", "")
        avg_price = float(pos.get("avgPrice") or 0)
        size = float(pos.get("size") or 0)
        initial_value = float(pos.get("initialValue") or 0)
        end_date = pos.get("endDate", "")

        # size_usdc = 初始投入成本
        size_usdc = initial_value if initial_value > 0 else round(size * avg_price, 4)

        # 赛道推断（Data API 无 tags，用标题关键词）
        category = self._infer_category_from_tags([], title)

        # Gamma numeric market ID（best-effort，失败则用 condition_id）
        gamma_market_id, resolved_event_slug = self._resolve_gamma_info(slug)
        if resolved_event_slug and not event_slug:
            event_slug = resolved_event_slug
        market_id = gamma_market_id or condition_id

        # category="unknown" 时尝试从同市场已有记录继承
        if category == "unknown" and existing_bets:
            for eb in existing_bets:
                eb_cat = eb.get("category") or "unknown"
                if eb_cat == "unknown":
                    continue
                eb_slug = eb.get("market_slug") or ""
                eb_mid = str(eb.get("market_id") or "")
                if (slug and eb_slug == slug) or (market_id and eb_mid == market_id):
                    category = eb_cat
                    logger.info(
                        f"[PositionSyncer] 继承赛道: {title[:40]}... → {category}"
                    )
                    break

        # AI 概率：YES→avgPrice，NO→1-avgPrice，命名结果→avgPrice
        outcome_upper = outcome.upper() if outcome else ""
        if outcome_upper == "NO":
            ai_prob = 1 - avg_price
        else:
            ai_prob = avg_price

        # T201: 尝试从同市场已有记录继承元数据（strategy_tag, bet_bucket, source 等）
        # 场景：bot 下单后 position_syncer 因去重失败创建了重复记录，
        # 至少应继承原始记录的策略标签和来源信息
        inherited_strategy_tag = ""
        inherited_bet_bucket = "manual"
        inherited_source = "manual"
        inherited_ai_models = "manual"
        inherited_ai_reasoning = "链上持仓同步（Data API）"
        if existing_bets:
            for eb in existing_bets:
                eb_slug = eb.get("market_slug") or ""
                eb_mid = str(eb.get("market_id") or "")
                eb_cid = eb.get("condition_id") or ""
                eb_outcome = (eb.get("outcome") or "").upper()
                # 匹配条件：同 slug、同 market_id 或同 condition_id，且同 outcome
                slug_match = slug and eb_slug == slug
                mid_match = market_id and eb_mid == market_id
                cid_match = condition_id and eb_cid == condition_id
                if (
                    slug_match or mid_match or cid_match
                ) and eb_outcome == outcome_upper:
                    inherited_strategy_tag = eb.get("strategy_tag") or ""
                    inherited_bet_bucket = eb.get("bet_bucket") or "manual"
                    inherited_source = eb.get("source") or "manual"
                    inherited_ai_models = eb.get("ai_models") or "manual"
                    orig_reasoning = eb.get("ai_reasoning") or ""
                    if orig_reasoning and orig_reasoning != "链上持仓同步（Data API）":
                        inherited_ai_reasoning = orig_reasoning
                    logger.info(
                        f"[PositionSyncer] 继承元数据: {title[:40]}... "
                        f"← {eb['bet_id'][:8]} | tag={inherited_strategy_tag} "
                        f"source={inherited_source}"
                    )
                    break

        # T201: 天气赛道 strategy_tag 推断（若未从已有记录继承）
        if not inherited_strategy_tag and category == "weather":
            inherited_strategy_tag = self._infer_weather_strategy_tag(
                title, outcome_upper, avg_price
            )
            if inherited_strategy_tag:
                inherited_bet_bucket = inherited_strategy_tag

        return {
            "bet_id": str(uuid.uuid4()),
            "ts_entered": time.time(),
            "market_id": market_id,
            "question": title[:500],
            "category": category,
            "outcome": outcome,
            "confidence": 0.5,  # 同步仓位无 AI 置信度
            "ai_probability": ai_prob,
            "market_price": avg_price,
            "ev": 0.0,  # 同步仓位无 EV 计算
            "size_usdc": size_usdc,
            "matched_shares": round(size, 6),  # Data API size = 持仓份数
            "sim_balance_before": 0.0,
            "sim_balance_after": 0.0,
            "market_end_date": end_date,
            "weather_context": "",
            "ai_models": inherited_ai_models,
            "ai_reasoning": inherited_ai_reasoning,
            "cycle_id": 0,
            "bet_bucket": inherited_bet_bucket,
            "market_slug": slug,
            "event_slug": event_slug,
            "bet_type": "entry",
            "dimension_tags": "chain_sync",
            "condition_id": condition_id,
            "source": inherited_source,
            "strategy_tag": inherited_strategy_tag,
        }

    # ------------------------------------------------------------------
    # 手动退出检测
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_manual_exits(chain_cids: set[str]) -> list[str]:
        """检测本地有而链上已无的仓位，标记为手动退出。

        Args:
            chain_cids: 链上当前所有《活跃》持仓的 condition_id 集合（不含 redeemable）。

        Returns:
            被标记为手动退出的 bet_id 列表。
        """
        now_utc = datetime.now(timezone.utc)
        open_bets = db.get_open_paper_bets()
        exited = []

        for bet in open_bets:
            cid = bet.get("condition_id", "")
            if not cid:
                continue  # 老记录无 condition_id，跳过

            source = bet.get("source") or "auto"

            if source in ("auto", "copy_trade"):
                # T96 Fix: DRY_RUN 模式下 auto/copy_trade 仓位是纸质的，链上不存在，无法对账 → 跳过
                # 实盘模式下 auto/copy_trade 仓位在用户平台手动清仓或市场结算后，
                # 链上代币消失，必须在此处检测并关闭 DB 记录，避免幽灵仓位（SUPREME 1）
                # T201追加: copy_trade 来源的实盘仓位同样需要链上对账，
                # 否则市场结算后 DB 残留大量幽灵仓位
                if is_dry_run():
                    continue
            elif source != "manual":
                continue  # 其他非标准来源跳过

            if cid in chain_cids:
                continue  # 链上仍持有，正常

            # Phase 54 Fix: 如果市场已过期，仓位消失可能是市场结算（代币已可赎回/已赎回）
            # 跳过手动退出判断，由 outcome_tracker 的正常结算流程处理
            end_date_str = bet.get("market_end_date", "")
            if end_date_str:
                try:
                    # 支持多种格式："2026-03-21" 或 ISO timestamp
                    from datetime import date as _date

                    if len(end_date_str) == 10:  # "YYYY-MM-DD"
                        end_dt = datetime(
                            *map(int, end_date_str.split("-")), tzinfo=timezone.utc
                        )
                    else:
                        import email.utils as _eu

                        end_dt = datetime.fromisoformat(
                            end_date_str.replace("Z", "+00:00")
                        )
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt < now_utc:
                        logger.debug(
                            f"[PositionSyncer] 市场已过期，跳过手动退出判断（由结算系统处理）: "
                            f"{bet.get('question','?')[:40]}... end={end_date_str}"
                        )
                        continue
                except Exception:
                    pass  # 日期解析失败时保守遍历，继续检测

            # T96 Fix: source=auto/copy_trade 的实盘仓位需要 min_age 保护
            # 防止刚下单（尚未链上确认）的仓位被误判为手动退出
            if source in ("auto", "copy_trade"):
                import time as _time

                _min_age = float(get("reconcile.min_age_seconds") or 300)
                _ts_entered = float(bet.get("ts_entered") or 0)
                if _time.time() - _ts_entered < _min_age:
                    logger.debug(
                        f"[PositionSyncer] auto仓位未满{_min_age:.0f}s，跳过退出检测: "
                        f"{bet.get('question','?')[:40]}"
                    )
                    continue

            # 市场仍活跃但链上无持仓 → 手动退出（网站卖出/市场结算后赎回）
            bet_id = bet["bet_id"]
            exit_price, pnl, payout = PositionSyncer._estimate_exit_pnl(bet)

            db.close_paper_bet_manual_exit(
                bet_id=bet_id,
                exit_price=exit_price,
                pnl=pnl,
                payout=payout,
            )
            exited.append(bet_id)
            logger.info(
                f"[PositionSyncer] 标记手动退出({source}): {bet.get('question', '?')[:50]}..."
                f" | bet_id={bet_id[:8]}... | pnl={pnl:+.2f}"
            )

        if exited:
            logger.info(f"[PositionSyncer] 本次标记 {len(exited)} 个手动退出仓位")
        return exited

    @staticmethod
    def _estimate_exit_pnl(bet: dict) -> tuple[float, float, float]:
        """根据 Gamma API 当前市场价估算手动退出的 exit_price / pnl / payout。

        Returns:
            (exit_price, pnl, payout)
        """
        import json as _json

        size_usdc = float(bet.get("size_usdc") or 0)
        matched_shares = float(bet.get("matched_shares") or 0)
        entry_price = float(bet.get("market_price") or 0)
        outcome = bet.get("outcome", "YES")
        market_id = bet.get("market_id", "")

        if not market_id or matched_shares <= 0:
            return entry_price, 0.0, 0.0

        try:
            resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=5)
            if resp.status_code != 200:
                return entry_price, 0.0, 0.0

            market = resp.json()

            # 1) 市场已结算 → 根据最终结果精确计算
            if market.get("resolved"):
                prices_raw = market.get("outcomePrices", "[]")
                prices = (
                    _json.loads(prices_raw)
                    if isinstance(prices_raw, str)
                    else prices_raw
                )
                yes_final = float(prices[0]) if prices else 0
                if outcome == "YES":
                    payout = matched_shares * yes_final
                    exit_price = yes_final
                else:
                    payout = matched_shares * (1 - yes_final)
                    exit_price = 1 - yes_final
                pnl = round(payout - size_usdc, 4)
                # T189-G: 扣除买入手续费
                _ps_entry_fee = float(bet.get("entry_fee_usdc") or 0)
                pnl = round(payout - size_usdc - _ps_entry_fee, 4)
                return exit_price, pnl, round(payout, 4)

            # 2) 市场仍活跃 → 用当前价格估算卖出价
            prices_raw = market.get("outcomePrices", "[]")
            prices = (
                _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            )
            yes_price = float(prices[0]) if prices else 0

            if outcome == "YES":
                exit_price = yes_price
                payout = matched_shares * yes_price
            else:
                exit_price = 1 - yes_price
                payout = matched_shares * (1 - yes_price)
            pnl = round(payout - size_usdc, 4)
            # T189-G: 扣除买入手续费
            _ps_entry_fee2 = float(bet.get("entry_fee_usdc") or 0)
            pnl = round(payout - size_usdc - _ps_entry_fee2, 4)
            return exit_price, pnl, round(payout, 4)

        except Exception as e:
            logger.warning(f"[PositionSyncer] 获取市场价格失败: {e}")
            # Phase 54 Fix: API 失败时用年均价 × shares 估算，而非静默返回 pnl=0
            # 防止 Gamma API 瞬时故障导致所有手动退出的 PnL 被错误记录为 $0
            if matched_shares > 0 and entry_price > 0:
                est_payout = round(matched_shares * entry_price, 4)
                est_pnl = round(est_payout - size_usdc, 4)
                # T189-G: 扣除买入手续费
                _ps_entry_fee3 = float(bet.get("entry_fee_usdc") or 0)
                est_pnl = round(est_payout - size_usdc - _ps_entry_fee3, 4)
                logger.warning(
                    f"[PositionSyncer] API失败用年均价估算: "
                    f"shares={matched_shares} × entry={entry_price} = "
                    f"payout={est_payout} pnl={est_pnl:+.4f}"
                )
                return entry_price, est_pnl, est_payout
            return entry_price, 0.0, 0.0

    # ------------------------------------------------------------------
    # 链上对账：校准 auto 来源仓位的 size_usdc 和 market_price
    # ------------------------------------------------------------------

    @staticmethod
    def _reconcile_auto_positions(chain_positions: list[dict]) -> int:
        """用链上 initialValue/avgPrice 校准 auto 来源仓位。

        Args:
            chain_positions: Data API 返回的链上持仓列表。

        Returns:
            校准的仓位数量。
        """
        # T192-FIX: 使用 (conditionId, outcome) 复合键，防止同一 condition 的
        # YES/NO 持仓数据互相覆盖。之前仅用 conditionId 做 key，当用户同时持有
        # 同一市场的 YES 和 NO 仓位时（如 copy_trade YES + weather NO），
        # 后出现的持仓会覆盖前者，导致错误的数据被写入另一个方向的仓位。
        chain_by_key: dict[tuple[str, str], dict] = {}
        for pos in chain_positions:
            cid = pos.get("conditionId", "")
            outcome = (pos.get("outcome") or "").upper()
            if cid and float(pos.get("size") or 0) > 0:
                chain_by_key[(cid, outcome)] = pos

        open_bets = db.get_open_paper_bets()
        corrected = 0
        min_age = get("reconcile.min_age_seconds") or 300
        now_ts = time.time()

        for bet in open_bets:
            source = bet.get("source") or "auto"
            dimension_tags = bet.get("dimension_tags") or ""
            # chain_sync 来源若数据为零（首次同步时链上还未结算），允许对账修正
            is_chain_sync_zero = (
                source == "manual"
                and "chain_sync" in dimension_tags
                and float(bet.get("size_usdc") or 0) == 0
            )
            # T170 Fix: 扩展对账范围至 copy_trade 来源，修正 pre-T169 bug 产生的
            # 错误 size_usdc（策略预算）和 matched_shares（部分成交后未更新）。
            # copy_trade 仓位的链上 initialValue/size 是唯一权威来源。
            if source not in ("auto", "copy_trade") and not is_chain_sync_zero:
                continue

            # Phase34 Fix: 跳过刚下单的仓位，等待链上完全结算
            ts_entered = float(bet.get("ts_entered") or 0)
            if now_ts - ts_entered < min_age:
                continue

            cid = bet.get("condition_id", "")
            bet_outcome = (bet.get("outcome") or "").upper()
            key = (cid, bet_outcome)
            if not cid or key not in chain_by_key:
                continue

            chain = chain_by_key[key]
            chain_cost = float(chain.get("initialValue") or 0)
            chain_avg = float(chain.get("avgPrice") or 0)
            chain_shares = float(chain.get("size") or 0)
            db_cost = float(bet.get("size_usdc") or 0)
            db_price = float(bet.get("market_price") or 0)
            db_shares = float(bet.get("matched_shares") or 0)

            cost_diff = abs(db_cost - chain_cost)
            price_diff = abs(db_price - chain_avg)
            shares_diff = abs(db_shares - chain_shares)

            if cost_diff < 0.01 and price_diff < 0.005 and shares_diff < 0.01:
                continue

            bet_id = bet["bet_id"]

            # Phase 57: 检测手动加仓（链上份额增长）→ 创建 scale_in 子记录
            if chain_shares > db_shares + 0.1 and chain_cost > db_cost + 0.05:
                inc_shares = round(chain_shares - db_shares, 6)
                inc_cost = round(chain_cost - db_cost, 4)
                inc_price = (
                    round(inc_cost / inc_shares, 6) if inc_shares > 0 else chain_avg
                )
                child_id = str(uuid.uuid4())
                try:
                    with db.connect() as conn:
                        conn.execute(
                            "INSERT INTO paper_bets "
                            "(bet_id, ts_entered, market_id, question, category, outcome, "
                            "confidence, ai_probability, market_price, ev, size_usdc, "
                            "matched_shares, sim_balance_before, sim_balance_after, "
                            "market_end_date, weather_context, ai_models, ai_reasoning, "
                            "cycle_id, bet_bucket, market_slug, event_slug, bet_type, "
                            "dimension_tags, condition_id, source, parent_bet_id, "
                            "strategy_tag) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                child_id,
                                time.time(),
                                bet.get("market_id", ""),
                                bet.get("question", "")[:500],
                                bet.get("category", "unknown"),
                                bet.get("outcome", ""),
                                0.5,
                                bet.get("ai_probability", 0),
                                inc_price,
                                0.0,
                                inc_cost,
                                inc_shares,
                                0.0,
                                0.0,
                                bet.get("market_end_date", ""),
                                "",
                                "manual",
                                "链上检测到手动加仓（PositionSyncer对账）",
                                0,
                                bet.get("bet_bucket")
                                or bet.get("strategy_tag")
                                or "manual",
                                bet.get("market_slug", ""),
                                bet.get("event_slug", ""),
                                "scale_in",
                                "chain_sync",
                                bet.get("condition_id", ""),
                                "manual",
                                bet_id,
                                bet.get("strategy_tag") or "",
                            ),
                        )
                        conn.execute(
                            "UPDATE paper_bets SET scale_in_count = COALESCE(scale_in_count, 0) + 1, "
                            "last_scale_at = ? WHERE bet_id = ?",
                            (datetime.now(timezone.utc).isoformat(), bet_id),
                        )
                    logger.info(
                        f"[PositionSyncer] 检测手动加仓: {bet.get('question', '?')[:40]}... | "
                        f"+{inc_shares:.4f} shares +${inc_cost:.2f} @ {inc_price:.4f}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[PositionSyncer] 创建手动加仓记录失败 {bet_id[:8]}: {e}"
                    )

            try:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE paper_bets SET size_usdc=?, market_price=?, matched_shares=? "
                        "WHERE bet_id=? AND resolved_outcome IS NULL",
                        (
                            round(chain_cost, 4),
                            round(chain_avg, 6),
                            round(chain_shares, 6),
                            bet_id,
                        ),
                    )
                corrected += 1
                # T189-F: 校准时补算买入手续费（如果尚未记录）
                _existing_fee = float(bet.get("entry_fee_usdc") or 0)
                if _existing_fee == 0 and chain_shares > 0 and chain_avg > 0:
                    _cat = bet.get("category", "weather")
                    _ot = bet.get("order_type", "taker")
                    _entry_fee, _fr = db.calc_pm_fee(chain_shares, chain_avg, _cat, _ot)
                    if _entry_fee > 0:
                        db.update_bet_fees(
                            bet_id, entry_fee_usdc=_entry_fee, fee_rate=_fr
                        )
                logger.info(
                    f"[PositionSyncer] 校准仓位: {bet.get('question', '?')[:40]}... | "
                    f"size: ${db_cost:.4f}→${chain_cost:.4f} | "
                    f"price: {db_price:.4f}→{chain_avg:.4f} | "
                    f"shares: {db_shares:.4f}→{chain_shares:.4f}"
                )
            except Exception as e:
                logger.warning(f"[PositionSyncer] 校准失败 {bet_id[:8]}: {e}")

        if corrected:
            logger.info(
                f"[PositionSyncer] 本次校准 {corrected} 个仓位（auto+chain_sync）"
            )
        return corrected

    # ------------------------------------------------------------------
    # Gamma API 辅助（解析 numeric market ID）
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_gamma_id(market_slug: str) -> str:
        """通过 slug 查找 Gamma API numeric market ID。"""
        gamma_id, _ = PositionSyncer._resolve_gamma_info(market_slug)
        return gamma_id

    @staticmethod
    def _resolve_gamma_info(market_slug: str) -> tuple[str, str]:
        """通过 slug 查找 Gamma API numeric market ID 和 event_slug。

        Returns:
            (gamma_market_id, event_slug) 元组
        """
        if not market_slug:
            return "", ""
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"slug": market_slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) == 1:
                item = data[0]
                gamma_id = str(item.get("id", ""))
                event_slug = str(item.get("groupItemTitle", "") or "")
                if not event_slug:
                    event_slug = _infer_event_slug(market_slug)
                return gamma_id, event_slug
        except Exception as e:
            logger.debug(f"[PositionSyncer] Gamma slug 查询失败: {e}")
        return "", ""

    # ------------------------------------------------------------------
    # 天气策略标签推断
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_weather_strategy_tag(
        question: str, outcome_upper: str, avg_price: float = 0.0
    ) -> str:
        """根据天气市场问题文本、持仓方向和价格推断 strategy_tag。

        T201: chain_sync 仓位无法确定精确的左翼/右翼（需要气象模型数据），
        但可以根据 outcome 方向和价格做基础推断：
        - NO → weather_right_tail_same（默认右尾做空策略）
        - YES + 高价(≥0.70) → weather_ehcl（EHCL尾部锁定，买入高置信度尾桶）
        - YES + 低价(<0.70) → weather_yes_ambush（埋伏买入）

        T204-fix: 原来 YES 一律标为 weather_yes_ambush，导致
        PositionSyncer 导入的 EHCL 仓位被错误标记。

        Returns:
            strategy_tag 字符串，空串表示无法推断。
        """
        q_lower = question.lower()
        if "temperature" not in q_lower:
            return ""
        if outcome_upper == "NO":
            return "weather_right_tail_same"
        elif outcome_upper == "YES":
            # T204: EHCL 买 YES 时入场价很高(≥0.70)，YES Ambush 买入价很低
            ehcl_min = 0.85  # V2: hardcoded, no longer reads from V1 weather_strategy config
            if avg_price >= ehcl_min:
                return "weather_ehcl"
            return "weather_yes_ambush"
        return ""

    # ------------------------------------------------------------------
    # 赛道推断
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_category_from_tags(tags: list[str], question: str) -> str:
        """从 tags 和 question 推断赛道分类。"""
        tags_lower = [t.lower() for t in tags]
        question_lower = question.lower()

        if "weather" in tags_lower or "temperature" in question_lower:
            return "weather"
        if any(t in tags_lower for t in ["sports", "nba", "nfl", "mlb", "nhl"]):
            return "sports"
        if any(t in tags_lower for t in ["crypto", "bitcoin", "ethereum"]):
            return "crypto"
        if any(t in tags_lower for t in ["politics", "geopolitics"]):
            return "geopolitics"
        if any(t in tags_lower for t in ["finance", "economics", "stocks"]):
            return "finance"

        # 关键词回退
        sport_kw = [
            "vs.",
            "game",
            "match",
            "playoff",
            "championship",
            "win",
            "spread:",
            "over/under",
            "moneyline",
            "total points",
        ]
        if any(kw in question_lower for kw in sport_kw):
            return "sports"
        crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "crypto"]
        if any(kw in question_lower for kw in crypto_kw):
            return "crypto"
        recession_kw = ["recession", "gdp", "inflation", "interest rate", "fed"]
        if any(kw in question_lower for kw in recession_kw):
            return "finance"

        return "unknown"


def _infer_event_slug(market_slug: str) -> str:
    """从 market_slug 推断 event_slug。

    Polymarket URL 格式: /event/{event_slug}/{market_slug}
    例如: market_slug = "highest-temperature-in-shanghai-on-march-21-2026-16c"
          event_slug  = "highest-temperature-in-shanghai-on-march-21-2026"

    策略：尝试去掉末尾的选项后缀（数字/温度/队名等）
    """
    if not market_slug:
        return ""
    # 尝试通过 Gamma events API 查找
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": market_slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) == 1:
            return str(data[0].get("slug", ""))
    except Exception:
        pass

    # 回退：用 market_slug 搜索匹配的 event
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={
                "slug_contains": (
                    market_slug.rsplit("-", 1)[0] if "-" in market_slug else market_slug
                )
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) == 1:
            return str(data[0].get("slug", ""))
    except Exception:
        pass

    return ""
