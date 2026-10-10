"""
dashboard_routes/backtest_routes.py — T240 回测系统 API 路由

端点:
    GET /api/backtest/run      - 运行回测（fast 模式，<1s）
    GET /api/backtest/deep     - 运行深度回测（含 Polymarket API 止损分类）
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

router = APIRouter()
logger = logging.getLogger("web_dashboard")


@router.get("/api/backtest/run")
async def api_backtest_run(
    mode: str = Query("days", description="hours|days|range"),
    value: int = Query(7, ge=1, le=365),
    start: str = Query("", description="start date YYYY-MM-DD (range mode)"),
    end: str = Query("", description="end date YYYY-MM-DD (range mode)"),
    track: str = Query("", description="category filter: weather|sports|crypto|..."),
):
    """运行快速回测（纯 DB 查询，<1s）。"""
    from src.backtest.backtest_engine import run_backtest

    try:
        result = run_backtest(
            mode=mode,
            value=value,
            start=start,
            end=end,
            track=track,
            deep=False,
        )
        return result
    except Exception as e:
        logger.error(f"[Backtest] 快速回测失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/backtest/deep")
async def api_backtest_deep(
    mode: str = Query("days", description="hours|days|range"),
    value: int = Query(7, ge=1, le=365),
    start: str = Query("", description="start date YYYY-MM-DD"),
    end: str = Query("", description="end date YYYY-MM-DD"),
    track: str = Query("", description="category filter: weather|sports|crypto|..."),
):
    """运行深度回测（含 Polymarket API 止损正确/错误分类，可能较慢）。"""
    from src.backtest.backtest_engine import run_backtest

    try:
        result = run_backtest(
            mode=mode,
            value=value,
            start=start,
            end=end,
            track=track,
            deep=True,
        )
        return result
    except Exception as e:
        logger.error(f"[Backtest] 深度回测失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/backtest/export")
async def api_backtest_export(
    mode: str = Query("days", description="hours|days|range"),
    value: int = Query(7, ge=1, le=365),
    start: str = Query("", description="start date YYYY-MM-DD"),
    end: str = Query("", description="end date YYYY-MM-DD"),
    track: str = Query("", description="category filter"),
):
    """导出回测结果为 JSON 文件。"""
    import json

    from src.backtest.backtest_engine import run_backtest

    try:
        result = run_backtest(
            mode=mode,
            value=value,
            start=start,
            end=end,
            track=track,
            deep=False,
        )
        body = json.dumps(result, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": "attachment; filename=backtest_result.json"
            },
        )
    except Exception as e:
        logger.error(f"[Backtest] 导出失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
