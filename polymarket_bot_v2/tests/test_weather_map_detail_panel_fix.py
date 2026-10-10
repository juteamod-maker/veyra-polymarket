import re


TEMPLATE_PATH = "scripts/frontend/templates/weather_event_map.html"


def _read_template():
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


class TestFmtBetTypeParamFix:
    def test_fmtBetType_param_not_named_t(self):
        html = _read_template()
        match = re.search(r"function _fmtBetType\((\w+)\)", html)
        assert match, "_fmtBetType function not found"
        param = match.group(1)
        assert param != "t", (
            f"_fmtBetType parameter is still named 't' — shadows global t() function: got '{param}'"
        )

    def test_fmtBetType_map_uses_renamed_param(self):
        html = _read_template()
        match = re.search(r"function _fmtBetType\((\w+)\)", html)
        assert match
        param = match.group(1)
        assert f"m[{param}]" in html, f"expected m[{param}] in body"
        assert "m[t]" not in html or "function _fmtBetType" not in html.split("m[t]")[0].split("function _fmtBetType")[-1], \
            "m[t] still exists inside _fmtBetType"


class TestBuildBetLinkFunction:
    def test_buildBetLink_present(self):
        html = _read_template()
        assert "function _buildBetLink" in html

    def test_buildBetLink_snapshot_gets_pm_url(self):
        html = _read_template()
        assert "function _buildBetLink" in html
        assert "user_pm_positions_snapshots" in html
        assert "polymarket.com" in html

    def test_buildBetLink_platform_bet_gets_internal_url(self):
        html = _read_template()
        assert "/event/" in html, "Internal /event/ URL should appear in _buildBetLink"
        assert "external:false" in html or "external: false" in html

    def test_renderSingleCard_uses_buildBetLink_not_pmUrl(self):
        html = _read_template()
        assert "_buildBetLink(b)" in html, "Card should use _buildBetLink(b)"
        card_section = html[html.find("function _renderSingleCard"):html.find("function _renderAll")]
        assert "_buildBetLink" in card_section, "Card section should use _buildBetLink"

    def test_buildBetLink_pm_has_external_true(self):
        html = _read_template()
        idx = html.find("function _buildBetLink")
        assert idx >= 0
        snippet = html[idx:idx+800]
        assert "external:true" in snippet or "external: true" in snippet

    def test_buildBetLink_ours_has_external_false(self):
        html = _read_template()
        idx = html.find("function _buildBetLink")
        assert idx >= 0
        snippet = html[idx:idx+1600]
        assert "external:false" in snippet or "external: false" in snippet


class TestToggleDetailSnapshotBranch:
    def test_toggleDetail_has_snapshot_source_check(self):
        html = _read_template()
        idx = html.find("function _toggleDetail")
        assert idx >= 0, "_toggleDetail not found"
        snippet = html[idx:idx+2000]
        assert "_isSnapshotBet" in snippet, "No snapshot source check helper usage in _toggleDetail"

    def test_toggleDetail_calls_renderSnapshot(self):
        html = _read_template()
        idx = html.find("function _toggleDetail")
        assert idx >= 0
        snippet = html[idx:idx+2000]
        assert "_renderSnapshotDetailPanel" in snippet

    def test_toggleDetail_checks_data_error_before_render(self):
        html = _read_template()
        idx = html.find("function _toggleDetail")
        assert idx >= 0
        snippet = html[idx:idx+3200]
        assert "data.error" in snippet, "No error check for API response in _toggleDetail"


class TestRenderSnapshotDetailPanel:
    def test_renderSnapshotDetailPanel_present(self):
        html = _read_template()
        assert "function _renderSnapshotDetailPanel" in html

    def test_renderSnapshotDetailPanel_shows_pm_link(self):
        html = _read_template()
        idx = html.find("function _renderSnapshotDetailPanel")
        assert idx >= 0
        snippet = html[idx:idx+6400]
        assert "polymarket.com" in snippet, "Snapshot panel should link to polymarket.com"
        assert "/event/" in snippet, "Snapshot panel should also expose VeYra event link"
        assert "weatherMap.detail.pmSnapshot" in snippet or "PM Snapshot" in snippet

    def test_renderSnapshotDetailPanel_shows_pnl(self):
        html = _read_template()
        idx = html.find("function _renderSnapshotDetailPanel")
        assert idx >= 0
        snippet = html[idx:idx+2000]
        assert "unrealized_pnl" in snippet, "Snapshot panel should display unrealized_pnl"

    def test_renderSnapshotDetailPanel_no_api_fetch(self):
        html = _read_template()
        idx = html.find("function _renderSnapshotDetailPanel")
        assert idx >= 0
        snippet = html[idx:idx+2000]
        assert "fetch(" not in snippet, "Snapshot panel should not make API calls"


