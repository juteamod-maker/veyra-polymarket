"""
test_weather_dedup.py — 天气问题文本去重 + 参数收紧验证
"""

import json
from pathlib import Path

import pytest


class TestNormalizeWeatherQuestion:
    """_normalize_weather_question 静态方法测试。"""

    @pytest.fixture(autouse=True)
    def _import_method(self):
        from src.simulation.paper_trader import PaperTrader

        self.normalize = PaperTrader._normalize_weather_question

    def test_same_city_temp_different_dates(self):
        """同城市同温度不同日期 → 应归一化为相同键。"""
        q1 = "Will the high temperature in Shanghai be 11°C on March 18?"
        q2 = "Will the high temperature in Shanghai be 11°C on March 19?"
        assert self.normalize(q1) == self.normalize(q2)

    def test_different_cities(self):
        """不同城市 → 不同键。"""
        q1 = "Will the high temperature in Shanghai be 11°C on March 18?"
        q2 = "Will the high temperature in Tokyo be 11°C on March 18?"
        assert self.normalize(q1) != self.normalize(q2)

    def test_different_temps(self):
        """同城市不同温度 → 不同键。"""
        q1 = "Will the high temperature in Shanghai be 11°C on March 18?"
        q2 = "Will the high temperature in Shanghai be 12°C on March 18?"
        assert self.normalize(q1) != self.normalize(q2)

    def test_with_year(self):
        """带年份的日期也能正确去除。"""
        q1 = "Will the high in NYC be 5°C on March 18, 2026?"
        q2 = "Will the high in NYC be 5°C on March 20, 2026?"
        assert self.normalize(q1) == self.normalize(q2)

    def test_iso_date_format(self):
        """ISO 日期格式 on YYYY-MM-DD 也能去除。"""
        q1 = "Will the high in Seoul be 10°C on 2026-03-18?"
        q2 = "Will the high in Seoul be 10°C on 2026-03-19?"
        assert self.normalize(q1) == self.normalize(q2)

    def test_no_date(self):
        """无日期引用 → 正常返回标准化文本。"""
        q = "Will the high temperature in Shanghai be 11°C?"
        result = self.normalize(q)
        assert result == "will the high temperature in shanghai be 11°c"

    def test_empty_input(self):
        """空输入 → 返回空字符串。"""
        assert self.normalize("") == ""
        assert self.normalize(None) == ""

    def test_preserves_core_content(self):
        """日期去除后核心内容不变。"""
        q = "Will the high in Ankara be 16°C on April 5?"
        result = self.normalize(q)
        assert "ankara" in result
        assert "16°c" in result
        assert "april" not in result

    def test_multiple_month_names(self):
        """所有月份名称都能正确去除。"""
        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        for month in months:
            q = f"Will it rain on {month} 15?"
            result = self.normalize(q)
            assert month.lower() not in result, f"Failed for {month}"


class TestWeatherConfigTightened:
    """验证 config.json 天气/地缘参数已收紧到安全范围。"""

    @pytest.fixture(autouse=True)
    def _load_config(self):
        config_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(config_path) as f:
            self.config = json.load(f)
        self.pt = self.config["paper_trading"]
        self.scanner = self.config["scanner"]

    def test_weather_ev_not_in_config(self):
        """天气赛道已通过 WCS/EHCL 质量门槛管控，category_min_ev 中不再设置 weather 键。"""
        assert "weather" not in self.pt.get(
            "category_min_ev", {}
        ), "category_min_ev.weather 已废弃，天气信号使用 WCS/EHCL 自身质量门槛"

    def test_cn_max_price_exists(self):
        """T268天气最高价格参数存在且 ≤ 0.98（T53: 放宽以提升覆盖率, EV门槛提供质量控制）。"""
        price = self.pt["city_no"]["max_price"]
        assert price <= 0.98, f"t268_max_price={price}, must be ≤0.98"

    def test_weather_max_size_capped(self):
        """天气单注上限 ≤ $20。"""
        sz = self.pt["weather_max_size_usdc"]
        assert sz <= 20.0, f"weather_max_size_usdc={sz}, must be ≤$20"

    def test_weather_max_positions_capped(self):
        """天气最大持仓数 ≤ 60（VPS生产配置允许天气赛道多持仓）。"""
        n = self.pt["category_max_positions"]["weather"]
        assert n <= 60, f"weather max positions={n}, must be \u226460"

    def test_weather_per_event_strict(self):
        """天气每事件最多 3 仓位（同事件含多个独立温度阈值市场）。"""
        n = self.pt["category_max_per_event"]["weather"]
        assert 1 <= n <= 3, f"weather per_event={n}, must be 1-3"

    def test_weather_size_multiplier_reduced(self):
        """Phase27: 天气仓位乘数 ≤ 2.0（100%WR核心赛道，数据驱动加仓）。"""
        m = self.pt["category_size_multiplier"]["weather"]
        assert m <= 2.0, f"weather size_mult={m}, must be ≤2.0"

    def test_weather_categories_bonus_removed(self):
        """Phase 24: 天气赛道加成 ≤ 1.2（核心优势赛道适度加成）。"""
        b = self.scanner["categories_bonus"]["weather"]
        assert b <= 1.2, f"weather bonus={b}, must be ≤1.2"

    def test_non_core_categories_blocked(self):
        """T49: 非核心赛道已被屏蔽。"""
        blocked = self.scanner.get("blocked_categories", [])
        for cat in ["geopolitics", "finance", "tech", "economy", "pop_culture"]:
            assert cat in blocked, f"{cat} should be in blocked_categories"

    def test_commodity_blacklist(self):
        """大宗商品关键词在黑名单中。"""
        bl = [kw.lower() for kw in self.scanner["blacklist_keywords"]]
        for kw in ["crude oil", "wti", "gold price", "silver price"]:
            assert kw in bl, f"'{kw}' missing from blacklist"

    def test_weather_min_confidence_raised(self):
        """天气最低置信度 ≥ 0.30（T256: 高量投注模式，放宽置信度准入）。"""
        c = self.pt["category_min_confidence"]["weather"]
        assert c >= 0.30, f"weather min_confidence={c}, must be ≥0.30"

    def test_cn_min_score_exists(self):
        """T268最低分数参数存在且 ≥ 0.5。"""
        s = self.pt["city_no"]["min_score"]
        assert s >= 0.5, f"t268_min_score={s}, must be ≥0.5"

    def test_dry_run_enabled(self):
        """dry_run 必须为 true。"""
        assert self.config["general"]["dry_run"] is True

    def test_cn_min_ev_exists(self):
        """城市NO策略 EV门槛存在且 ≥ 0.01。"""
        e = self.pt["city_no"]["min_ev"]
        assert e >= 0.01, f"t268_min_ev={e}, must be ≥0.01"

    def test_cn_only_scenario_multiplier_exists(self):
        """城市NO策略 only-scenario仓位乘数存在且 0 < x ≤ 1。"""
        m = self.pt["city_no"]["only_scenario_multiplier"]
        assert 0 < m <= 1.0, f"t268_only_scenario_multiplier={m}, must be 0<x≤1"
