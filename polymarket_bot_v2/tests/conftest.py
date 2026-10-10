"""
全局 pytest fixtures。

确保测试隔离：外部 API 环境变量不会因 load_dotenv 副作用泄漏到纯单元测试中。
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_relayer_env(monkeypatch):
    """移除 POLY_RELAYER_ADDRESS，防止 Data API 在单元测试中被真实调用。"""
    monkeypatch.delenv("POLY_RELAYER_ADDRESS", raising=False)
    monkeypatch.delenv("POLY_RELAYER_ADDRESS", raising=False)


@pytest.fixture(autouse=True)
def _mock_orderbook_min_order_size():
    """T36: 全局 mock get_min_order_size，防止测试发起真实 Orderbook API 调用。

    默认返回 min_order_size=5.0（与旧 executor.min_order_shares=5 一致）。
    需要自定义值的测试可在本地 patch 覆盖。
    需要测试真实函数的测试可将 fixture return 值用于重设。
    """
    with patch(
        "src.utils.orderbook_history.get_min_order_size", return_value=5.0
    ) as _mock:
        yield _mock