class TestIntradayRefreshStateMachine:
    def test_refresh_intraday_increments_req_seq(self):
        html = _read_template()
        idx = html.find("window.refreshCurrentOverlayTab = function()")
        assert idx >= 0
        snippet = html[idx:idx+1200]
        assert "cityDetailReqSeq += 1" in snippet
        assert "loadCityDetail(cityName, cityDetailReqSeq)" in snippet

    def test_loadCityDetail_hides_loading_on_success(self):
        html = _read_template()
        idx = html.find("function loadCityDetail(cityName, reqSeq)")
        assert idx >= 0
        snippet = html[idx:idx+1500]
        assert "intraday-loading" in snippet
        assert "style.display = 'none'" in snippet

    def test_loadCityDetail_hides_loading_on_error(self):
        html = _read_template()
        idx = html.find("function loadCityDetail(cityName, reqSeq)")
        assert idx >= 0
        snippet = html[idx:idx+2000]
        assert ".catch(function(e)" in snippet
        assert "intraday-loading" in snippet


class TestDetailPriceChartRecovery:
    def test_loadDetailPriceChart_function_present(self):
        html = _read_template()
        assert "window.loadDetailPriceChart = function(betId, hours)" in html

    def test_loadDetailPriceChart_uses_price_history_api(self):
        html = _read_template()
        idx = html.find("window.loadDetailPriceChart = function(betId, hours)")
        assert idx >= 0
        snippet = html[idx:idx+4200]
        assert "/api/bets/" in snippet
        assert "/price-history?interval=" in snippet
        assert "/api/pm/market/trades" in snippet

    def test_price_chart_html_corruption_removed(self):
        html = _read_template()
        assert "weatherMap.lit.12f717bfdd" not in html
        assert "weatherMap.lit.c7ec9a7d1e" not in html

    def test_detail_chart_live_labels_update_in_poll(self):
        html = _read_template()
        idx = html.find("function _pollLive()")
        assert idx >= 0
        snippet = html[idx:idx+2400]
        assert "dp-pchart-cur-" in snippet
        assert "dp-pchart-chg-" in snippet


class TestLinkDiscriminationHardening:
    def test_buildBetLink_snapshot_detection_hardened(self):
        html = _read_template()
        idx = html.find("function _isSnapshotBet(b)")
        assert idx >= 0
        snippet = html[idx:idx+1300]
        assert "pmpos:" in snippet
        assert "manual_user_pm_snapshot" in snippet

    def test_buildBetLink_internal_fallback_exists(self):
        html = _read_template()
        idx = html.find("function _buildBetLink(b)")
        assert idx >= 0
        snippet = html[idx:idx+1300]
        assert "/v2/discover?p=weather" in snippet


class TestManualPmIntegration:
    def test_manual_pm_helpers_present(self):
        html = _read_template()
        assert "function _isManualPmBet" in html
        assert "function _extractConditionId" in html

    def test_loadCityBets_merges_user_pm_bets(self):
        html = _read_template()
        idx = html.find("function loadCityBets(cityName, options)")
        assert idx >= 0
        snippet = html[idx:idx+3600]
        assert "/api/user/pm/bets?status=open" in snippet
        assert "_betDedupKey" in snippet

    def test_toggleDetail_treats_manual_pm_like_snapshot(self):
        html = _read_template()
        idx = html.find("function _toggleDetail(betId, cardEl)")
        assert idx >= 0
        snippet = html[idx:idx+2400]
        assert "_isManualPmBet" in snippet
        assert "_renderSnapshotDetailPanel" in snippet

