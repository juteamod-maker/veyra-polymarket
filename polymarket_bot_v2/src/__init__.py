try:
    from src.scanner import MarketScanner
except ImportError:
    # T471 wave-A: V1 scanner 已归档到 _archive_v1/，V2 运行时不依赖它。
    # 保持可选导入，避免 `import src.*` 整体失败（T901 开源前修复）。
    MarketScanner = None  # type: ignore[assignment,misc]

try:
    from src.signal import Signal, SignalEngine
except ImportError:
    SignalEngine = None  # type: ignore[assignment,misc]
    Signal = None  # type: ignore[assignment,misc]
try:
    from src.executor import OrderExecutor, OrderResult
except ImportError:
    # T471 wave-A: V1 executor 依赖已归档的 src.signal，V2 运行时不再使用。
    OrderExecutor = None  # type: ignore[assignment,misc]
    OrderResult = None  # type: ignore[assignment,misc]

try:
    from src.monitor import RiskMonitor
except ImportError:
    RiskMonitor = None  # type: ignore[assignment,misc]

__all__ = [
    "MarketScanner",
    "SignalEngine",
    "Signal",
    "OrderExecutor",
    "OrderResult",
    "RiskMonitor",
]
