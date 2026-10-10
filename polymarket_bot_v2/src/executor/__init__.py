try:
    from .order_executor import OrderExecutor, OrderResult
except ImportError:
    # T471 wave-A: order_executor 依赖已归档的 V1 src.signal，V2 运行时不再使用它。
    # 保持可选导入，避免 `import src.executor.<V2 子模块>` 整体失败（T901 修复）。
    OrderExecutor = None  # type: ignore[assignment,misc]
    OrderResult = None  # type: ignore[assignment,misc]

__all__ = ["OrderExecutor", "OrderResult"]
