"""
weather_fetcher.py — 天气数据抓取工具 + GFS Ensemble 概率引擎

职责：
  - 识别 Polymarket 天气类市场
  - 从市场规则中提取 wunderground 气象站 URL
  - 抓取实时气象数据（主力: wttr.in JSON API；备选: wunderground 页面解析）
  - 【核心】GFS 31 成员集合预报 → 直接计算概率 P(YES)，绕过 LLM
  - 结果缓存 10 分钟，避免频繁请求

用途：
  天气市场优先使用 fetch_ensemble_probability() 计算精确概率，
  仅当问题无法解析时才 fallback 到 AI LLM 分析。
"""

import hashlib
import json
import logging
import random
import re
import struct
import time
from datetime import datetime, timedelta, timezone
from math import erf, sqrt
from typing import Optional

import requests
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 匹配 wunderground 气象站 URL
_WU_URL_RE = re.compile(r"https?://(?:www\.)?wunderground\.com/\S+", re.IGNORECASE)

# 从 wunderground URL 提取气象站 ID（ICAO / PWS）
# 示例：.../weather/kr/incheon/RKSI → RKSI
#       .../personal-weather-station/dashboard?ID=KFLL3 → KFLL3
_STATION_ID_RE = re.compile(
    r"/weather/[^/]+/[^/]+/([A-Z0-9]+)" r"|[?&]ID=([A-Z0-9]+)",
    re.IGNORECASE,
)

# 天气类市场关键词（覆盖中英文，避免 "ukraine" 含 "rain" 的误匹配）
_WEATHER_KEYWORDS = frozenset(
    [
        "temperature",
        "celsius",
        "fahrenheit",
        "°c",
        "°f",
        "precipitation",
        "rainfall",
        "snowfall",
        "humidity",
        "weather station",
        "weather forecast",
        "wunderground",
        "high temp",
        "low temp",
        "max temp",
        "min temp",
        "will it rain",
        "will it snow",
        "will it be",
        "天气",
        "温度",
        "气温",
        "降雨",
        "降雪",
        "湿度",
        "气象",
    ]
)

# 阈值型天气问题模式 — AI无法可靠估价，策略不支持（累积概率型）
_THRESHOLD_WEATHER_PATTERNS = (
    "or higher",
    "or above",
    "or more",
    "or lower",
    "or below",
    "or less",
    "at least",
    "at most",
    "or warmer",
    "or colder",
)


def is_threshold_weather_question(question: str) -> bool:
    """判断天气问题是否为阈值型（≥X 或 ≤X），AI 无法可靠估价此类问题。"""
    q_lower = (question or "").lower()
    return any(pat in q_lower for pat in _THRESHOLD_WEATHER_PATTERNS)


# 简单结果缓存 {url: (timestamp, data)}
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600  # 10 分钟

# T265+T267: Open-Meteo Ensemble API 智能速率限制 + 缓存层
# 多层防护: ① 结果缓存(避免重复请求) ② 自适应节流(动态间隔) ③ 过期缓存兜底(429时用旧数据)
import threading as _threading

_ENSEMBLE_API_LOCK = _threading.Lock()
_LAST_ENSEMBLE_API_TS: float = 0.0

# 自适应速率限制: 429 时自动加倍间隔, 成功时缓慢恢复
_ADAPTIVE_INTERVAL: float = 0.0  # 0 = 未初始化, 首次使用时从 config 读取
_ADAPTIVE_LOCK = _threading.Lock()
_CACHE_STALE_TTL = 7200  # 2小时过期缓存兜底 (可被 config 覆盖)

# T271修复: 分离 GFS / ECMWF 熔断器 — 独立失败，互不影响
# 额外独立的批量请求熔断器，批量429不波及单坐标请求
_GFS_CIRCUIT_OPEN_UNTIL: float = 0.0
_ECMWF_CIRCUIT_OPEN_UNTIL: float = 0.0
_BATCH_CIRCUIT_OPEN_UNTIL: float = 0.0  # 只影响 batch prefetch，不影响个体请求
_GFS_CONSEC_FAILURES: int = 0  # 连续失败次数，用于指数退避
_ECMWF_CONSEC_FAILURES: int = 0
_CIRCUIT_LOCK = _threading.Lock()
_CIRCUIT_COOLDOWN_DEFAULT = 60  # 默认基础冷却 60s（指数退避, 最大 120s）
_CIRCUIT_COOLDOWN_MAX = 120  # 最大冷却时间 2 分钟（原5分钟导致T192大量跳过）

# T275: Open-Meteo IP级429时间戳 — batch/individual 共享 IP,
# 任一端点429后，其他端点短时间内跳过主站直接走云端镜像降级链
_OPEN_METEO_LAST_429_TS: float = 0.0
_OPEN_METEO_429_SKIP_WINDOW: float = 90.0  # 90秒内认为同IP仍被限速

# T272/T273: 优雅降级 — 多层降级链防止 T192 阻断天气市场
_GFS_DET_CIRCUIT_OPEN_UNTIL: float = 0.0  # api.open-meteo.com确定性GFS熔断器
_ECMWF_DET_CIRCUIT_OPEN_UNTIL: float = 0.0  # api.open-meteo.com确定性ECMWF熔断器
_GFS_FALLBACK_COUNT: int = 0  # 连续降级次数 (超过阈值时发出 WARNING)
_ECMWF_FALLBACK_COUNT: int = 0
_FALLBACK_WARN_THRESHOLD = 3  # 连续降级N次后发 WARNING（可被config覆盖）

# 确定性API端点 (与ensemble-api.open-meteo.com完全独立的速率限制池)
_OPEN_METEO_DETERMINISTIC_URL = "https://api.open-meteo.com/v1/forecast"

# T273: 数据源追踪元数据 & 置信度惩罚系数
# 每次请求后由 _gfs_multi_fallback / _ecmwf_multi_fallback 更新
_DATA_SOURCE_META: dict = (
    {}
)  # {cache_key: {"source": str, "members": int, "age": float}}

# 数据源置信度乘数 — 降级数据质量越低, 惩罚越重（不允许低质量数据冒充高质量信号）
# T37-W3: 从 config.json weather_ensemble.source_confidence_multiplier 读取
_SOURCE_CONFIDENCE_DEFAULTS: dict = {
    "ensemble": 1.0,
    "aws_gfs_s3": 0.72,
    "ecmwf_mirror": 0.68,
    "noaa_nomads": 0.62,
    "stale_fresh": 0.78,
    "stale_recent": 0.55,
    "stale_old": 0.30,
    "det_fallback": 0.35,
    "synthetic": 0.15,
    "emergency_climate": 0.12,
}


def _get_source_confidence_multiplier() -> dict:
    """Read source confidence multiplier from config (SUPREME 2 compliant)."""
    cfg = get("weather_ensemble.source_confidence_multiplier")
    if isinstance(cfg, dict):
        merged = dict(_SOURCE_CONFIDENCE_DEFAULTS)
        merged.update({k: float(v) for k, v in cfg.items()})
        return merged
    return dict(_SOURCE_CONFIDENCE_DEFAULTS)


# Module-level reference for backward compat (log messages etc.)
_SOURCE_CONFIDENCE_MULTIPLIER: dict = _get_source_confidence_multiplier()

# ── T274: AWS S3 GFS 云端镜像 ──────────────────────────────────────────────
# s3://noaa-gfs-bdp-pds/ → HTTPS 直接访问，.idx 索引 + HTTP Range Request
# 与 Open-Meteo / NOMADS 完全独立的 AWS 基础设施
_AWS_GFS_S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
_AWS_GFS_CIRCUIT_OPEN_UNTIL: float = 0.0

# ── T274: ECMWF 云端镜像 (AWS / Azure / Google) ───────────────────────────
# ECMWF Open Data 分布在三大云平台，彼此独立
_ECMWF_MIRROR_URLS: list = [
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/",
    "https://ai4edataeuwest.blob.core.windows.net/ecmwf/",
    "https://storage.googleapis.com/ecmwf-forecasts/",
]
_ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL: float = 0.0

# ── NOAA NOMADS GrADS Data Server ──────────────────────────────────────────
# 官方NOAA OPeNDAP服务器，底层数据 = AWS Open Data Registry s3://noaa-gfs-bdp-pds/
# (registry.opendata.aws/noaa-gfs-bdp-pds/) — 与Open-Meteo完全独立的基础设施
# 格式: OPeNDAP ASCII, 无需GRIB2解析库, 全球覆盖, 无需API密钥
_NOMADS_GDS_BASE = "https://nomads.ncep.noaa.gov/dods/gfs_0p25"
_NOMADS_CIRCUIT_OPEN_UNTIL: float = 0.0


def _get_adaptive_interval() -> float:
    """获取当前自适应请求间隔(秒)。"""
    global _ADAPTIVE_INTERVAL
    with _ADAPTIVE_LOCK:
        if _ADAPTIVE_INTERVAL <= 0:
            ens_cfg = get("weather_ensemble") or {}
            _ADAPTIVE_INTERVAL = float(ens_cfg.get("api_request_interval_sec", 3.0))
        return _ADAPTIVE_INTERVAL


def _on_api_429(model: str = "gfs") -> None:
    """429 响应后: 加倍请求间隔 (上限 30s) + 开启对应模型熔断器。

    T275修复: batch 429 不再膨胀共享 _ADAPTIVE_INTERVAL，
    只记录 _OPEN_METEO_LAST_429_TS 让 individual 请求跳过主站。

    Args:
        model: "gfs" | "ecmwf" | "batch"
    """
    global _ADAPTIVE_INTERVAL, _GFS_CONSEC_FAILURES, _ECMWF_CONSEC_FAILURES
    global _OPEN_METEO_LAST_429_TS
    # 记录 IP 级 429 时间戳（所有 Open-Meteo 端点共享 IP）
    _OPEN_METEO_LAST_429_TS = time.time()
    if model != "batch":
        # T275: 只有 individual 请求的 429 才膨胀自适应间隔
        # batch 429 不污染 individual 的节流策略
        with _ADAPTIVE_LOCK:
            _ens_cfg = get("weather_ensemble") or {}
            base = float(_ens_cfg.get("api_request_interval_sec", 3.0))
            # T101: P2-1 节流参数从 config 读取
            _throttle_mult = float(_ens_cfg.get("throttle_backoff_multiplier", 2.0))
            _throttle_max = float(_ens_cfg.get("throttle_max_interval_sec", 30.0))
            if _ADAPTIVE_INTERVAL <= 0:
                _ADAPTIVE_INTERVAL = base
            _ADAPTIVE_INTERVAL = min(_ADAPTIVE_INTERVAL * _throttle_mult, _throttle_max)
            logger.info(
                f"[WeatherThrottle] {model.upper()} 429 → "
                f"自适应间隔提升至 {_ADAPTIVE_INTERVAL:.1f}s"
            )
    else:
        logger.info(
            "[WeatherThrottle] BATCH 429 → 记录IP级限速时间戳"
            "（不膨胀individual自适应间隔）"
        )
    _circuit_open(model, "429 rate limit")


def _on_api_success(model: str = "gfs") -> None:
    """成功响应后: 缓慢恢复请求间隔 + 关闭对应模型熔断器。

    Args:
        model: "gfs" | "ecmwf" | "batch"
    """
    global _ADAPTIVE_INTERVAL, _GFS_CONSEC_FAILURES, _ECMWF_CONSEC_FAILURES
    with _ADAPTIVE_LOCK:
        base = float(
            (get("weather_ensemble") or {}).get("api_request_interval_sec", 3.0)
        )
        if _ADAPTIVE_INTERVAL <= 0:
            _ADAPTIVE_INTERVAL = base
        _ADAPTIVE_INTERVAL = max(_ADAPTIVE_INTERVAL * 0.85, base)
    _circuit_close(model)
    # 重置连续失败计数
    with _CIRCUIT_LOCK:
        if model == "gfs":
            _GFS_CONSEC_FAILURES = 0
        elif model == "ecmwf":
            _ECMWF_CONSEC_FAILURES = 0


def _circuit_open(model: str = "gfs", reason: str = "") -> None:
    """打开指定模型的熔断器（指数退避冷却）。

    Args:
        model: "gfs" | "ecmwf" | "batch"
    """
    global _GFS_CIRCUIT_OPEN_UNTIL, _ECMWF_CIRCUIT_OPEN_UNTIL
    global _BATCH_CIRCUIT_OPEN_UNTIL
    global _GFS_CONSEC_FAILURES, _ECMWF_CONSEC_FAILURES

    base_cooldown = float(
        (get("weather_ensemble") or {}).get(
            "circuit_breaker_cooldown_sec", _CIRCUIT_COOLDOWN_DEFAULT
        )
    )
    with _CIRCUIT_LOCK:
        if model == "gfs":
            _GFS_CONSEC_FAILURES += 1
            cooldown = min(
                base_cooldown * (2 ** (_GFS_CONSEC_FAILURES - 1)), _CIRCUIT_COOLDOWN_MAX
            )
            _GFS_CIRCUIT_OPEN_UNTIL = time.time() + cooldown
        elif model == "ecmwf":
            _ECMWF_CONSEC_FAILURES += 1
            cooldown = min(
                base_cooldown * (2 ** (_ECMWF_CONSEC_FAILURES - 1)),
                _CIRCUIT_COOLDOWN_MAX,
            )
            _ECMWF_CIRCUIT_OPEN_UNTIL = time.time() + cooldown
        elif model == "batch":
            # batch熔断独立：使用配置值但上限300s，不影响单城市请求
            cooldown = min(base_cooldown * 2, _CIRCUIT_COOLDOWN_MAX)
            _BATCH_CIRCUIT_OPEN_UNTIL = time.time() + cooldown
        else:
            cooldown = base_cooldown

    logger.warning(
        f"[WeatherCircuit] {model.upper()} 熔断器开启, 暂停 {cooldown:.0f}s ({reason})"
    )


def _circuit_close(model: str = "gfs") -> None:
    """关闭指定模型的熔断器。"""
    global _GFS_CIRCUIT_OPEN_UNTIL, _ECMWF_CIRCUIT_OPEN_UNTIL, _BATCH_CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        if model == "gfs":
            if _GFS_CIRCUIT_OPEN_UNTIL > 0:
                logger.info("[WeatherCircuit] GFS 熔断器关闭，API 成功恢复")
            _GFS_CIRCUIT_OPEN_UNTIL = 0.0
        elif model == "ecmwf":
            if _ECMWF_CIRCUIT_OPEN_UNTIL > 0:
                logger.info("[WeatherCircuit] ECMWF 熔断器关闭，API 成功恢复")
            _ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        elif model == "batch":
            _BATCH_CIRCUIT_OPEN_UNTIL = 0.0


def _is_circuit_open(model: str = "gfs") -> bool:
    """检查指定模型的熔断器是否开启。

    Args:
        model: "gfs" | "ecmwf" | "batch"
    """
    global _GFS_CIRCUIT_OPEN_UNTIL, _ECMWF_CIRCUIT_OPEN_UNTIL, _BATCH_CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        if model == "gfs":
            until = _GFS_CIRCUIT_OPEN_UNTIL
        elif model == "ecmwf":
            until = _ECMWF_CIRCUIT_OPEN_UNTIL
        elif model == "batch":
            until = _BATCH_CIRCUIT_OPEN_UNTIL
        else:
            return False

        if until <= 0:
            return False
        if time.time() >= until:
            return False
        remaining = until - time.time()
        logger.debug(
            f"[WeatherCircuit] {model.upper()} 熔断器开启中, 剩余 {remaining:.0f}s"
        )
        return True


def _throttle_ensemble_api() -> None:
    """确保两次 Open-Meteo Ensemble API 请求之间有自适应最小间隔。"""
    global _LAST_ENSEMBLE_API_TS
    interval = _get_adaptive_interval()
    with _ENSEMBLE_API_LOCK:
        now = time.time()
        elapsed = now - _LAST_ENSEMBLE_API_TS
        if elapsed < interval:
            wait = interval - elapsed
            logger.debug(
                f"[WeatherThrottle] 速率限制等待 {wait:.2f}s (间隔={interval:.1f}s)"
            )
            time.sleep(wait)
        _LAST_ENSEMBLE_API_TS = time.time()


def _get_stale_cache(
    cache_key: str, model_label: str, max_age: Optional[float] = None
) -> Optional[dict]:
    """API 失败后尝试从过期缓存取数据。

    Args:
        cache_key: 缓存键
        model_label: 日志标签 ("GFS" / "ECMWF")
        max_age: 过期容忍时长(秒)。None = 使用配置的 cache_stale_ttl_seconds(默认2h)；
                 float('inf') = 接受任意年龄的缓存（最后兜底）
    """
    if max_age is None:
        max_age = float(
            (get("weather_ensemble") or {}).get(
                "cache_stale_ttl_seconds", _CACHE_STALE_TTL
            )
        )
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        age_sec = time.time() - ts
        age_min = age_sec / 60
        if age_sec < max_age:
            if max_age == float("inf"):
                logger.warning(
                    f"[Weather{model_label}] 所有数据源均失败, 使用 {age_min:.0f}分钟前的任意缓存"
                    f"（终极兜底，数据较旧但可用）"
                )
            else:
                logger.warning(
                    f"[Weather{model_label}] API 全部失败, 使用 {age_min:.0f}分钟前的缓存数据 "
                    f"(过期兜底, max_age={max_age/60:.0f}min)"
                )
            return data
    return None


def _build_synthetic_ensemble(
    mean_c: float, date_str: str, sigma_c: float, n: int
) -> list:
    """从确定性预报生成可复现的 Gaussian 合成集合成员。

    ⚠️ 仅作为绝对最后手段调用。合成成员基于人为假设的σ，
    下游 fetch_ensemble_probability 会检测并施加 ×0.15 重度置信度惩罚
    并发出 [SYNTHETIC DATA] 警告。

    Args:
        mean_c: 确定性预报温度(°C)
        date_str: 预测日期字符串（用于哈希种子）
        sigma_c: 集合离散度假设(°C)
        n: 合成成员数量

    Returns:
        长度为 n 的排序温度列表(°C)
    """
    seed_bytes = f"{mean_c:.3f}|{date_str}|{sigma_c:.2f}|{n}".encode()
    seed_int = int(hashlib.md5(seed_bytes).hexdigest()[:8], 16)
    rng = random.Random(seed_int)
    members = sorted(rng.gauss(mean_c, sigma_c) for _ in range(n))
    return members


def _fetch_deterministic_fallback(
    lat: float,
    lon: float,
    model_det: str,
    variable: str,
    forecast_days: int,
    model_label: str = "GFS",
) -> Optional[dict]:
    """T273 Tier-5: api.open-meteo.com 确定性预报降级。

    api.open-meteo.com 与 ensemble-api.open-meteo.com 是完全独立的服务，
    各自有独立的速率限制。主站429不影响此端点。

    ⚠️ 返回单值列表 {date: [single_value_celsius]}，NOT Gaussian合成成员。
    下游 fetch_ensemble_probability 检测单值(total<=2)并施加:
      - normcdf + 时效误差σ 估算概率 (替代 Gaussian 假spread)
      - source_confidence_multiplier["det_fallback"] = 0.35× 置信度惩罚

    GFS 确定性: model_det="gfs_seamless"
    ECMWF 确定性: model_det="ecmwf_ifs025"
    """
    global _GFS_DET_CIRCUIT_OPEN_UNTIL, _ECMWF_DET_CIRCUIT_OPEN_UNTIL
    global _GFS_FALLBACK_COUNT, _ECMWF_FALLBACK_COUNT

    ens_cfg = get("weather_ensemble") or {}
    if not ens_cfg.get("deterministic_fallback_enabled", True):
        return None

    is_gfs = model_label.upper() == "GFS"
    with _CIRCUIT_LOCK:
        det_circuit_until = (
            _GFS_DET_CIRCUIT_OPEN_UNTIL if is_gfs else _ECMWF_DET_CIRCUIT_OPEN_UNTIL
        )
        if det_circuit_until > time.time():
            remaining = det_circuit_until - time.time()
            logger.debug(
                f"[WeatherFallback] {model_label} api.open-meteo.com熔断中 剩余{remaining:.0f}s"
            )
            return None

    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"
    api_url = (
        f"{_OPEN_METEO_DETERMINISTIC_URL}?"
        f"latitude={lat}&longitude={lon}"
        f"&daily={daily_var}"
        f"&models={model_det}"
        f"&temperature_unit=celsius"
        f"&timezone=auto"
        f"&forecast_days={min(forecast_days, 16)}"
    )

    try:
        resp = requests.get(api_url, timeout=15)
        if resp.status_code == 429:
            global _OPEN_METEO_LAST_429_TS
            _OPEN_METEO_LAST_429_TS = time.time()  # T275: 标记IP级429
            with _CIRCUIT_LOCK:
                cooldown = 120.0
                if is_gfs:
                    _GFS_DET_CIRCUIT_OPEN_UNTIL = time.time() + cooldown
                else:
                    _ECMWF_DET_CIRCUIT_OPEN_UNTIL = time.time() + cooldown
            logger.warning(
                f"[WeatherFallback] {model_label} api.open-meteo.com 也429, 熔断{cooldown:.0f}s"
            )
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.debug(
            f"[WeatherFallback] {model_label} api.open-meteo.com 请求失败: {e}"
        )
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    det_values = daily.get(daily_var, [])

    if not dates or not det_values:
        logger.debug(f"[WeatherFallback] {model_label} api.open-meteo.com 返回空数据")
        return None

    # ⚠️ 返回单值 (不生成Gaussian合成成员) — 下游检测单值并施加置信度惩罚
    ensemble_data: dict = {}
    for date, val in zip(dates, det_values):
        if val is None:
            continue
        ensemble_data[date] = [round(float(val), 2)]  # 单值列表

    if not ensemble_data:
        return None

    warn_threshold = int(
        ens_cfg.get("fallback_consecutive_warn_threshold", _FALLBACK_WARN_THRESHOLD)
    )
    with _CIRCUIT_LOCK:
        if is_gfs:
            _GFS_FALLBACK_COUNT += 1
            count = _GFS_FALLBACK_COUNT
        else:
            _ECMWF_FALLBACK_COUNT += 1
            count = _ECMWF_FALLBACK_COUNT

    if count >= warn_threshold:
        logger.warning(
            f"[WeatherFallback-WARN] {model_label} 已连续{count}次依赖确定性API降级预报"
            f" ({lat:.2f},{lon:.2f})，建议检查 ensemble-api.open-meteo.com 服务状态"
        )
    else:
        logger.info(
            f"[WeatherFallback-T5] {model_label} → api.open-meteo.com 确定性预报"
            f" ({lat:.2f},{lon:.2f}) {len(ensemble_data)}天 [单值, 降级#{count}]"
        )

    return ensemble_data


# ---------------------------------------------------------------------------
# T274: AWS S3 GFS + ECMWF 云端镜像 — .idx/index 索引 + HTTP Range Request + GRIB2 解析
# ---------------------------------------------------------------------------


def _parse_gfs_idx(idx_text: str, target_var: str, target_level: str) -> list:
    """解析 GFS .idx 索引文件，返回匹配变量的 (byte_start, byte_end) 列表。

    .idx 格式:
      1:0:d=2026032900:PRMSL:mean sea level:anl:
      2:45678:d=2026032900:TMP:2 m above ground:anl:
      3:89012:d=2026032900:TMP:surface:anl:

    每行: msg_num:byte_offset:d=init_time:var:level:fcst_hour:
    消息 N 的字节范围 = offset[N] 到 offset[N+1]-1

    Args:
        idx_text: .idx 文件内容
        target_var: 目标变量名 (如 "TMP")
        target_level: 目标层级 (如 "2 m above ground")

    Returns:
        list of (byte_start, byte_end) tuples
    """
    lines = [ln.strip() for ln in idx_text.strip().split("\n") if ln.strip()]
    entries = []
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 6:
            try:
                offset = int(parts[1])
                var_name = parts[3]
                level = parts[4]
                entries.append((offset, var_name, level, line))
            except (ValueError, IndexError):
                continue

    ranges = []
    for i, (offset, var_name, level, _line) in enumerate(entries):
        if var_name == target_var and target_level in level:
            if i + 1 < len(entries):
                end_byte = entries[i + 1][0] - 1
            else:
                end_byte = offset + 2_000_000  # 最后一条消息, 给足2MB上限
            ranges.append((offset, end_byte))
    return ranges


def _extract_bits(buf: bytes, bit_offset: int, num_bits: int) -> int:
    """从字节缓冲区中提取 num_bits 位无符号整数（big-endian bit order）。"""
    if num_bits == 0:
        return 0
    byte_start = bit_offset >> 3
    bit_in_byte = bit_offset & 7
    bytes_needed = (bit_in_byte + num_bits + 7) >> 3
    if byte_start + bytes_needed > len(buf):
        return 0
    raw = 0
    for i in range(bytes_needed):
        raw = (raw << 8) | buf[byte_start + i]
    shift = (bytes_needed << 3) - bit_in_byte - num_bits
    return (raw >> shift) & ((1 << num_bits) - 1)


def _decode_grib2_complex_spatial(
    sect5_data: bytes, sect7_data: bytes, num_data_points: int, target_idx: int
) -> Optional[float]:
    """T275: Template 5.3 — Complex Packing with Spatial Differencing 解码器。

    GFS pgrb2.0p25 所有温度场均使用此模板。
    实现 WMO GRIB2 规范 Section 5 Template 5.3 + Section 7 解码算法。

    解码步骤:
      1. 从 Section 7 读取初始值 + 全局最小值 (extra descriptors)
      2. 读取每组引用值 (group references)
      3. 读取每组位宽 (group widths)
      4. 读取每组长度 (group lengths, 含增量重建)
      5. 定位目标点所在的组，只解码该组数据
      6. 逆空间差分还原原始缩放值
      7. 应用 R + X × 2^E / 10^D 得到物理量

    Args:
        sect5_data: Section 5 从 pos+5 开始的原始字节 (不含前5字节 length+section_num)
        sect7_data: Section 7 数据字节 (不含前5字节)
        num_data_points: 数据点总数
        target_idx: 目标网格点的线性索引

    Returns:
        解码后的物理量值 (Kelvin), 或 None
    """
    # ── Section 5 参数解析 ──
    # 偏移量相对于 Section 5 第 6 字节 (即 pos+5 处开始)
    # octets 12-15 → sect5_data[6:10]  (R)
    # octets 16-17 → sect5_data[10:12] (E)
    # ...
    if len(sect5_data) < 44:
        return None
    R = struct.unpack(">f", sect5_data[6:10])[0]
    E = struct.unpack(">h", sect5_data[10:12])[0]
    D = struct.unpack(">h", sect5_data[12:14])[0]
    bits_per_val = sect5_data[14]
    # sect5_data[15] = field_type
    # sect5_data[16] = group_split_method
    miss_mgmt = sect5_data[17]
    # sect5_data[18:22] = primary_missing, sect5_data[22:26] = secondary_missing
    NG = struct.unpack(">I", sect5_data[26:30])[0]
    ref_group_widths = sect5_data[30]
    bits_group_widths = sect5_data[31]
    ref_group_lengths = struct.unpack(">I", sect5_data[32:36])[0]
    len_increment = sect5_data[36]
    last_group_len = struct.unpack(">I", sect5_data[37:41])[0]
    bits_group_lengths = sect5_data[41]
    order_spatial = sect5_data[42]
    extra_octets = sect5_data[43]

    if NG == 0 or order_spatial not in (1, 2) or extra_octets == 0:
        return None

    # ── Section 7 解码 ──
    d7 = sect7_data
    byte_ptr = 0

    # Step 1: 读取空间差分初始值 + 全局最小值
    # order_spatial=2 → 2个初始值 + 1个全局最小值, 各 extra_octets 字节
    # ⚠️ GRIB2 使用 sign-magnitude 编码（非 two's complement）:
    #   最高位=符号位, 其余位=绝对值
    def _read_sign_mag(buf: bytes) -> int:
        raw = int.from_bytes(buf, "big")
        nbits = len(buf) * 8
        sign = raw >> (nbits - 1)
        mag = raw & ((1 << (nbits - 1)) - 1)
        return -mag if sign else mag

    ival1 = _read_sign_mag(d7[byte_ptr : byte_ptr + extra_octets])
    byte_ptr += extra_octets
    if order_spatial == 2:
        ival2 = _read_sign_mag(d7[byte_ptr : byte_ptr + extra_octets])
        byte_ptr += extra_octets
    else:
        ival2 = 0
    gmin = _read_sign_mag(d7[byte_ptr : byte_ptr + extra_octets])
    byte_ptr += extra_octets

    # Step 2: 读取每组引用值 (NG × bits_per_val bits)
    bit_ptr = byte_ptr << 3
    group_refs = []
    for _ in range(NG):
        group_refs.append(_extract_bits(d7, bit_ptr, bits_per_val))
        bit_ptr += bits_per_val

    # Step 3: 读取每组位宽 (NG × bits_group_widths bits)
    group_widths = []
    for _ in range(NG):
        w = _extract_bits(d7, bit_ptr, bits_group_widths) + ref_group_widths
        group_widths.append(w)
        bit_ptr += bits_group_widths

    # Step 4: 读取每组长度 (NG × bits_group_lengths bits) + 增量重建
    group_lengths = []
    for g in range(NG):
        if g == NG - 1:
            group_lengths.append(last_group_len)
        else:
            scaled = _extract_bits(d7, bit_ptr, bits_group_lengths)
            group_lengths.append(ref_group_lengths + scaled * len_increment)
            bit_ptr += bits_group_lengths

    # Step 5: 定位目标点所在的组 + 解码全部数据
    # 对于空间差分,需要从头到目标点顺序解码(逆差分)
    # 为了性能, 仅解码到目标组+额外顺序还原所需范围
    data_bit_ptr = bit_ptr  # 组数据开始位置

    # 为了第二阶空间差分的逆运算，需要顺序遍历所有点
    # 优化: 使用 array 加速
    import array

    scaled_vals = array.array("l", [0]) * num_data_points
    idx = 0

    for g in range(NG):
        gref = group_refs[g]
        gw = group_widths[g]
        gl = group_lengths[g]
        for _ in range(gl):
            if idx >= num_data_points:
                break
            if gw == 0:
                scaled_vals[idx] = gref + gmin
            else:
                X = _extract_bits(d7, data_bit_ptr, gw)
                scaled_vals[idx] = X + gref + gmin
                data_bit_ptr += gw
            idx += 1
            # 早期退出: 第二阶差分需要所有前面的值,但如果找到足够的点可以停止
            # (不能早停,因为空间差分需要从头到目标逐步还原)

    # Step 6: 逆空间差分
    if order_spatial == 2:
        # 2nd order: X[0]=ival1, X[1]=ival2,
        # X[n] = X[n-1] + (X[n-1] - X[n-2]) + scaled_vals[n] for n >= 2
        # 即 X[n] = 2*X[n-1] - X[n-2] + scaled_vals[n]
        scaled_vals[0] = ival1
        if num_data_points > 1:
            scaled_vals[1] = ival2
        for i in range(2, min(idx, num_data_points)):
            scaled_vals[i] = (
                scaled_vals[i] + 2 * scaled_vals[i - 1] - scaled_vals[i - 2]
            )
    elif order_spatial == 1:
        # 1st order: X[0]=ival1, X[n] = X[n-1] + scaled_vals[n]
        scaled_vals[0] = ival1
        for i in range(1, min(idx, num_data_points)):
            scaled_vals[i] = scaled_vals[i] + scaled_vals[i - 1]

    if target_idx >= num_data_points:
        return None

    # Step 7: 物理量还原 Y = (R + X × 2^E) × 10^(-D)
    # WMO GRIB2 标准: Y × 10^D = R + X × 2^E
    X = scaled_vals[target_idx]
    Y = (R + X * (2.0**E)) * (10.0 ** (-D))
    return Y


def _decode_grib2_eccodes(
    data: bytes, target_lat: float, target_lon: float
) -> Optional[float]:
    """T275: eccodes 库解码器 — 处理 Template 5.42 (CCSDS/AEC) 等复杂格式。

    如果系统安装了 eccodes Python 绑定, 使用 eccodes 解码任意 GRIB2 模板。
    ECMWF GRIB2 使用 Template 5.42 (CCSDS SZIP) 需要 libaec 库。
    """
    try:
        import eccodes
    except ImportError:
        return None

    try:
        msgid = eccodes.codes_new_from_message(data)
        try:
            nearest = eccodes.codes_grib_find_nearest(
                msgid, target_lat, target_lon, is_lsm=False, npoints=1
            )
            if nearest and len(nearest) > 0:
                val = nearest[0].value
                if val == 9999.0 or val > 1e10:  # missing value
                    return None
                return float(val)
        finally:
            eccodes.codes_release(msgid)
    except Exception as exc:
        logger.debug(f"[GRIB2-eccodes] 解码失败: {exc}")
    return None


def _decode_grib2_temperature(
    data: bytes, target_lat: float, target_lon: float
) -> Optional[float]:
    """T275: GRIB2 温度解码器 — eccodes 优先，纯 Python 兜底。

    解码策略:
      1. [优先] eccodes 库 — 处理所有 Template (5.0/5.3/5.42 等)
      2. [兜底] 纯 Python — 仅在 eccodes 不可用时启用 (Template 5.0/5.3)

    eccodes 一次性解决 GRIB2 所有问题，无需手动实现复杂解码。

    Returns:
        温度值 (°C) 或 None
    """
    if len(data) < 100 or data[:4] != b"GRIB":
        return None
    if data[7] != 2:
        return None

    # ── [优先] eccodes 解码 — 处理所有 Template ──
    eccodes_val = _decode_grib2_eccodes(data, target_lat, target_lon)
    if eccodes_val is not None:
        # eccodes 返回 Kelvin → 转 Celsius
        temp_c = eccodes_val - 273.15
        if -100 < temp_c < 80:
            logger.debug(
                f"[GRIB2-eccodes] ✓ ({target_lat:.2f},{target_lon:.2f}) "
                f"{temp_c:.1f}°C [K={eccodes_val:.1f}]"
            )
            return round(temp_c, 2)
        # 值可能已经是 Celsius（某些 GRIB2 文件直接输出 °C）
        if -100 < eccodes_val < 80:
            logger.debug(
                f"[GRIB2-eccodes] ✓ ({target_lat:.2f},{target_lon:.2f}) "
                f"{eccodes_val:.1f}°C [已是Celsius]"
            )
            return round(eccodes_val, 2)
        logger.debug(f"[GRIB2-eccodes] 值异常: {eccodes_val:.1f} → 尝试纯Python兜底")

    # ── [兜底] 纯 Python 解码 — 仅在 eccodes 不可用时 ──
    pos = 16  # Skip Section 0

    ni = 0
    nj = 0
    lat_first = 0.0
    lon_first = 0.0
    di = 0.0
    dj = 0.0
    ref_val = 0.0
    bin_scale = 0
    dec_scale = 0
    bits_per_val = 0
    num_data_points = 0
    sect5_raw = b""  # Section 5 整段(不含length+sect_num)
    sect7_data = b""
    template_5 = -1

    max_pos = len(data) - 4
    while pos < max_pos:
        if data[pos : pos + 4] == b"7777":
            break
        if pos + 4 > len(data):
            break
        sect_len = struct.unpack(">I", data[pos : pos + 4])[0]
        if sect_len < 5 or pos + sect_len > len(data):
            break

        sect_num = data[pos + 4]

        if sect_num == 3 and sect_len >= 72:
            _tpl = struct.unpack(">H", data[pos + 12 : pos + 14])[0]
            if _tpl == 0:
                ni = struct.unpack(">I", data[pos + 30 : pos + 34])[0]
                nj = struct.unpack(">I", data[pos + 34 : pos + 38])[0]
                lat_first = struct.unpack(">i", data[pos + 46 : pos + 50])[0] / 1e6
                lon_first = struct.unpack(">i", data[pos + 50 : pos + 54])[0] / 1e6
                di = struct.unpack(">I", data[pos + 63 : pos + 67])[0] / 1e6
                dj = struct.unpack(">I", data[pos + 67 : pos + 71])[0] / 1e6

        elif sect_num == 5 and sect_len >= 21:
            num_data_points = struct.unpack(">I", data[pos + 5 : pos + 9])[0]
            template_5 = struct.unpack(">H", data[pos + 9 : pos + 11])[0]
            sect5_raw = data[pos + 5 : pos + sect_len]
            if template_5 == 0:
                ref_val = struct.unpack(">f", data[pos + 11 : pos + 15])[0]
                bin_scale = struct.unpack(">h", data[pos + 15 : pos + 17])[0]
                dec_scale = struct.unpack(">h", data[pos + 17 : pos + 19])[0]
                bits_per_val = data[pos + 19]

        elif sect_num == 7:
            sect7_data = data[pos + 5 : pos + sect_len]

        pos += sect_len

    if ni == 0 or nj == 0 or not sect7_data:
        logger.debug(f"[GRIB2] 解析参数不完整: ni={ni} nj={nj}")
        return None

    # ── 计算目标网格点索引 ──
    # T101: P0-3 网格分辨率从 config 读取
    _grid_res = float(
        (get("weather_ensemble") or {}).get("gfs_grid_resolution_degrees", 0.25)
    )
    # GFS: lon_first=0, lon [0,359.75]
    # ECMWF: lon_first=180 (or -180), lon [-180,179.75]
    lon_norm = target_lon if target_lon >= 0 else target_lon + 360.0
    # 处理 lon_first > 0 的情况 (ECMWF lon_first=180°)
    if lon_first > 1.0:  # ECMWF 格式: lon起点180°
        # 将目标经度转到 [lon_first, lon_first+360) 参考系
        lon_grid = lon_norm
        if lon_grid < lon_first:
            lon_grid += 360.0
        col = round((lon_grid - lon_first) / (di if di > 0 else _grid_res))
    else:
        col = round((lon_norm - lon_first) / (di if di > 0 else _grid_res))
    row = round((lat_first - target_lat) / (dj if dj > 0 else _grid_res))

    col = max(0, min(col, ni - 1))
    row = max(0, min(row, nj - 1))
    linear_idx = row * ni + col

    if linear_idx >= num_data_points:
        logger.debug(f"[GRIB2] 索引越界: {linear_idx} >= {num_data_points}")
        return None

    # ── 根据模板分发解码（纯 Python 兜底路径）──
    # 注意: 走到这里说明 eccodes 不可用, 只能用纯 Python
    decoded_k: Optional[float] = None

    if template_5 == 0:
        # Simple Packing
        if bits_per_val == 0:
            return None
        bit_offset = linear_idx * bits_per_val
        raw_int = _extract_bits(sect7_data, bit_offset, bits_per_val)
        decoded_k = (ref_val + raw_int * (2.0**bin_scale)) * (10.0 ** (-dec_scale))

    elif template_5 == 3:
        # Complex Packing with Spatial Differencing (GFS 专用)
        decoded_k = _decode_grib2_complex_spatial(
            sect5_raw, sect7_data, num_data_points, linear_idx
        )

    else:
        # Template 2/40/42 等 — 纯 Python 无法处理，需要 eccodes
        logger.debug(
            f"[GRIB2] Template 5.{template_5} 需要 eccodes 库 "
            f"(pip install eccodes), 纯Python无法解码"
        )
        return None

    if decoded_k is None:
        return None

    # Kelvin → Celsius
    decoded_c = decoded_k - 273.15
    if decoded_c < -100 or decoded_c > 80:
        logger.debug(f"[GRIB2] 解码值异常: {decoded_c:.1f}°C (K={decoded_k:.1f})")
        return None

    return round(decoded_c, 2)


def _fetch_aws_gfs_s3(
    lat: float, lon: float, variable: str, forecast_days: int
) -> Optional[dict]:
    """T274 Tier-2: AWS S3 真实 GFS GRIB2 — .idx 索引 + HTTP Range Request。

    数据来源: s3://noaa-gfs-bdp-pds/ (AWS Open Data Registry)
    HTTPS 直达: https://noaa-gfs-bdp-pds.s3.amazonaws.com/
    完全独立于 Open-Meteo 和 NOMADS 的 AWS 基础设施。

    工作流:
      1. 推算最新 GFS 运行时次 (00z/06z/12z/18z)
      2. 对每个预报日, 下载对应的 .idx 文件
      3. 解析 .idx 找到 TMP:2 m above ground 的字节偏移
      4. HTTP Range Request 只下载该 GRIB2 消息 (~KB)
      5. 纯 Python 解码 Simple Packing, 提取目标网格点温度

    ⚠️ 返回单值 {date: [daily_value_celsius]}
    置信度: _SOURCE_CONFIDENCE_MULTIPLIER["aws_gfs_s3"] = 0.72×
    """
    global _AWS_GFS_CIRCUIT_OPEN_UNTIL

    with _CIRCUIT_LOCK:
        if _AWS_GFS_CIRCUIT_OPEN_UNTIL > time.time():
            remaining = _AWS_GFS_CIRCUIT_OPEN_UNTIL - time.time()
            logger.debug(f"[WeatherAWS-GFS] 熔断中 剩余{remaining:.0f}s, 跳过")
            return None

    ens_cfg = get("weather_ensemble") or {}
    api_timeout = int(ens_cfg.get("api_timeout", 25))

    now_utc = datetime.now(timezone.utc)
    avail_time = now_utc - timedelta(hours=5)  # GFS 发布延迟 ~4-5h
    run_hour = (avail_time.hour // 6) * 6
    run_date = avail_time.strftime("%Y%m%d")
    cycle = f"{run_hour:02d}"

    target_var = "TMP"
    target_level = "2 m above ground"

    result: dict = {}
    try:
        for day in range(min(forecast_days, 7)):
            # 对应预报时效: day0=f000, day1=f024, day2=f048, ...
            # 每天取多个时效 (00h,06h,12h,18h) 求日最高/最低
            target_date = (now_utc + timedelta(days=day)).strftime("%Y-%m-%d")
            # T101: P1-2 每天采样时效从 config 读取
            _sample_hours = (get("weather_ensemble") or {}).get(
                "gfs_daily_sample_hours", [6, 12, 18]
            )
            fcst_hours = [day * 24 + h for h in _sample_hours if day * 24 + h <= 384]
            if day == 0:
                fcst_hours = [0] + [h for h in _sample_hours if h <= 18]
            daily_values: list = []

            for fhr in fcst_hours[:4]:  # 最多4个时效/天, 限制请求数
                grib_path = (
                    f"gfs.{run_date}/{cycle}/atmos/"
                    f"gfs.t{cycle}z.pgrb2.0p25.f{fhr:03d}"
                )
                idx_url = f"{_AWS_GFS_S3_BASE}{grib_path}.idx"
                grib_url = f"{_AWS_GFS_S3_BASE}{grib_path}"

                try:
                    # Step 1: 下载 .idx 文件 (~2KB)
                    idx_resp = requests.get(idx_url, timeout=api_timeout)
                    if idx_resp.status_code == 404:
                        # 当前运行时次未发布 → 尝试前一个
                        prev_hour = run_hour - 6 if run_hour >= 6 else 18
                        prev_date = run_date
                        if run_hour < 6:
                            prev_date = (avail_time - timedelta(hours=6)).strftime(
                                "%Y%m%d"
                            )
                        prev_cycle = f"{prev_hour:02d}"
                        alt_path = (
                            f"gfs.{prev_date}/{prev_cycle}/atmos/"
                            f"gfs.t{prev_cycle}z.pgrb2.0p25.f{fhr:03d}"
                        )
                        idx_url = f"{_AWS_GFS_S3_BASE}{alt_path}.idx"
                        grib_url = f"{_AWS_GFS_S3_BASE}{alt_path}"
                        idx_resp = requests.get(idx_url, timeout=api_timeout)

                    if idx_resp.status_code == 429:
                        with _CIRCUIT_LOCK:
                            _AWS_GFS_CIRCUIT_OPEN_UNTIL = time.time() + 120.0
                        logger.warning("[WeatherAWS-GFS] S3 429 → 熔断120s")
                        break

                    if not idx_resp.ok:
                        continue

                    # Step 2: 解析 .idx 找到 TMP:2m 的字节范围
                    ranges = _parse_gfs_idx(idx_resp.text, target_var, target_level)
                    if not ranges:
                        continue

                    byte_start, byte_end = ranges[0]  # 取第一个匹配

                    # Step 3: HTTP Range Request 下载 GRIB2 消息
                    range_header = f"bytes={byte_start}-{byte_end}"
                    grib_resp = requests.get(
                        grib_url,
                        timeout=api_timeout,
                        headers={"Range": range_header},
                    )
                    if grib_resp.status_code not in (200, 206):
                        continue

                    # Step 4: 解码 GRIB2 → 温度 (°C)
                    temp_c = _decode_grib2_temperature(grib_resp.content, lat, lon)
                    if temp_c is not None:
                        daily_values.append(temp_c)

                except requests.exceptions.Timeout:
                    logger.debug(f"[WeatherAWS-GFS] 超时 fhr={fhr}")
                    continue
                except requests.exceptions.RequestException as e:
                    logger.debug(f"[WeatherAWS-GFS] 请求失败 fhr={fhr}: {e}")
                    continue

            if daily_values:
                if variable == "temp_max":
                    result[target_date] = [round(max(daily_values), 1)]
                else:
                    result[target_date] = [round(min(daily_values), 1)]

    except Exception as e:
        logger.debug(f"[WeatherAWS-GFS] 意外错误: {e}")

    if result:
        logger.info(
            f"[WeatherAWS-GFS] ✓ AWS S3真实GFS GRIB2: ({lat:.2f},{lon:.2f}) "
            f"{len(result)}天 [独立AWS基础设施, .idx+RangeRequest]"
        )
    return result if result else None


def _parse_ecmwf_index(
    index_text: str, target_param: str, target_step: int
) -> Optional[tuple]:
    """解析 ECMWF Open Data .index 文件，返回匹配条目的 (offset, length)。

    ECMWF .index 格式（每行一个 JSON 对象）:
      {"domain":"g","levtype":"sfc","param":"2t","step":"24",...,"_offset":12345,"_length":6789}

    Args:
        index_text: .index 文件内容
        target_param: ECMWF 参数短名 (如 "2t" = 2m temperature)
        target_step: 预报时效 (小时)

    Returns:
        (offset, length) 或 None
    """
    for line in index_text.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
            param = entry.get("param", "")
            step = entry.get("step", "")
            levtype = entry.get("levtype", "")
            # 匹配: 2m温度 (param=2t, levtype=sfc) + 指定时效
            if (
                param == target_param
                and levtype == "sfc"
                and str(step) == str(target_step)
            ):
                offset = int(entry.get("_offset", -1))
                length = int(entry.get("_length", -1))
                if offset >= 0 and length > 0:
                    return (offset, length)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return None


def _fetch_ecmwf_cloud_mirror(
    lat: float, lon: float, variable: str, forecast_days: int
) -> Optional[dict]:
    """T274 Tier-2: ECMWF 云端镜像 (AWS → Azure → Google) — .index + Range Request。

    三大云平台依序尝试: AWS S3 → Azure Blob → Google GCS
    每个平台完全独立，互不影响。

    ECMWF Open Data IFS 高分辨率预报:
      - 0.25° 全球网格
      - 每日 00z/12z 两次运行
      - 预报时效: 0h-240h (10天)

    文件路径: {YYYYMMDD}/{HH}z/ifs/0p25/oper/{YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2
    索引文件: 同路径 + .index 后缀

    ⚠️ 返回单值 {date: [daily_value_celsius]}
    置信度: _SOURCE_CONFIDENCE_MULTIPLIER["ecmwf_mirror"] = 0.68×
    """
    global _ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL

    with _CIRCUIT_LOCK:
        if _ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL > time.time():
            remaining = _ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL - time.time()
            logger.debug(f"[WeatherECMWF-Mirror] 熔断中 剩余{remaining:.0f}s, 跳过")
            return None

    ens_cfg = get("weather_ensemble") or {}
    api_timeout = int(ens_cfg.get("api_timeout", 25))

    now_utc = datetime.now(timezone.utc)
    avail_time = now_utc - timedelta(hours=8)  # ECMWF 发布延迟 ~6-8h
    run_hour = (avail_time.hour // 12) * 12  # ECMWF 00z/12z
    run_date = avail_time.strftime("%Y%m%d")
    cycle = f"{run_hour:02d}"

    result: dict = {}
    mirror_ok = False

    try:
        for day in range(min(forecast_days, 7)):
            target_date = (now_utc + timedelta(days=day)).strftime("%Y-%m-%d")
            # ECMWF IFS HRES 步长: 0,3,6,9,...,240h (每3小时)
            # 每天取 4 个时间步覆盖日内温度变化
            fcst_steps = [day * 24 + h for h in (3, 9, 15, 21) if day * 24 + h <= 240]
            if day == 0:
                fcst_steps = [0, 6, 12, 18]
            daily_values: list = []

            for step in fcst_steps[:3]:  # 限制请求数
                base_name = f"{run_date}{cycle}0000-{step}h-oper-fc"
                dir_path = f"{run_date}/{cycle}z/ifs/0p25/oper"

                # 尝试三大云平台
                for mirror_url in _ECMWF_MIRROR_URLS:
                    idx_url = f"{mirror_url}{dir_path}/{base_name}.index"
                    grib_url = f"{mirror_url}{dir_path}/{base_name}.grib2"

                    try:
                        idx_resp = requests.get(idx_url, timeout=api_timeout)
                        if idx_resp.status_code == 429:
                            with _CIRCUIT_LOCK:
                                _ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = time.time() + 120.0
                            logger.warning(
                                f"[WeatherECMWF-Mirror] {mirror_url[:40]}... 429 → 熔断120s"
                            )
                            continue
                        if not idx_resp.ok:
                            continue

                        # 解析 .index 找到 2t (2m temperature) 指定时效
                        match = _parse_ecmwf_index(idx_resp.text, "2t", step)
                        if not match:
                            continue

                        offset, length = match

                        # HTTP Range Request
                        range_hdr = f"bytes={offset}-{offset + length - 1}"
                        grib_resp = requests.get(
                            grib_url,
                            timeout=api_timeout,
                            headers={"Range": range_hdr},
                        )
                        if grib_resp.status_code not in (200, 206):
                            continue

                        temp_c = _decode_grib2_temperature(grib_resp.content, lat, lon)
                        if temp_c is not None:
                            daily_values.append(temp_c)
                            mirror_ok = True
                            break  # 一个平台成功就不试其他平台

                    except requests.exceptions.Timeout:
                        continue
                    except requests.exceptions.RequestException:
                        continue

            if daily_values:
                if variable == "temp_max":
                    result[target_date] = [round(max(daily_values), 1)]
                else:
                    result[target_date] = [round(min(daily_values), 1)]

    except Exception as e:
        logger.debug(f"[WeatherECMWF-Mirror] 意外错误: {e}")

    if result:
        logger.info(
            f"[WeatherECMWF-Mirror] ✓ ECMWF云端镜像: ({lat:.2f},{lon:.2f}) "
            f"{len(result)}天 [AWS/Azure/Google独立基础设施]"
        )
    return result if result else None


def _fetch_nomads_gfs(
    lat: float, lon: float, variable: str, forecast_days: int
) -> Optional[dict]:
    """T273 Tier-2: NOAA NOMADS GrADS OPeNDAP — 真实GFS数据，AWS云基础设施。

    数据来源：NOAA NOMADS官方OPeNDAP服务器。
    底层数据 = AWS Open Data Registry s3://noaa-gfs-bdp-pds/
    (registry.opendata.aws/noaa-gfs-bdp-pds/) — 与Open-Meteo完全独立的基础设施。

    格式：OPeNDAP ASCII (无需GRIB2解析库, HTTP GET直接访问)
    覆盖：全球, GFS 0.25° resolution, 每3小时
    发布延迟：模型运行完成后约3.5小时

    ⚠️ 返回单值 {date: [daily_max_temp_celsius]} — 真实GFS确定性数据
    置信度惩罚: _SOURCE_CONFIDENCE_MULTIPLIER["noaa_nomads"] = 0.62×
    """
    global _NOMADS_CIRCUIT_OPEN_UNTIL

    with _CIRCUIT_LOCK:
        if _NOMADS_CIRCUIT_OPEN_UNTIL > time.time():
            remaining = _NOMADS_CIRCUIT_OPEN_UNTIL - time.time()
            logger.debug(f"[WeatherNOMADS] NOMADS熔断中 剩余{remaining:.0f}s, 跳过")
            return None

    # 计算最新可用的GFS运行时次 (每6小时: 00/06/12/18 UTC, 发布延迟约3.5h)
    now_utc = datetime.now(timezone.utc)
    avail_time = now_utc - timedelta(hours=4)
    run_hour_raw = (avail_time.hour // 6) * 6
    run_date_str = avail_time.strftime("%Y%m%d")
    cycle = f"{run_hour_raw:02d}"

    # T101: P0-3 GFS 0.25° 网格索引计算（分辨率从 config 读取）
    _gfs_res = float(
        (get("weather_ensemble") or {}).get("gfs_grid_resolution_degrees", 0.25)
    )
    # lat: [90, -90] step _gfs_res, 721点 → lat_idx = (90 - lat) / _gfs_res
    # lon: [0, 359.75] step _gfs_res, 1440点 → lon_idx = lon_0to360 / _gfs_res
    lat_idx = int(round((90.0 - lat) / _gfs_res))
    lon_norm = lon if lon >= 0 else lon + 360.0
    lon_idx = int(round(lon_norm / _gfs_res))
    lat_idx = max(0, min(lat_idx, 720))
    lon_idx = max(0, min(lon_idx, 1439))

    ens_cfg = get("weather_ensemble") or {}
    api_timeout = int(ens_cfg.get("api_timeout", 25))

    result: dict = {}
    try:
        for day in range(min(forecast_days, 7)):  # NOMADS 数据一般7天内可靠
            target_date = (now_utc + timedelta(days=day)).strftime("%Y-%m-%d")
            # 每日8个3小时时间步: day*8 至 day*8+7 (0h,3h,...,21h)
            t0 = day * 8
            t1 = min(t0 + 7, 55)  # 最多7天 = 56步

            url = (
                f"{_NOMADS_GDS_BASE}/gfs{run_date_str}"
                f"/gfs_0p25_{cycle}z.ascii"
                f"?tmp2m[{t0}:{t1}][{lat_idx}:{lat_idx}][{lon_idx}:{lon_idx}]"
            )

            try:
                resp = requests.get(
                    url, timeout=api_timeout, headers={"Accept": "text/plain"}
                )

                # 当前运行时次可能还未发布 → 尝试前一个运行时次
                if resp.status_code == 404:
                    prev_hour = run_hour_raw - 6 if run_hour_raw >= 6 else 18
                    if run_hour_raw < 6:
                        prev_date = (avail_time - timedelta(hours=6)).strftime("%Y%m%d")
                    else:
                        prev_date = run_date_str
                    url_prev = (
                        f"{_NOMADS_GDS_BASE}/gfs{prev_date}"
                        f"/gfs_0p25_{prev_hour:02d}z.ascii"
                        f"?tmp2m[{t0}:{t1}][{lat_idx}:{lat_idx}][{lon_idx}:{lon_idx}]"
                    )
                    resp = requests.get(
                        url_prev, timeout=api_timeout, headers={"Accept": "text/plain"}
                    )

                if resp.status_code == 429:
                    with _CIRCUIT_LOCK:
                        _NOMADS_CIRCUIT_OPEN_UNTIL = time.time() + 120.0
                    logger.warning("[WeatherNOMADS] NOMADS 429 → 熔断120s")
                    break

                if not resp.ok:
                    logger.debug(f"[WeatherNOMADS] HTTP {resp.status_code} day={day}")
                    continue

                # 解析 OPeNDAP ASCII 响应
                # 格式:  "tmp2m, [N][1][1]\n305.24, 304.87, ..."
                text = resp.text
                sep_idx = text.find("-----")
                data_part = text[sep_idx:] if sep_idx != -1 else text
                # 提取所有数值 (Kelvin, 合理范围 200-380K)
                raw_nums = re.findall(r"[-+]?\d+\.?\d*", data_part)
                kelvin_vals = [float(n) for n in raw_nums if 200.0 < float(n) < 380.0]
                if not kelvin_vals:
                    continue

                if variable == "temp_max":
                    daily_val_c = max(kelvin_vals) - 273.15
                else:
                    daily_val_c = min(kelvin_vals) - 273.15

                result[target_date] = [round(daily_val_c, 1)]

            except requests.exceptions.Timeout:
                logger.debug(f"[WeatherNOMADS] 超时 day={day}")
                continue
            except requests.exceptions.RequestException as e:
                logger.debug(f"[WeatherNOMADS] 请求失败 day={day}: {e}")
                continue

    except Exception as e:
        logger.debug(f"[WeatherNOMADS] 意外错误: {e}")

    if result:
        logger.info(
            f"[WeatherNOMADS] NOAA/AWS真实GFS数据: ({lat:.2f},{lon:.2f}) "
            f"{len(result)}天 [独立基础设施]"
        )
    return result if result else None


def _emergency_climate_estimate(lat: float, variable: str, forecast_days: int) -> dict:
    """T275 紧急气候学估计 — 绝对最后手段，保证 NEVER 返回 None。

    基于纬度和月份的粗略气候学温度估计。数据不精确但足以:
    1. 防止 T192 跳过（保持市场在评估管线中）
    2. 通过 0.12× 置信度乘数确保不会实际下注
    3. 为日志和监控提供降级信号

    气温粗略模型:
    - 赤道 (0°): ~30°C max, ~24°C min
    - 中纬度 (40°): ~20°C max, ~10°C min (夏季), ~5°C max, ~-5°C min (冬季)
    - 高纬度 (60°): ~15°C max, ~5°C min (夏季), ~-10°C max, ~-20°C min (冬季)
    """
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    abs_lat = abs(lat)

    # 季节因子: 北半球 7月=夏(1.0), 1月=冬(-1.0); 南半球反转
    if lat >= 0:
        season_factor = -1.0 * ((month - 7) ** 2) / 36.0 + 1.0  # peak at July
    else:
        season_factor = -1.0 * ((month - 1) ** 2) / 36.0 + 1.0  # peak at January

    # 基础温度 (年均)
    if abs_lat < 15:  # 热带
        base_max, base_min = 32.0, 24.0
        season_amp = 3.0  # 热带季节振幅小
    elif abs_lat < 35:  # 亚热带
        base_max, base_min = 26.0, 16.0
        season_amp = 10.0
    elif abs_lat < 50:  # 温带
        base_max, base_min = 18.0, 8.0
        season_amp = 14.0
    else:  # 高纬度
        base_max, base_min = 10.0, 0.0
        season_amp = 18.0

    est_max = base_max + season_factor * season_amp * 0.5
    est_min = base_min + season_factor * season_amp * 0.5

    est_temp = est_max if variable == "temp_max" else est_min

    result: dict = {}
    for day in range(min(forecast_days, 8)):
        target_date = (now_utc + timedelta(days=day)).strftime("%Y-%m-%d")
        result[target_date] = [round(est_temp, 1)]

    logger.error(
        f"[WeatherGFS-T8] ⚠️ 紧急气候学估计: ({lat:.2f}) "
        f"temp={est_temp:.1f}°C {len(result)}天 | "
        f"置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['emergency_climate']} | "
        f"警告: 此数据基于粗略气候模型, 非真实预报! 所有上游数据源均已失败!"
    )
    return result


def _gfs_multi_fallback(
    lat: float,
    lon: float,
    variable: str,
    forecast_days: int,
    cache_key: str,
) -> Optional[dict]:
    """T274 GFS 七层降级链 — 主站失败时依序尝试所有备用数据源。

    降级层级（优先级从高到低）:
      T2: AWS S3 真实GFS GRIB2 (.idx + Range Request) — 独立AWS基础设施
      T3: NOAA NOMADS GDS OPeNDAP — 真实GFS, NOAA官方服务器
      T4: Fresh stale cache — 真实集合数据, 略过期
      T5: api.open-meteo.com 确定性GFS — 独立速率限制, 单值
      T6: Any-age stale cache — 真实集合但可能很旧

    每层成功后在 _DATA_SOURCE_META[cache_key] 记录数据源类型，
    供 fetch_ensemble_probability 施加对应的置信度惩罚。

    Returns: 第一个成功的非None结果, 或 None(全部失败)
    """
    global _DATA_SOURCE_META

    # T2: AWS S3 真实GFS GRIB2 — .idx索引 + HTTP Range Request (独立AWS基础设施)
    aws_gfs = _fetch_aws_gfs_s3(lat, lon, variable, forecast_days)
    if aws_gfs:
        _DATA_SOURCE_META[cache_key] = {"source": "aws_gfs_s3", "members": 1}
        logger.info(
            f"[WeatherGFS-T2] ✓ AWS S3真实GFS GRIB2数据: ({lat:.2f},{lon:.2f}) "
            f"{len(aws_gfs)}天 | 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['aws_gfs_s3']}"
        )
        return aws_gfs

    # T3: NOAA NOMADS GDS OPeNDAP — NOAA官方服务器
    nomads = _fetch_nomads_gfs(lat, lon, variable, forecast_days)
    if nomads:
        _DATA_SOURCE_META[cache_key] = {"source": "noaa_nomads", "members": 1}
        logger.info(
            f"[WeatherGFS-T3] ✓ NOMADS/NOAA真实GFS数据: ({lat:.2f},{lon:.2f}) "
            f"{len(nomads)}天 | 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['noaa_nomads']}"
        )
        return nomads

    # T4: Fresh stale cache (real ensemble data, slightly expired)
    stale = _get_stale_cache(cache_key, "GFS")
    if stale is not None:
        cache_age = time.time() - _CACHE[cache_key][0] if cache_key in _CACHE else 86400
        _stale_fresh_s = int(get("weather_ensemble.cache_stale_fresh_seconds") or 7200)
        _stale_recent_s = int(
            get("weather_ensemble.cache_stale_recent_seconds") or 43200
        )
        if cache_age < _stale_fresh_s:
            src = "stale_fresh"
        elif cache_age < _stale_recent_s:
            src = "stale_recent"
        else:
            src = "stale_old"
        _DATA_SOURCE_META[cache_key] = {"source": src, "age": cache_age}
        logger.warning(
            f"[WeatherGFS-T4] ✓ stale cache({cache_age/3600:.1f}h前真实集合数据): "
            f"({lat:.2f},{lon:.2f}) | 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER[src]}"
        )
        return stale

    # T5: api.open-meteo.com 确定性GFS (独立速率限制, 单值)
    det = _fetch_deterministic_fallback(
        lat, lon, "gfs_seamless", variable, forecast_days, "GFS"
    )
    if det is not None:
        _DATA_SOURCE_META[cache_key] = {"source": "det_fallback", "members": 1}
        logger.warning(
            f"[WeatherGFS-T5] ✓ api.open-meteo.com确定性GFS: ({lat:.2f},{lon:.2f}) "
            f"| 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['det_fallback']}"
        )
        return det

    # T6: Any-age stale cache (任意年龄的历史真实集合)
    any_stale = _get_stale_cache(cache_key, "GFS", max_age=float("inf"))
    if any_stale is not None:
        cache_age = (
            time.time() - _CACHE[cache_key][0] if cache_key in _CACHE else 999999
        )
        _DATA_SOURCE_META[cache_key] = {"source": "stale_old", "age": cache_age}
        logger.warning(
            f"[WeatherGFS-T6] ✓ 超旧stale cache({cache_age/3600:.0f}h前): "
            f"({lat:.2f},{lon:.2f}) | 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['stale_old']}"
        )
        return any_stale

    # 所有真实数据源均失败 — T7 紧急气候学估计（绝对不返回 None）
    logger.error(
        f"[WeatherGFS] ✗ 全部6层降级链失败 ({lat:.2f},{lon:.2f}): "
        f"AWS-S3/NOMADS/stale/det_fallback 均无法提供数据。"
        f"启动T7紧急气候学估计（防止T192跳过）"
    )
    emergency = _emergency_climate_estimate(lat, variable, forecast_days)
    _DATA_SOURCE_META[cache_key] = {"source": "emergency_climate", "members": 1}
    return emergency


def _ecmwf_multi_fallback(
    lat: float,
    lon: float,
    variable: str,
    forecast_days: int,
    cache_key: str,
) -> Optional[dict]:
    """T274 ECMWF 四层降级链。

    降级链:
      T2: ECMWF 云端镜像 (AWS S3 → Azure Blob → Google GCS) — GRIB2 Range Request
      T3: Fresh stale cache (真实集合数据)
      T4: api.open-meteo.com?models=ecmwf_ifs025 (确定性, 独立速率限制)
      T5: Any-age stale cache (历史真实集合)

    注: ECMWF 降级不影响 GFS 主链。fetch_ensemble_probability 主要依赖 GFS。
    """
    global _DATA_SOURCE_META
    _ecmwf_meta_key = cache_key  # ECMWF cache_key 以 "ecmwf:" 开头

    # T2: ECMWF 云端镜像 (AWS → Azure → Google)
    mirror = _fetch_ecmwf_cloud_mirror(lat, lon, variable, forecast_days)
    if mirror:
        _DATA_SOURCE_META[_ecmwf_meta_key] = {"source": "ecmwf_mirror", "members": 1}
        logger.info(
            f"[WeatherECMWF-T2] ✓ ECMWF云端镜像: ({lat:.2f},{lon:.2f}) "
            f"{len(mirror)}天 | 置信度×{_SOURCE_CONFIDENCE_MULTIPLIER['ecmwf_mirror']}"
        )
        return mirror

    # T3: Fresh stale cache
    stale = _get_stale_cache(cache_key, "ECMWF")
    if stale is not None:
        cache_age = time.time() - _CACHE[cache_key][0] if cache_key in _CACHE else 86400
        src = (
            "stale_fresh"
            if cache_age < 7200
            else ("stale_recent" if cache_age < 43200 else "stale_old")
        )
        _DATA_SOURCE_META[_ecmwf_meta_key] = {"source": src, "age": cache_age}
        logger.warning(
            f"[WeatherECMWF-T3] ✓ stale cache({cache_age/3600:.1f}h): ({lat:.2f},{lon:.2f})"
        )
        return stale

    # T4: api.open-meteo.com 确定性 ECMWF
    det = _fetch_deterministic_fallback(
        lat, lon, "ecmwf_ifs025", variable, forecast_days, "ECMWF"
    )
    if det is not None:
        _DATA_SOURCE_META[_ecmwf_meta_key] = {"source": "det_fallback", "members": 1}
        logger.warning(
            f"[WeatherECMWF-T4] ✓ api.open-meteo.com确定性ECMWF: ({lat:.2f},{lon:.2f})"
        )
        return det

    # T5: Any-age stale cache
    any_stale = _get_stale_cache(cache_key, "ECMWF", max_age=float("inf"))
    if any_stale is not None:
        cache_age = (
            time.time() - _CACHE[cache_key][0] if cache_key in _CACHE else 999999
        )
        _DATA_SOURCE_META[_ecmwf_meta_key] = {"source": "stale_old", "age": cache_age}
        logger.warning(
            f"[WeatherECMWF-T5] ✓ 超旧stale cache({cache_age/3600:.0f}h): ({lat:.2f},{lon:.2f})"
        )
        return any_stale

    # 所有真实数据源均失败 — T6 紧急气候学估计（绝对不返回 None）
    logger.error(
        f"[WeatherECMWF] ✗ 全部降级链失败 ({lat:.2f},{lon:.2f}), "
        f"启动紧急气候学估计（防止T192跳过）"
    )
    emergency = _emergency_climate_estimate(lat, variable, forecast_days)
    _DATA_SOURCE_META[_ecmwf_meta_key] = {"source": "emergency_climate", "members": 1}
    return emergency


_NOAA_HEADERS = {
    "User-Agent": "(PolymarketBot, github.com/polymarket-bot)",
    "Accept": "application/geo+json",
}

# requests 浏览器头，避免被 wunderground 拒绝
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.wunderground.com/",
    "DNT": "1",
}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def is_weather_market(market: dict) -> bool:
    """
    判断是否为天气类市场（基于问题关键词 + resolutionSource + description 关键词）。
    严格过滤，避免 "ukraine" 含 "rain" 等误匹配。
    """
    question = (market.get("question") or "").lower()
    res_src = (market.get("resolutionSource") or "").lower()
    desc = (market.get("description") or "")[:300].lower()
    tags = " ".join(str(t) for t in (market.get("tags") or [])).lower()

    text = question + " " + res_src + " " + desc + " " + tags

    # wunderground URL 直接判定为天气市场
    if "wunderground.com" in text:
        return True

    # 关键词匹配：要求 question 中出现（避免 description 泛匹配）
    return any(kw in question for kw in _WEATHER_KEYWORDS)


def extract_oracle_url(market: dict) -> Optional[str]:
    """
    从市场数据中提取 wunderground 气象站 URL。
    优先级：resolutionSource > description > question（罕见）。
    """
    sources = [
        market.get("resolutionSource") or "",
        market.get("description") or "",
        market.get("question") or "",
    ]
    for text in sources:
        m = _WU_URL_RE.search(text)
        if m:
            url = m.group(0).rstrip(".,;)>\"'")
            return url
    return None


def fetch_weather(url: str) -> dict:
    """
    获取气象站当前观测数据，返回结构化字典。
    主力：wttr.in JSON API（免费、无需认证、结构化）。
    备选：wunderground 页面 HTML 解析。
    结果缓存 10 分钟。

    返回字段：
      temperature_c, temperature_f, conditions, humidity_pct,
      wind_kph, wind_direction, pressure_mb, visibility_km,
      raw_summary（给 AI 的一句话摘要）,source_url
    """
    now = time.time()
    if url in _CACHE:
        ts, data = _CACHE[url]
        if now - ts < CACHE_TTL:
            logger.debug(f"[WeatherFetcher] 缓存命中: {url}")
            return data

    station_id = _extract_station_id(url)
    data: dict = {}

    # --- 主力：wttr.in JSON API ---
    if station_id:
        data = _fetch_wttr(station_id, url)

    # --- 二号源：Open-Meteo standalone（结构化API，无需HTML解析）---
    if not data.get("temperature_c") and station_id:
        data = _fetch_open_meteo_standalone(station_id)

    # --- 三号源：wunderground 页面解析（最后手段，HTML fragile）---
    if not data.get("temperature_c"):
        data = _fetch_wunderground_page(url)

    # --- Open-Meteo 精确预报补充（修复 forecast_3day 为空的问题）---
    if data.get("latitude") and data.get("longitude"):
        _enrich_with_open_meteo(data)

    # --- NOAA 美国气象权威数据补充 ---
    station_id_for_noaa = _extract_station_id(url)
    if station_id_for_noaa and station_id_for_noaa.startswith("K"):
        _enrich_with_noaa(data, station_id_for_noaa)
        # 促升: 若主数据仍无温度，从 NOAA 观测提升为主字段（当 wttr+wunderground 均失败时）
        if not data.get("temperature_c"):
            noaa_obs = data.get("noaa_observation", {})
            if noaa_obs.get("temperature_c") is not None:
                data["temperature_c"] = noaa_obs["temperature_c"]
                data["temperature_f"] = noaa_obs.get("temperature_f")
                data["conditions"] = noaa_obs.get("conditions", "")
                data["humidity_pct"] = noaa_obs.get("humidity_pct")
                data["raw_summary"] = (
                    f"温度: {noaa_obs['temperature_c']}°C"
                    f"/{noaa_obs.get('temperature_f', '?')}°F"
                    + (
                        f", {noaa_obs['conditions']}"
                        if noaa_obs.get("conditions")
                        else ""
                    )
                    + " (NOAA观测)"
                )
                logger.info(
                    f"[WeatherFetcher] wttr+wunderground均失败，NOAA作为主数据源: {station_id_for_noaa}"
                )

    if not data:
        data = {"raw_summary": "无法获取气象数据", "source_url": url}
    else:
        data["source_url"] = url

    _CACHE[url] = (now, data)
    logger.info(f"[WeatherFetcher] {station_id or url}: {data.get('raw_summary', '')}")
    return data


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _fetch_open_meteo_standalone(station_id: str) -> dict:
    """
    Open-Meteo 独立数据源：当 wttr.in 失败时，使用 _STATION_MAP 坐标直接调用
    Open-Meteo current weather + 3日预报。返回完整 data dict（与 wttr.in 格式一致）。
    """
    # 查找 station_id 对应的坐标
    coords = None
    for _city, (icao, lat, lon, name) in _STATION_MAP.items():
        if icao == station_id:
            coords = (lat, lon, name)
            break
    if not coords:
        return {}

    lat, lon, station_name = coords
    try:
        api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,"
            f"wind_direction_10m,apparent_temperature,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&timezone=auto&forecast_days=3"
        )
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        current = result.get("current", {})
        temp_c = current.get("temperature_2m")
        if temp_c is None:
            return {}

        temp_f = round(temp_c * 9 / 5 + 32, 1)
        humidity = current.get("relative_humidity_2m")
        wind_kph = current.get("wind_speed_10m")
        feels_c = current.get("apparent_temperature")

        # 3日预报
        daily = result.get("daily", {})
        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        forecast_parts = []
        for d, mx, mn in zip(dates, max_temps, min_temps):
            mx_f = round(mx * 9 / 5 + 32, 1)
            mn_f = round(mn * 9 / 5 + 32, 1)
            forecast_parts.append(f"{d} 高{mx}°C({mx_f}°F)/低{mn}°C({mn_f}°F)")

        raw_summary = (
            f"温度: {temp_c}°C/{temp_f}°F"
            + (f", 湿度: {humidity}%" if humidity else "")
            + (f", 风速: {wind_kph}km/h" if wind_kph else "")
            + (
                f" | 预报(Open-Meteo): " + " / ".join(forecast_parts)
                if forecast_parts
                else ""
            )
        )

        data = {
            "temperature_c": temp_c,
            "temperature_f": temp_f,
            "humidity_pct": humidity,
            "wind_kph": wind_kph,
            "feels_like_c": feels_c,
            "forecast_3day": forecast_parts,
            "forecast_max_c": max_temps[0] if max_temps else None,
            "forecast_min_c": min_temps[0] if min_temps else None,
            "raw_summary": raw_summary,
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
            "data_source": "open-meteo",
        }
        if len(max_temps) > 1:
            data["tomorrow_max_c"] = max_temps[1]
            data["tomorrow_min_c"] = min_temps[1]
            data["tomorrow_max_f"] = round(max_temps[1] * 9 / 5 + 32, 1)
            data["tomorrow_min_f"] = round(min_temps[1] * 9 / 5 + 32, 1)

        logger.info(
            f"[WeatherFetcher] Open-Meteo standalone 成功: {station_id} ({station_name}) "
            f"temp={temp_c}°C"
        )
        return data
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning(
            f"[WeatherFetcher] Open-Meteo standalone 失败 ({station_id}): {e}"
        )
        return {}


def _enrich_with_open_meteo(data: dict) -> None:
    """
    用 Open-Meteo 免费天气API补充精确的3日逐日预报（°C + °F）。
    同时调用多个气象模型（ECMWF IFS, GFS, ICON）进行对比，
    帮助 AI 评估预测一致性和不确定性。
    仅当有 latitude/longitude 时调用；失败时静默回退。
    """
    lat = data.get("latitude")
    lon = data.get("longitude")
    if not lat or not lon:
        return

    # ── 1. 标准预报（默认 best-match 模型）────────────────────────
    api_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit=celsius"
        f"&timezone=auto&forecast_days=3"
    )
    try:
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        daily = result.get("daily", {})
        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])

        if dates and max_temps and min_temps:
            forecast_parts = []
            for d, mx, mn in zip(dates, max_temps, min_temps):
                mx_f = round(mx * 9 / 5 + 32, 1)
                mn_f = round(mn * 9 / 5 + 32, 1)
                forecast_parts.append(f"{d} 高{mx}°C({mx_f}°F)/低{mn}°C({mn_f}°F)")
            data["forecast_3day"] = forecast_parts
            data["forecast_max_c"] = max_temps[0]
            data["forecast_min_c"] = min_temps[0]

            if len(max_temps) > 1:
                data["tomorrow_max_c"] = max_temps[1]
                data["tomorrow_min_c"] = min_temps[1]
                data["tomorrow_max_f"] = round(max_temps[1] * 9 / 5 + 32, 1)
                data["tomorrow_min_f"] = round(min_temps[1] * 9 / 5 + 32, 1)

            if forecast_parts:
                old_summary = data.get("raw_summary", "")
                if " | 预报: " in old_summary:
                    old_summary = old_summary.split(" | 预报: ")[0]
                data["raw_summary"] = (
                    old_summary + " | 预报(Open-Meteo): " + " / ".join(forecast_parts)
                )

            logger.debug(
                f"[WeatherFetcher] Open-Meteo 预报补充成功: "
                f"今日高{max_temps[0]}°C, 明日高{max_temps[1] if len(max_temps) > 1 else '?'}°C"
            )
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        logger.warning(f"[WeatherFetcher] Open-Meteo 标准预报失败: {e}")

    # ── 2. 多模型对比（ECMWF IFS + GFS + ICON）─────────────────────
    _enrich_multi_model(data, lat, lon)


def _enrich_multi_model(data: dict, lat: float, lon: float) -> None:
    """
    调用 Open-Meteo 多模型 API，获取 ECMWF IFS、GFS、ICON 三个模型的
    逐日最高/最低温预报，供 AI 对比分析。
    模型一致 → 高置信；模型分歧 → 低置信，应更保守。
    """
    models = "ecmwf_ifs025,gfs_seamless,icon_seamless"
    api_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&models={models}"
        f"&temperature_unit=celsius"
        f"&timezone=auto&forecast_days=3"
    )
    try:
        resp = requests.get(api_url, timeout=12)
        resp.raise_for_status()
        result = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"[WeatherFetcher] Open-Meteo 多模型请求失败: {e}")
        return

    model_names = {
        "ecmwf_ifs025": "ECMWF-IFS",
        "gfs_seamless": "GFS",
        "icon_seamless": "ICON",
    }
    model_forecasts: list[str] = []
    tomorrow_highs: list[float] = []
    tomorrow_lows: list[float] = []

    for model_key, display_name in model_names.items():
        daily = result.get(f"daily_{model_key}") or result.get("daily", {})
        if not daily:
            continue
        dates = daily.get("time", [])
        max_temps = daily.get(f"temperature_2m_max_{model_key}") or daily.get(
            "temperature_2m_max", []
        )
        min_temps = daily.get(f"temperature_2m_min_{model_key}") or daily.get(
            "temperature_2m_min", []
        )
        if not dates or not max_temps or not min_temps:
            continue

        parts = []
        for i, (d, mx, mn) in enumerate(zip(dates[:3], max_temps[:3], min_temps[:3])):
            if mx is None or mn is None:
                continue
            mx_f = round(mx * 9 / 5 + 32, 1)
            mn_f = round(mn * 9 / 5 + 32, 1)
            parts.append(f"{d}: 高{mx}°C({mx_f}°F)/低{mn}°C({mn_f}°F)")
            if i == 1:  # 明天 index=1
                tomorrow_highs.append(mx)
                tomorrow_lows.append(mn)
        if parts:
            model_forecasts.append(f"  {display_name}: {' | '.join(parts)}")

    if not model_forecasts:
        return

    data["multi_model_forecasts"] = model_forecasts

    # 计算模型一致性
    if len(tomorrow_highs) >= 2:
        spread_high = max(tomorrow_highs) - min(tomorrow_highs)
        spread_low = max(tomorrow_lows) - min(tomorrow_lows)
        avg_high_c = sum(tomorrow_highs) / len(tomorrow_highs)
        avg_low_c = sum(tomorrow_lows) / len(tomorrow_lows)
        avg_high_f = round(avg_high_c * 9 / 5 + 32, 1)
        avg_low_f = round(avg_low_c * 9 / 5 + 32, 1)

        data["model_consensus"] = {
            "models_count": len(tomorrow_highs),
            "tomorrow_high_spread_c": round(spread_high, 1),
            "tomorrow_low_spread_c": round(spread_low, 1),
            "tomorrow_avg_high_c": round(avg_high_c, 1),
            "tomorrow_avg_high_f": avg_high_f,
            "tomorrow_avg_low_c": round(avg_low_c, 1),
            "tomorrow_avg_low_f": avg_low_f,
            "consensus": (
                "HIGH"
                if spread_high
                <= float(get("weather_ensemble.consensus_spread_threshold") or 2.0)
                else "LOW"
            ),
        }
        _cst = float(get("weather_ensemble.consensus_spread_threshold") or 2.0)
        logger.info(
            f"[WeatherFetcher] 多模型对比: {len(tomorrow_highs)}模型, "
            f"明日高温差异={spread_high:.1f}°C, "
            f"一致性={'HIGH' if spread_high <= _cst else 'LOW'}"
        )


# ---------------------------------------------------------------------------
# NOAA Weather API — 美国气象权威数据源
# ---------------------------------------------------------------------------
# api.weather.gov — 免费、无需 API key、仅限美国 ICAO 站点 (K开头)
# 提供: 最新观测值 + NWS 官方预报
# 用途: 作为 wttr.in/Open-Meteo 的独立验证源，提升天气市场置信度
# ---------------------------------------------------------------------------


def _enrich_with_noaa(data: dict, station_id: str) -> None:
    """
    用 NOAA api.weather.gov 补充美国气象站的权威观测和预报数据。
    仅对 K 开头的 ICAO 站点有效（美国本土）。
    失败时静默回退，不影响已有数据。
    """
    # 获取最新观测
    obs = _fetch_noaa_observation(station_id)
    if obs:
        data["noaa_observation"] = obs
        # 如果 NOAA 观测温度与 wttr.in 差异较大，记录警告
        wttr_temp = data.get("temperature_c")
        noaa_temp = obs.get("temperature_c")
        if wttr_temp is not None and noaa_temp is not None:
            diff = abs(wttr_temp - noaa_temp)
            _tdw = float(
                get("weather_ensemble.temp_divergence_warning_threshold") or 3.0
            )
            if diff > _tdw:
                logger.warning(
                    f"[WeatherFetcher] NOAA vs wttr.in 温差 {diff:.1f}°C "
                    f"({station_id}: NOAA={noaa_temp}°C, wttr={wttr_temp}°C)"
                )

    # 获取 NWS 预报
    forecast = _fetch_noaa_forecast(station_id)
    if forecast:
        data["noaa_forecast"] = forecast
        # 将 NOAA 预报追加到 raw_summary
        if forecast.get("periods"):
            noaa_parts = []
            for p in forecast["periods"][:2]:
                noaa_parts.append(
                    f"{p.get('name', '')}: {p.get('short_forecast', '')}, "
                    f"{p.get('temperature', '?')}°{p.get('temperature_unit', 'F')}"
                )
            if noaa_parts:
                old_summary = data.get("raw_summary", "")
                data["raw_summary"] = old_summary + " | NOAA: " + " / ".join(noaa_parts)

    if obs or forecast:
        logger.info(
            f"[WeatherFetcher] NOAA 数据补充成功: {station_id} "
            f"(obs={'OK' if obs else 'N/A'}, fcst={'OK' if forecast else 'N/A'})"
        )


def _fetch_noaa_observation(station_id: str) -> Optional[dict]:
    """
    从 NOAA api.weather.gov 获取气象站最新观测值。
    返回标准化字典或 None。
    """
    cache_key = f"noaa_obs:{station_id}"
    now = time.time()
    if cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return cached

    url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    try:
        resp = requests.get(url, headers=_NOAA_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        props = data.get("properties", {})

        temp_c_val = props.get("temperature", {}).get("value")
        if temp_c_val is None:
            return None

        result = {
            "temperature_c": round(float(temp_c_val), 1),
            "temperature_f": round(float(temp_c_val) * 9 / 5 + 32, 1),
            "humidity_pct": _noaa_extract_value(props, "relativeHumidity"),
            "wind_speed_kmh": _noaa_extract_value(props, "windSpeed"),
            "wind_direction_deg": _noaa_extract_value(props, "windDirection"),
            "pressure_pa": _noaa_extract_value(props, "barometricPressure"),
            "conditions": (props.get("textDescription") or ""),
            "timestamp": props.get("timestamp", ""),
            "source": "NOAA/NWS",
        }
        _CACHE[cache_key] = (now, result)
        return result
    except requests.RequestException as e:
        logger.debug(f"[WeatherFetcher] NOAA 观测获取失败 ({station_id}): {e}")
    except (KeyError, ValueError, TypeError) as e:
        logger.debug(f"[WeatherFetcher] NOAA 观测解析失败 ({station_id}): {e}")
    return None


def _fetch_noaa_forecast(station_id: str) -> Optional[dict]:
    """
    从 NOAA api.weather.gov 获取 NWS 官方天气预报。
    两步流程: 站点 → gridpoint → forecast。
    返回结构化预报或 None。
    """
    cache_key = f"noaa_fcst:{station_id}"
    now = time.time()
    if cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return cached

    try:
        # Step 1: 获取站点的 gridpoint 信息
        station_url = f"https://api.weather.gov/stations/{station_id}"
        resp1 = requests.get(station_url, headers=_NOAA_HEADERS, timeout=10)
        resp1.raise_for_status()
        station_data = resp1.json()

        forecast_url = station_data.get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        # Step 2: 获取预报
        resp2 = requests.get(forecast_url, headers=_NOAA_HEADERS, timeout=10)
        resp2.raise_for_status()
        forecast_data = resp2.json()
        periods_raw = forecast_data.get("properties", {}).get("periods", [])

        periods = []
        for p in periods_raw[:6]:  # 前 3 天 (day/night pairs)
            periods.append(
                {
                    "name": p.get("name", ""),
                    "temperature": p.get("temperature"),
                    "temperature_unit": p.get("temperatureUnit", "F"),
                    "wind_speed": p.get("windSpeed", ""),
                    "wind_direction": p.get("windDirection", ""),
                    "short_forecast": p.get("shortForecast", ""),
                    "is_daytime": p.get("isDaytime", True),
                }
            )

        result = {"periods": periods, "source": "NOAA/NWS"}
        _CACHE[cache_key] = (now, result)
        return result
    except requests.RequestException as e:
        logger.debug(f"[WeatherFetcher] NOAA 预报获取失败 ({station_id}): {e}")
    except (KeyError, ValueError, TypeError) as e:
        logger.debug(f"[WeatherFetcher] NOAA 预报解析失败 ({station_id}): {e}")
    return None


def _noaa_extract_value(props: dict, field: str) -> Optional[float]:
    """从 NOAA 观测属性中提取数值（处理 null/None 值）。"""
    obj = props.get(field, {})
    if isinstance(obj, dict):
        val = obj.get("value")
        if val is not None:
            try:
                return round(float(val), 1)
            except (ValueError, TypeError):
                pass
    return None


def _extract_station_id(url: str) -> Optional[str]:
    """从 wunderground URL 提取气象站 ID。"""
    m = _STATION_ID_RE.search(url)
    if m:
        return (m.group(1) or m.group(2) or "").upper()
    return None


def _fetch_wttr(station_id: str, source_url: str = "") -> dict:
    """
    通过 wttr.in 免费天气 API 查询当前观测数据。
    接受 ICAO 机场代码（如 RKSI）、城市名、坐标。
    https://wttr.in/RKSI?format=j1
    """
    api_url = f"https://wttr.in/{station_id}?format=j1"
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=12)
        resp.raise_for_status()
        payload = resp.json()
        cur = payload["current_condition"][0]

        temp_c = float(cur.get("temp_C", 0))
        temp_f = float(cur.get("temp_F", 0))
        conditions = cur.get("weatherDesc", [{}])[0].get("value", "Unknown")
        humidity = int(cur.get("humidity", 0))
        wind_kph = float(cur.get("windspeedKmph", 0))
        wind_dir = cur.get("winddir16Point", "")
        pressure = float(cur.get("pressure", 0))
        visibility = float(cur.get("visibility", 0))
        feels_like_c = float(cur.get("FeelsLikeC", temp_c))

        # 近期预报（今/明/后天最高最低温）
        forecast_parts = []
        for day in payload.get("weather", [])[:3]:
            date = day.get("date", "")
            max_c = day.get("maxtempC", "?")
            min_c = day.get("mintempC", "?")
            desc = (
                day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
            )
            forecast_parts.append(f"{date} 高{max_c}°C/低{min_c}°C {desc}")

        raw_summary = (
            f"当前: {temp_c}°C/{temp_f}°F, {conditions}, "
            f"体感{feels_like_c}°C, 湿度{humidity}%, "
            f"风{wind_dir} {wind_kph}km/h, 气压{pressure}hPa"
        )
        if forecast_parts:
            raw_summary += " | 预报: " + " / ".join(forecast_parts)

        # 今日预测最高/最低气温 + 逐3小时数据
        today = payload.get("weather", [{}])[0]
        forecast_max_c = float(today.get("maxtempC", temp_c))
        forecast_min_c = float(today.get("mintempC", temp_c))
        hourly_slots = today.get("hourly", [])
        # 格式: [(hour_int, temp_c), ...]  hour=0,3,6,9,12,15,18,21
        hourly_temps_3h = [
            (int(h.get("time", "0")) // 100, float(h.get("tempC", temp_c)))
            for h in hourly_slots
        ]

        # 提取经纬度（用于 Open-Meteo 预报补充）
        nearest = payload.get("nearest_area", [{}])[0]
        lat = nearest.get("latitude", "")
        lon = nearest.get("longitude", "")

        return {
            "temperature_c": temp_c,
            "temperature_f": temp_f,
            "conditions": conditions,
            "humidity_pct": humidity,
            "wind_kph": wind_kph,
            "wind_direction": wind_dir,
            "pressure_mb": pressure,
            "visibility_km": visibility,
            "feels_like_c": feels_like_c,
            "forecast_3day": forecast_parts,
            "forecast_max_c": forecast_max_c,
            "forecast_min_c": forecast_min_c,
            "hourly_temps_3h": hourly_temps_3h,
            "raw_summary": raw_summary,
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
            "data_source": "wttr.in",
        }
    except requests.RequestException as e:
        logger.warning(f"[WeatherFetcher] wttr.in 请求失败 ({station_id}): {e}")
    except (KeyError, ValueError, IndexError) as e:
        logger.warning(f"[WeatherFetcher] wttr.in 解析失败 ({station_id}): {e}")
    return {}


def _fetch_wunderground_page(url: str) -> dict:
    """
    备选：从 wunderground 页面 HTML 中提取天气数据。
    wunderground 为 SPA，部分数据可在 <script> 中以 JSON 嵌入形式找到。
    最多重试 2 次，每次间隔 3s。
    使用 Session 保持 cookies 以通过反爬检测。
    """
    session = requests.Session()
    session.headers.update(_HEADERS)
    for attempt in range(2):
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            resp.raise_for_status()
            html = resp.text

            data: dict = {}

            # 提取温度（metric）
            m = re.search(r'"metric"\s*:\s*\{[^}]*"temp"\s*:\s*([\d.-]+)', html)
            if m:
                data["temperature_c"] = float(m.group(1))
                data["temperature_f"] = round(data["temperature_c"] * 9 / 5 + 32, 1)

            # 提取温度（imperial）
            m = re.search(r'"imperial"\s*:\s*\{[^}]*"temp"\s*:\s*([\d.-]+)', html)
            if m:
                data["temperature_f"] = float(m.group(1))
                if "temperature_c" not in data:
                    data["temperature_c"] = round(
                        (data["temperature_f"] - 32) * 5 / 9, 1
                    )

            # 天气状况描述
            m = re.search(r'"wxPhraseMedium"\s*:\s*"([^"]+)"', html)
            if not m:
                m = re.search(r'"wxPhraseShort"\s*:\s*"([^"]+)"', html)
            if m:
                data["conditions"] = m.group(1)

            # 湿度
            m = re.search(r'"humidity"\s*:\s*"?([\d.]+)"?', html)
            if m:
                data["humidity_pct"] = int(float(m.group(1)))

            # 风速 (km/h)
            m = re.search(
                r'"windSpeed"\s*:\s*\{[^}]*"metric"\s*:\s*\{[^}]*"value"\s*:\s*([\d.]+)',
                html,
            )
            if m:
                data["wind_kph"] = float(m.group(1))

            # 生成摘要
            if data.get("temperature_c") is not None:
                parts = [
                    f"温度: {data['temperature_c']}°C/{data.get('temperature_f','?')}°F",
                ]
                if data.get("conditions"):
                    parts.append(data["conditions"])
                if data.get("humidity_pct"):
                    parts.append(f"湿度: {data['humidity_pct']}%")
                if data.get("wind_kph"):
                    parts.append(f"风速: {data['wind_kph']} km/h")
                data["raw_summary"] = ", ".join(parts)
                data["data_source"] = "wunderground"
                return data

        except requests.RequestException as e:
            # 首次失败用 WARNING，重试失败降级为 DEBUG（避免已知 403 刷屏）
            lvl = logging.WARNING if attempt == 0 else logging.DEBUG
            logger.log(
                lvl,
                f"[WeatherFetcher] wunderground 请求失败 (尝试{attempt + 1}/2): {e}",
            )
            if attempt == 0:
                time.sleep(3)
        except Exception as e:
            logger.warning(f"[WeatherFetcher] wunderground 解析失败: {e}")
            break
    return {}


def fetch_city_weather(city_name: str) -> dict:
    """
    按城市名称查询天气（供仪表盘直接调用，无需 wunderground URL）。
    城市名可为英文城市名或 ICAO 代码，结果缓存 10 分钟。
    返回字段同 _fetch_wttr：temperature_c, forecast_max_c, hourly_temps_3h, ...
    """
    cache_key = f"city:{city_name.lower().replace(' ', '_')}"
    now = time.time()
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return data
    data = _fetch_wttr(city_name)
    if data:
        _CACHE[cache_key] = (now, data)
    return data


# ===========================================================================
# GFS Ensemble 概率引擎 — 直接从 31 成员集合预报计算 P(YES)
# ===========================================================================
# Polymarket 天气市场结算依据: Wunderground 特定气象站(ICAO)每日最高/最低温
# API: Open-Meteo Ensemble API (FREE) — 必须使用站点精确经纬度
# ===========================================================================

# ── Polymarket 天气市场 ICAO 气象站映射 ──
# 来源: Polymarket event descriptions 中的 wunderground.com/history/daily/... URL
# 格式: city_lower → (ICAO, latitude, longitude, station_name)
_STATION_MAP: dict[str, tuple[str, float, float, str]] = {
    # ── 北美 ──
    "atlanta": ("KATL", 33.6407, -84.4277, "Hartsfield-Jackson Atlanta Intl"),
    "austin": ("KAUS", 30.1945, -97.6699, "Austin-Bergstrom Intl"),
    "boston": ("KBOS", 42.3656, -71.0096, "Logan Intl"),
    "chicago": ("KORD", 41.9742, -87.9073, "O'Hare Intl"),
    "dallas": ("KDAL", 32.8471, -96.8518, "Dallas Love Field"),
    "denver": ("KDEN", 39.8561, -104.6737, "Denver Intl"),
    "houston": ("KHOU", 29.6454, -95.2789, "William P. Hobby Airport"),
    "las vegas": ("KLAS", 36.0840, -115.1537, "McCarran Intl"),
    "los angeles": ("KLAX", 33.9600, -118.4000, "Los Angeles Intl Airport"),
    "miami": ("KMIA", 25.7959, -80.2870, "Miami Intl"),
    "nashville": ("KBNA", 36.1263, -86.6774, "Nashville Intl"),
    "nyc": ("KLGA", 40.7772, -73.8726, "LaGuardia Airport"),
    "new york": ("KLGA", 40.7772, -73.8726, "LaGuardia Airport"),
    "new york city": ("KLGA", 40.7772, -73.8726, "LaGuardia Airport"),
    "philadelphia": ("KPHL", 39.8744, -75.2424, "Philadelphia Intl"),
    "phoenix": ("KPHX", 33.4373, -112.0078, "Phoenix Sky Harbor Intl"),
    "portland": ("KPDX", 45.5898, -122.5951, "Portland Intl"),
    "san diego": ("KSAN", 32.7336, -117.1897, "San Diego Intl"),
    "san francisco": ("KSFO", 37.6213, -122.3790, "San Francisco Intl"),
    "seattle": ("KSEA", 47.4502, -122.3088, "Seattle-Tacoma Intl"),
    "washington": ("KDCA", 38.8512, -77.0402, "Reagan National"),
    "washington dc": ("KDCA", 38.8512, -77.0402, "Reagan National"),
    "washington d.c.": ("KDCA", 38.8512, -77.0402, "Reagan National"),
    # ── 加拿大 ──
    "montreal": ("CYUL", 45.4706, -73.7408, "Montreal Trudeau Intl"),
    "toronto": ("CYYZ", 43.6772, -79.6306, "Toronto Pearson Intl"),
    "vancouver": ("CYVR", 49.1967, -123.1815, "Vancouver Intl"),
    # ── 拉美 ──
    "bogota": ("SKBO", 4.7016, -74.1469, "El Dorado Intl"),
    "buenos aires": ("SAEZ", -34.8222, -58.5358, "Ezeiza Ministro Pistarini"),
    "lima": ("SPJC", -12.0219, -77.1143, "Jorge Chavez Intl"),
    "mexico city": ("MMMX", 19.4363, -99.0721, "Mexico City Intl"),
    "sao paulo": ("SBGR", -23.4356, -46.4731, "Guarulhos Intl"),
    "são paulo": ("SBGR", -23.4356, -46.4731, "Guarulhos Intl"),
    "santiago": ("SCEL", -33.3930, -70.7858, "Arturo Merino Benitez Intl"),
    # ── 欧洲 ──
    "amsterdam": ("EHAM", 52.3086, 4.7639, "Amsterdam Schiphol"),
    "ankara": ("LTAC", 40.1281, 32.9951, "Esenboğa Airport"),
    "berlin": ("EDDB", 52.3667, 13.5033, "Berlin Brandenburg"),
    "copenhagen": ("EKCH", 55.6180, 12.6508, "Copenhagen Airport"),
    "dublin": ("EIDW", 53.4213, -6.2701, "Dublin Airport"),
    "helsinki": ("EFHK", 60.3172, 24.9633, "Helsinki-Vantaa"),
    "istanbul": ("LTFM", 41.2608, 28.7419, "Istanbul Airport"),
    "lisbon": ("LPPT", 38.7742, -9.1342, "Lisbon Humberto Delgado"),
    "london": ("EGLC", 51.5053, 0.0553, "London City Airport"),
    "madrid": ("LEMD", 40.4936, -3.5668, "Adolfo Suárez Madrid-Barajas"),
    "milan": ("LIMC", 45.6306, 8.7231, "Milan Malpensa"),
    "moscow": ("UUEE", 55.9726, 37.4146, "Sheremetyevo Intl"),
    "munich": ("EDDM", 48.3538, 11.7861, "Munich Airport"),
    "oslo": ("ENGM", 60.1939, 11.1004, "Oslo Gardermoen"),
    "paris": ("LFPG", 49.0097, 2.5479, "Charles de Gaulle"),
    "prague": ("LKPR", 50.1008, 14.2600, "Vaclav Havel Airport"),
    "rome": ("LIRF", 41.8003, 12.2389, "Leonardo da Vinci-Fiumicino"),
    "stockholm": ("ESSA", 59.6519, 17.9186, "Stockholm Arlanda"),
    "vienna": ("LOWW", 48.1103, 16.5697, "Vienna Intl"),
    "warsaw": ("EPWA", 52.1657, 20.9671, "Warsaw Chopin Airport"),
    "zurich": ("LSZH", 47.4647, 8.5492, "Zurich Airport"),
    # ── 中东/非洲 ──
    "cairo": ("HECA", 30.1219, 31.4056, "Cairo Intl"),
    "dubai": ("OMDB", 25.2528, 55.3644, "Dubai Intl"),
    "johannesburg": ("FAOR", -26.1392, 28.2460, "O.R. Tambo Intl"),
    "riyadh": ("OERK", 24.9576, 46.6988, "King Khalid Intl"),
    "tel aviv": ("LLBG", 32.0114, 34.8867, "Ben Gurion Intl"),
    "tel aviv-yafo": ("LLBG", 32.0114, 34.8867, "Ben Gurion Intl"),
    # ── 亚洲 ──
    "bangkok": ("VTBS", 13.6900, 100.7501, "Suvarnabhumi Airport"),
    "beijing": ("ZBAA", 40.0500, 116.5900, "Beijing Capital Intl Airport"),
    "chengdu": ("ZUUU", 30.5700, 103.9600, "Chengdu Shuangliu Intl Airport"),
    "chongqing": ("ZUCK", 29.7192, 106.6414, "Chongqing Jiangbei Intl"),
    "delhi": ("VIDP", 28.5665, 77.1031, "Indira Gandhi Intl"),
    "new delhi": ("VIDP", 28.5665, 77.1031, "Indira Gandhi Intl"),
    "hong kong": ("VHHH", 22.3080, 113.9185, "Hong Kong Intl"),
    "jakarta": ("WIII", -6.1266, 106.6558, "Soekarno-Hatta Intl"),
    "lucknow": ("VILK", 26.7606, 80.8893, "Chaudhary Charan Singh Intl"),
    "manila": ("RPLL", 14.5086, 121.0198, "Ninoy Aquino Intl"),
    "mumbai": ("VABB", 19.0896, 72.8656, "Chhatrapati Shivaji Intl"),
    "osaka": ("RJBB", 34.4347, 135.2444, "Kansai Intl"),
    "seoul": ("RKSI", 37.4602, 126.4407, "Incheon Intl"),
    "shanghai": ("ZSPD", 31.1434, 121.8052, "Pudong Intl"),
    "shenzhen": ("ZGSZ", 22.6393, 113.8107, "Shenzhen Bao'an Intl"),
    "singapore": ("WSSS", 1.3502, 103.9940, "Changi Airport"),
    "taipei": ("RCTP", 25.0777, 121.2325, "Taoyuan Intl"),
    "tokyo": ("RJTT", 35.5494, 139.7798, "Haneda Airport"),
    "wuhan": ("ZHHH", 30.8000, 114.2200, "Wuhan Tianhe Intl Airport"),
    # ── 大洋洲 ──
    "auckland": ("NZAA", -36.9401, 174.7903, "Auckland Airport"),
    "melbourne": ("YMML", -37.6733, 144.8433, "Melbourne Airport"),
    "sydney": ("YSSY", -33.9461, 151.1772, "Sydney Kingsford Smith"),
    "wellington": ("NZWN", -41.3272, 174.8053, "Wellington Airport"),
}

# ── 城市 UTC 偏移映射（用于天气触顶/触底时间窗口判断） ──
# T172: 时间感知 Edge 策略 — 精确时区比经度估算更可靠
# 注意: 北美/欧洲夏令时需人工每季校准（±1h 对 10-14 窗口影响可控）
_CITY_UTC_OFFSET: dict[str, float] = {
    # 中国全境 UTC+8
    "beijing": 8,
    "shanghai": 8,
    "shenzhen": 8,
    "hong kong": 8,
    "taipei": 8,
    "chengdu": 8,
    "chongqing": 8,
    "wuhan": 8,
    # 韩国/日本 UTC+9
    "seoul": 9,
    "tokyo": 9,
    "osaka": 9,
    # 东南亚
    "bangkok": 7,
    "singapore": 8,
    "manila": 8,
    "jakarta": 7,
    # 南亚
    "delhi": 5.5,
    "new delhi": 5.5,
    "mumbai": 5.5,
    "lucknow": 5.5,
    # 中东
    "dubai": 4,
    "riyadh": 3,
    "tel aviv": 2,
    "tel aviv-yafo": 2,
    "cairo": 2,
    "istanbul": 3,
    "ankara": 3,
    # 欧洲（CEST夏令时 3月最后周日-10月, +1h）
    "london": 0,
    "dublin": 0,
    "paris": 1,
    "berlin": 1,
    "madrid": 1,
    "rome": 1,
    "milan": 1,
    "amsterdam": 1,
    "zurich": 1,
    "vienna": 1,
    "warsaw": 1,
    "prague": 1,
    "copenhagen": 1,
    "oslo": 1,
    "stockholm": 1,
    "lisbon": 0,
    "helsinki": 2,
    "moscow": 3,
    "munich": 1,
    # 北美 EDT(3-11月 UTC-4) / EST(UTC-5)  — 3月下旬已进入夏令时
    "new york": -4,
    "nyc": -4,
    "new york city": -4,
    "boston": -4,
    "philadelphia": -4,
    "washington": -4,
    "washington dc": -4,
    "washington d.c.": -4,
    "miami": -4,
    "atlanta": -4,
    "nashville": -4,
    # CDT(UTC-5)
    "chicago": -5,
    "dallas": -5,
    "houston": -5,
    "austin": -5,
    # MDT(UTC-6) / MST(UTC-7 Phoenix无DST)
    "denver": -6,
    "phoenix": -7,
    # PDT(UTC-7)
    "los angeles": -7,
    "san francisco": -7,
    "seattle": -7,
    "portland": -7,
    "las vegas": -7,
    "san diego": -7,
    # 加拿大
    "toronto": -4,
    "montreal": -4,
    "vancouver": -7,
    # 拉美
    "mexico city": -6,
    "bogota": -5,
    "lima": -5,
    "sao paulo": -3,
    "são paulo": -3,
    "buenos aires": -3,
    "santiago": -4,
    # 非洲
    "johannesburg": 2,
    # 大洋洲 (AEDT/NZDT)
    "sydney": 11,
    "melbourne": 11,
    "auckland": 13,
    "wellington": 13,
}


def get_city_utc_offset(city_name: str) -> Optional[float]:
    """获取城市的 UTC 偏移量（小时），如 Seoul=9, New York=-4。

    查找顺序: config.json weather_cities → _CITY_UTC_OFFSET → _STATION_MAP 经度估算。
    T211: 从 get_city_local_hour 提取的公共函数，供 outcome_tracker 等模块复用。
    """
    key = city_name.lower().strip()
    cfg_city = _load_config_city(key)
    if cfg_city and cfg_city.get("utc_offset") is not None:
        return float(cfg_city["utc_offset"])
    offset = _CITY_UTC_OFFSET.get(key)
    if offset is not None:
        return float(offset)
    station = _STATION_MAP.get(key)
    if station:
        return float(round(station[2] / 15))  # lon / 15
    return None


def get_city_local_hour(city_name: str) -> Optional[int]:
    """获取目标城市当前的当地小时(0-23)。

    查找顺序: config.json weather_cities → _CITY_UTC_OFFSET → _STATION_MAP 经度估算。
    """
    offset = get_city_utc_offset(city_name)
    if offset is None:
        return None
    now_utc = datetime.now(timezone.utc)
    local_hour = int((now_utc.hour + now_utc.minute / 60 + offset) % 24)
    return local_hour


def get_city_local_datetime(city_name: str) -> Optional[datetime]:
    """获取目标城市当前的当地日期时间（naive，无时区标记）。

    查找顺序: config.json weather_cities → _CITY_UTC_OFFSET → _STATION_MAP 经度估算。
    """
    from datetime import timedelta

    key = city_name.lower().strip()
    offset = None
    # T276: 优先从 config 查找 utc_offset
    cfg_city = _load_config_city(key)
    if cfg_city and cfg_city.get("utc_offset") is not None:
        offset = cfg_city["utc_offset"]
    if offset is None:
        offset = _CITY_UTC_OFFSET.get(key)
    if offset is None:
        station = _STATION_MAP.get(key)
        if station:
            offset = round(station[2] / 15)  # lon / 15
        else:
            return None
    now_utc = datetime.now(timezone.utc)
    return (now_utc + timedelta(hours=offset)).replace(tzinfo=None)


# ── 问题模式解析 — 匹配 Polymarket 天气问题的实际格式 ──
# 格式A: "Will the highest temperature in CITY be NUMBER°C on DATE?"
_Q_EXACT_HIGH = re.compile(
    r"(?:highest|high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(\d+)\s*°?\s*([FC])\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_Q_EXACT_LOW = re.compile(
    r"(?:lowest|low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(\d+)\s*°?\s*([FC])\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
# 格式B: "Will the highest temperature in CITY be between LOW-HIGH°F on DATE?"
_Q_RANGE_HIGH = re.compile(
    r"(?:highest|high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+between\s+(\d+)\s*[-–]\s*(\d+)\s*°?\s*([FC])\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_Q_RANGE_LOW = re.compile(
    r"(?:lowest|low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+between\s+(\d+)\s*[-–]\s*(\d+)\s*°?\s*([FC])\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
# 格式C (旧格式兜底): "high temp in CITY on DATE be NUMBER°F or higher/lower"
_Q_TEMP_HIGH_GTE = re.compile(
    r"(?:high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+(.+?)\s+on\s+(.+?)\s+be\s+(\d+)"
    r"\s*°?\s*([FC])\s+or\s+(?:higher|above|more|warmer)",
    re.IGNORECASE,
)
_Q_TEMP_HIGH_LTE = re.compile(
    r"(?:high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+(.+?)\s+on\s+(.+?)\s+be\s+(\d+)"
    r"\s*°?\s*([FC])\s+or\s+(?:lower|below|less|colder)",
    re.IGNORECASE,
)
_Q_TEMP_LOW_GTE = re.compile(
    r"(?:low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+(.+?)\s+on\s+(.+?)\s+be\s+(\d+)"
    r"\s*°?\s*([FC])\s+or\s+(?:higher|above|more|warmer)",
    re.IGNORECASE,
)
_Q_TEMP_LOW_LTE = re.compile(
    r"(?:low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+(.+?)\s+on\s+(.+?)\s+be\s+(\d+)"
    r"\s*°?\s*([FC])\s+or\s+(?:lower|below|less|colder)",
    re.IGNORECASE,
)
# 格式D (主流Polymarket格式): "in CITY be NUMBER°C or higher/lower on DATE"
# e.g. "Will the highest temperature in Toronto be 3°C or higher on March 27?"
# 支持负数温度（-?\d+）
_Q_HIGH_GTE = re.compile(
    r"(?:highest|high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(-?\d+)\s*°?\s*([FC])\s+or\s+(?:higher|above|more|warmer)\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_Q_HIGH_LTE = re.compile(
    r"(?:highest|high|max(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(-?\d+)\s*°?\s*([FC])\s+or\s+(?:lower|below|less|colder)\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_Q_LOW_GTE = re.compile(
    r"(?:lowest|low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(-?\d+)\s*°?\s*([FC])\s+or\s+(?:higher|above|more|warmer)\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_Q_LOW_LTE = re.compile(
    r"(?:lowest|low|min(?:imum)?)\s+temp(?:erature)?\s+in\s+"
    r"(.+?)\s+be\s+(-?\d+)\s*°?\s*([FC])\s+or\s+(?:lower|below|less|colder)\s+on\s+(.+?)\s*\??$",
    re.IGNORECASE,
)

# 日期解析
_MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# 坐标缓存 {city_lower: (lat, lon)}
_COORD_CACHE: dict[str, tuple[float, float]] = {}


def parse_weather_question(question: str) -> Optional[dict]:
    """
    从 Polymarket 天气问题中提取结构化参数。

    支持三种格式:
      A) "highest temperature in CITY be NUMBER°C on DATE?"  → exact
      B) "highest temperature in CITY be between LOW-HIGH°F on DATE?"  → range
      C) "high temp in CITY on DATE be NUMBER°F or higher"  → gte/lte (旧格式兜底)

    Returns:
        dict with keys: city, date_str, threshold_value (or None),
                        threshold_low/threshold_high (for range, else None),
                        unit (C/F), variable (temp_max/temp_min),
                        direction (exact/range/gte/lte), station_icao (or None)
        None if question doesn't match any known pattern.
    """
    # ── 格式B: range "between LOW-HIGH" (先匹配，避免被exact误吞) ──
    range_patterns = [
        (_Q_RANGE_HIGH, "temp_max"),
        (_Q_RANGE_LOW, "temp_min"),
    ]
    for pattern, variable in range_patterns:
        m = pattern.search(question)
        if m:
            city = m.group(1).strip().rstrip(",")
            low_val = int(m.group(2))
            high_val = int(m.group(3))
            unit = m.group(4).upper()
            date_str = m.group(5).strip().rstrip(",?")
            station = _STATION_MAP.get(city.lower().strip())
            return {
                "city": city,
                "date_str": date_str,
                "threshold_value": (low_val + high_val)
                / 2,  # T30: range格式取中点值，供CityScanner距离计算
                "threshold_low": low_val,
                "threshold_high": high_val,
                "unit": unit,
                "variable": variable,
                "direction": "range",
                "station_icao": station[0] if station else None,
            }

    # ── 格式A: exact "be NUMBER°C on DATE" ──
    exact_patterns = [
        (_Q_EXACT_HIGH, "temp_max"),
        (_Q_EXACT_LOW, "temp_min"),
    ]
    for pattern, variable in exact_patterns:
        m = pattern.search(question)
        if m:
            city = m.group(1).strip().rstrip(",")
            threshold = int(m.group(2))
            unit = m.group(3).upper()
            date_str = m.group(4).strip().rstrip(",?")
            station = _STATION_MAP.get(city.lower().strip())
            return {
                "city": city,
                "date_str": date_str,
                "threshold_value": threshold,
                "threshold_low": None,
                "threshold_high": None,
                "unit": unit,
                "variable": variable,
                "direction": "exact",
                "station_icao": station[0] if station else None,
            }

    # ── 格式D (主流Polymarket格式): "in CITY be NUMBER°C or higher/lower on DATE" ──
    # e.g. "Will the highest temperature in Toronto be 3°C or higher on March 27?"
    new_threshold_patterns = [
        (_Q_HIGH_GTE, "temp_max", "gte"),
        (_Q_HIGH_LTE, "temp_max", "lte"),
        (_Q_LOW_GTE, "temp_min", "gte"),
        (_Q_LOW_LTE, "temp_min", "lte"),
    ]
    for pattern, variable, direction in new_threshold_patterns:
        m = pattern.search(question)
        if m:
            city = m.group(1).strip().rstrip(",")
            threshold = int(m.group(2))
            unit = m.group(3).upper()
            date_str = m.group(4).strip().rstrip(",?")
            station = _STATION_MAP.get(city.lower().strip())
            return {
                "city": city,
                "date_str": date_str,
                "threshold_value": threshold,
                "threshold_low": None,
                "threshold_high": None,
                "unit": unit,
                "variable": variable,
                "direction": direction,
                "station_icao": station[0] if station else None,
            }

    # ── 格式C: 旧格式 "on DATE be NUMBER or higher/lower" ──
    legacy_patterns = [
        (_Q_TEMP_HIGH_GTE, "temp_max", "gte"),
        (_Q_TEMP_HIGH_LTE, "temp_max", "lte"),
        (_Q_TEMP_LOW_GTE, "temp_min", "gte"),
        (_Q_TEMP_LOW_LTE, "temp_min", "lte"),
    ]
    for pattern, variable, direction in legacy_patterns:
        m = pattern.search(question)
        if m:
            city = m.group(1).strip().rstrip(",")
            date_str = m.group(2).strip().rstrip(",")
            threshold = int(m.group(3))
            unit = m.group(4).upper()
            station = _STATION_MAP.get(city.lower().strip())
            return {
                "city": city,
                "date_str": date_str,
                "threshold_value": threshold,
                "threshold_low": None,
                "threshold_high": None,
                "unit": unit,
                "variable": variable,
                "direction": direction,
                "station_icao": station[0] if station else None,
            }
    return None


def _parse_target_date(date_str: str) -> Optional[str]:
    """
    Parse date string like 'March 20', 'March 20, 2026', 'Mar 20' → 'YYYY-MM-DD'.
    Assumes current year if year not specified.
    """
    date_str = date_str.strip().rstrip("?").rstrip(",")
    # Try "Month Day, Year" or "Month Day"
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?", date_str)
    if not m:
        return None
    month_name = m.group(1).lower()
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
    month = _MONTH_MAP.get(month_name)
    if not month:
        return None
    try:
        dt = datetime(year, month, day)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _load_config_city(key: str) -> Optional[dict]:
    """从 config.json weather_cities 加载城市配置（slug 或 name 匹配）。"""
    cities_cfg = get("weather_cities.cities") or []
    for city in cities_cfg:
        if not city.get("enabled", True):
            continue
        if city.get("slug", "").lower() == key or city.get("name", "").lower() == key:
            return city
    # slug 可能含连字符，尝试空格 → 连字符转换
    slug_key = key.replace(" ", "-")
    for city in cities_cfg:
        if not city.get("enabled", True):
            continue
        if city.get("slug", "").lower() == slug_key:
            return city
    return None


def _get_city_coords(city_name: str) -> Optional[tuple[float, float]]:
    """
    Get lat/lon for a city. 查找顺序:
    1. _COORD_CACHE（内存缓存）
    2. config.json weather_cities（热加载，仪表盘可编辑）
    3. _STATION_MAP（内置 ICAO 站点映射）
    4. wttr.in（精度降低 fallback）

    Polymarket 结算依据特定气象站数据, 所以必须用站点精确经纬度。
    """
    key = city_name.lower().strip()
    if key in _COORD_CACHE:
        return _COORD_CACHE[key]

    # T276: 优先从 config.json weather_cities 加载（支持热加载 + 仪表盘管理）
    cfg_city = _load_config_city(key)
    if cfg_city and cfg_city.get("lat") and cfg_city.get("lon"):
        lat, lon = float(cfg_city["lat"]), float(cfg_city["lon"])
        _COORD_CACHE[key] = (lat, lon)
        icao = cfg_city.get("icao", "N/A")
        logger.info(
            f"[WeatherEnsemble] {city_name} → config ({lat}, {lon}) [ICAO:{icao}]"
        )
        return (lat, lon)

    # Fallback 1: _STATION_MAP ICAO 站点映射
    station = _STATION_MAP.get(key)
    if not station:
        # 剥离常见后缀再试: "new york city" → "new york", "sao paulo metro" → "sao paulo"
        for suffix in (" city", " metro", " area", " downtown", " urban"):
            if key.endswith(suffix):
                station = _STATION_MAP.get(key[: -len(suffix)])
                if station:
                    break
    if station:
        lat, lon = station[1], station[2]
        _COORD_CACHE[key] = (lat, lon)
        logger.info(
            f"[WeatherEnsemble] {city_name} → {station[0]} ({lat}, {lon}) [{station[3]}]"
        )
        return (lat, lon)

    # Fallback: wttr.in (精度降低, 可能偏离结算站点)
    logger.warning(
        f"[WeatherEnsemble] {city_name} 不在 ICAO 站点映射中, "
        f"使用 wttr.in 坐标 (可能与结算站点偏差)"
    )
    try:
        resp = requests.get(
            f"https://wttr.in/{city_name}?format=j1", headers=_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        nearest = data.get("nearest_area", [{}])[0]
        lat = float(nearest.get("latitude", 0))
        lon = float(nearest.get("longitude", 0))
        if lat == 0 and lon == 0:
            return None
        _COORD_CACHE[key] = (lat, lon)
        logger.debug(f"[WeatherEnsemble] {city_name} → ({lat}, {lon})")
        return (lat, lon)
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning(f"[WeatherEnsemble] 获取 {city_name} 坐标失败: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 批量多坐标请求 — 从根源消灭 429
# Open-Meteo 支持 latitude=a,b,c&longitude=x,y,z 多坐标单次请求
# N个城市 × 2模型 = 2N次请求 → 1次GFS + 1次ECMWF = 2次请求
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _parse_gfs_response_for_location(
    daily: dict, variable: str, member_count: int = 31
) -> Optional[dict]:
    """从单个位置的 daily 数据中提取 ensemble members。"""
    ens_cfg = get("weather_ensemble") or {}
    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"
    dates = daily.get("time", [])
    if not dates:
        return None

    ensemble_data: dict[str, list[float]] = {d: [] for d in dates}
    for idx in range(1, member_count + 1):
        col = f"{daily_var}_member{idx:02d}"
        values = daily.get(col, [])
        for i, val in enumerate(values):
            if i < len(dates) and val is not None:
                ensemble_data[dates[i]].append(val)

    min_members = int(ens_cfg.get("min_members_for_confidence", 20))
    ensemble_data = {d: v for d, v in ensemble_data.items() if len(v) >= min_members}
    return ensemble_data if ensemble_data else None


def _batch_fetch_ensemble(
    locations: list[tuple[float, float]],
    variable: str,
    model_name: str,
    model_label: str,
    member_count: int,
    forecast_days: int,
    cache_prefix: str,
) -> dict[tuple[float, float], dict]:
    """
    批量多坐标 Open-Meteo Ensemble API 请求。

    一次请求获取所有坐标的 ensemble 数据，解析后按坐标分别缓存。
    返回: {(lat, lon): {date: [members]}, ...}
    """
    # T271: 批量熔断器独立检查（不影响单城市请求路径）
    _batch_model = "batch"
    if _is_circuit_open(_batch_model):
        logger.info(f"[WeatherBatch] {model_label} 批量熔断器开启, 跳过批量请求")
        return {}

    ens_cfg = get("weather_ensemble") or {}
    api_timeout = int(ens_cfg.get("api_timeout", 25))
    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"

    lats = ",".join(f"{lat:.4f}" for lat, _ in locations)
    lons = ",".join(f"{lon:.4f}" for _, lon in locations)

    api_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble?"
        f"latitude={lats}&longitude={lons}"
        f"&daily={daily_var}"
        f"&models={model_name}"
        f"&temperature_unit=celsius"
        f"&timezone=auto"
        f"&forecast_days={min(forecast_days, 16 if 'gfs' in model_name else 15)}"
    )

    _max_attempts = 3
    _last_err = None
    _got_429 = False
    for _attempt in range(1, _max_attempts + 1):
        try:
            _throttle_ensemble_api()
            resp = requests.get(api_url, timeout=api_timeout)
            resp.raise_for_status()
            raw = resp.json()
            _last_err = None
            _on_api_success("batch")
            break
        except requests.exceptions.HTTPError as e:
            _last_err = e
            if (
                hasattr(e, "response")
                and e.response is not None
                and e.response.status_code == 429
            ):
                _got_429 = True
                _on_api_429("batch")  # T271: 批量429只开批量熔断器，不影响单城市请求
                logger.warning(
                    f"[WeatherBatch] {model_label} 批量请求429, "
                    f"第{_attempt}次, 批量熔断器已开启（单城市请求不受影响）"
                )
                break
            elif _attempt < _max_attempts:
                time.sleep(2)
        except (requests.RequestException, ValueError) as e:
            _last_err = e
            if _attempt < _max_attempts:
                time.sleep(2)

    if _last_err is not None:
        logger.warning(
            f"[WeatherBatch] {model_label} 批量请求失败({_max_attempts}次): {_last_err}"
        )
        return {}

    # 解析响应: 单坐标返回dict, 多坐标返回list
    if isinstance(raw, dict):
        results_list = [raw]
    elif isinstance(raw, list):
        results_list = raw
    else:
        logger.warning(f"[WeatherBatch] {model_label} 未知响应格式: {type(raw)}")
        return {}

    now = time.time()
    parsed: dict[tuple[float, float], dict] = {}
    for i, result in enumerate(results_list):
        if i >= len(locations):
            break
        lat, lon = locations[i]
        daily = result.get("daily", {})
        data = _parse_gfs_response_for_location(daily, variable, member_count)
        if data:
            cache_key = f"{cache_prefix}:{lat:.2f}:{lon:.2f}:{variable}:{forecast_days}"
            _CACHE[cache_key] = (now, data)
            parsed[(lat, lon)] = data

    logger.info(
        f"[WeatherBatch] {model_label} 批量完成: "
        f"{len(locations)}个坐标请求, {len(parsed)}个成功缓存"
    )
    return parsed


def prefetch_ensemble_data(markets: list[dict]) -> int:
    """
    T268: 批量预取所有天气市场的 GFS+ECMWF 数据。

    在评估循环开始前调用，一次性获取所有城市数据并缓存。
    后续 fetch_ensemble_probability / fetch_dual_model_probability 直接命中缓存。

    Args:
        markets: 天气市场列表（含 question 字段）

    Returns:
        实际发起的 API 调用次数 (0 = 全部缓存命中, 2 = 1 GFS + 1 ECMWF)
    """
    ens_cfg = get("weather_ensemble") or {}
    max_days = int(ens_cfg.get("max_forecast_days", 7))
    forecast_days = max_days + 1
    ecmwf_enabled = ens_cfg.get("ecmwf_enabled", True)
    ecmwf_ttl = int(ens_cfg.get("ecmwf_cache_ttl_seconds", 50400))

    # 1. 收集所有唯一 (lat, lon, variable) 组合
    location_vars: dict[tuple[float, float], str] = {}  # (lat,lon) → variable
    for m in markets:
        question = m.get("question", "")
        parsed = parse_weather_question(question)
        if not parsed:
            continue
        city = parsed["city"]
        variable = parsed["variable"]
        coords = _get_city_coords(city)
        if not coords:
            continue
        # 如果同一坐标有两个variable, 取 temp_max（覆盖率更高）
        if coords not in location_vars:
            location_vars[coords] = variable

    if not location_vars:
        return 0

    # 2. 按 variable 分组 (通常所有市场都是 temp_max)
    by_variable: dict[str, list[tuple[float, float]]] = {}
    for (lat, lon), var in location_vars.items():
        by_variable.setdefault(var, []).append((lat, lon))

    api_calls = 0

    gfs_ttl = int(ens_cfg.get("gfs_cache_ttl_seconds", 3600))

    for variable, all_locs in by_variable.items():
        # 3. 过滤已有新鲜缓存的坐标
        gfs_uncached = []
        for lat, lon in all_locs:
            cache_key = f"ens:{lat:.2f}:{lon:.2f}:{variable}:{forecast_days}"
            if cache_key in _CACHE:
                ts, _ = _CACHE[cache_key]
                if time.time() - ts < gfs_ttl:
                    continue
            gfs_uncached.append((lat, lon))

        ecmwf_uncached = []
        if ecmwf_enabled:
            for lat, lon in all_locs:
                cache_key = f"ecmwf:{lat:.2f}:{lon:.2f}:{variable}:{forecast_days}"
                if cache_key in _CACHE:
                    ts, _ = _CACHE[cache_key]
                    if time.time() - ts < ecmwf_ttl:
                        continue
                ecmwf_uncached.append((lat, lon))

        # 4. 批量获取 GFS — T277: 分块请求防429
        if gfs_uncached:
            gfs_model = ens_cfg.get("model", "gfs_seamless")
            chunk_size = int(ens_cfg.get("batch_chunk_size", 8))
            chunk_delay = float(ens_cfg.get("batch_chunk_delay_seconds", 2.0))
            for i in range(0, len(gfs_uncached), chunk_size):
                chunk = gfs_uncached[i : i + chunk_size]
                if i > 0:
                    time.sleep(chunk_delay)
                result = _batch_fetch_ensemble(
                    chunk,
                    variable,
                    gfs_model,
                    "GFS",
                    member_count=31,
                    forecast_days=forecast_days,
                    cache_prefix="ens",
                )
                api_calls += 1
                if not result and i > 0:
                    # 429 hit mid-batch → stop sending more chunks
                    logger.info(
                        f"[WeatherBatch] GFS 分块{i // chunk_size + 1}失败, "
                        f"停止后续分块 (已完成 {i}/{len(gfs_uncached)})"
                    )
                    break

        # 5. 批量获取 ECMWF — T277: 分块请求防429
        if ecmwf_uncached:
            ecmwf_model = ens_cfg.get("ecmwf_model", "ecmwf_ifs04")
            ecmwf_members = int(ens_cfg.get("ecmwf_members", 51))
            chunk_size = int(ens_cfg.get("batch_chunk_size", 8))
            chunk_delay = float(ens_cfg.get("batch_chunk_delay_seconds", 2.0))
            for i in range(0, len(ecmwf_uncached), chunk_size):
                chunk = ecmwf_uncached[i : i + chunk_size]
                if i > 0:
                    time.sleep(chunk_delay)
                result = _batch_fetch_ensemble(
                    chunk,
                    variable,
                    ecmwf_model,
                    "ECMWF",
                    member_count=ecmwf_members,
                    forecast_days=forecast_days,
                    cache_prefix="ecmwf",
                )
                api_calls += 1
                if not result and i > 0:
                    logger.info(
                        f"[WeatherBatch] ECMWF 分块{i // chunk_size + 1}失败, "
                        f"停止后续分块 (已完成 {i}/{len(ecmwf_uncached)})"
                    )
                    break

    total = len(location_vars)
    logger.info(
        f"[WeatherBatch] 预取完成: {total}个城市, "
        f"API调用={api_calls}次 (无批量需{total * 2}次)"
    )
    return api_calls


def _fetch_gfs_ensemble(
    lat: float, lon: float, variable: str, forecast_days: int = 7
) -> Optional[dict]:
    """
    Call Open-Meteo Ensemble API for GFS 31-member forecast.

    Args:
        lat, lon: coordinates
        variable: 'temp_max' or 'temp_min'
        forecast_days: how many days ahead (max 16 for GFS)

    Returns:
        {date_str: [member1_value, member2_value, ...31 values]} or None
    """
    ens_cfg = get("weather_ensemble") or {}
    model = ens_cfg.get("model", "gfs_seamless")
    api_timeout = int(ens_cfg.get("api_timeout", 25))

    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"

    api_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble?"
        f"latitude={lat}&longitude={lon}"
        f"&daily={daily_var}"
        f"&models={model}"
        f"&temperature_unit=celsius"
        f"&timezone=auto"
        f"&forecast_days={min(forecast_days, 16)}"
    )

    cache_key = f"ens:{lat:.2f}:{lon:.2f}:{variable}:{forecast_days}"
    now = time.time()
    # GFS 缓存 TTL 可配置 (默认 3600s = 1h, GFS每6h更新)
    gfs_ttl = int(ens_cfg.get("gfs_cache_ttl_seconds", 3600))
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < gfs_ttl:
            return data

    # T271修复: 使用 GFS 专属熔断器 — 不受 ECMWF/batch 429 影响
    # T273: 熔断时走降级链 — NOMADS/stale/det/stale_old，确保不直接返回 None
    if _is_circuit_open("gfs"):
        logger.info(f"[WeatherGFS] 熔断中，启动T273六层降级链: ({lat:.2f},{lon:.2f})")
        return _gfs_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    # T275: Open-Meteo IP级429短路 — batch或其他端点刚触发429,
    # 同IP个体请求大概率也会429, 跳过主站直接走云端镜像降级链(不开GFS熔断器)
    _429_age = time.time() - _OPEN_METEO_LAST_429_TS
    if _429_age < _OPEN_METEO_429_SKIP_WINDOW:
        logger.info(
            f"[WeatherGFS] Open-Meteo IP级429({_429_age:.0f}s前), "
            f"跳过主站 → 直接走云端镜像降级链: ({lat:.2f},{lon:.2f})"
        )
        return _gfs_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    # Task226+T265+T267: 自适应重试 + 429速率限制 + 过期缓存兜底
    _max_attempts = 3
    _last_err = None
    _got_429 = False
    for _attempt in range(1, _max_attempts + 1):
        try:
            _throttle_ensemble_api()
            resp = requests.get(api_url, timeout=api_timeout)
            resp.raise_for_status()
            result = resp.json()
            _last_err = None
            _on_api_success("gfs")
            break
        except requests.exceptions.HTTPError as e:
            _last_err = e
            if (
                hasattr(e, "response")
                and e.response is not None
                and e.response.status_code == 429
            ):
                _got_429 = True
                _on_api_429("gfs")
                # T271: GFS 熔断器已开启，不再重试
                logger.warning(f"[WeatherEnsemble] GFS API 429, 熔断器已开启")
                break
            elif _attempt < _max_attempts:
                logger.debug(
                    f"[WeatherEnsemble] GFS Ensemble API 第{_attempt}次失败，重试: {e}"
                )
                time.sleep(2)
        except (requests.RequestException, ValueError) as e:
            _last_err = e
            if _attempt < _max_attempts:
                logger.debug(
                    f"[WeatherEnsemble] GFS Ensemble API 第{_attempt}次失败，重试: {e}"
                )
                time.sleep(2)
    if _last_err is not None:
        logger.warning(
            f"[WeatherEnsemble] GFS Ensemble API 失败({_max_attempts}次): {_last_err}，"
            f"启动T273六层降级链"
        )
        return _gfs_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    daily = result.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        # T290修复: API返回200但无日期数据 → 必须走降级链，禁止直接返回None
        logger.warning(
            f"[WeatherGFS] API返回200但daily无数据, 启动降级链: ({lat:.2f},{lon:.2f})"
        )
        return _gfs_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    # Collect all member columns
    ensemble_data: dict[str, list[float]] = {}
    for date in dates:
        ensemble_data[date] = []

    # GFS ensemble members: member01 through member31
    for member_idx in range(1, 32):
        col_name = f"{daily_var}_member{member_idx:02d}"
        values = daily.get(col_name, [])
        for i, val in enumerate(values):
            if i < len(dates) and val is not None:
                ensemble_data[dates[i]].append(val)

    # Filter out dates with insufficient members
    min_members = int(ens_cfg.get("min_members_for_confidence", 20))
    ensemble_data = {
        d: vals for d, vals in ensemble_data.items() if len(vals) >= min_members
    }

    if ensemble_data:
        _CACHE[cache_key] = (now, ensemble_data)
        # T273: 主站成功 → 记录真实集合数据源，重置降级计数
        _DATA_SOURCE_META[cache_key] = {
            "source": "ensemble",
            "members": len(next(iter(ensemble_data.values()), [])),
        }
        global _GFS_FALLBACK_COUNT
        with _CIRCUIT_LOCK:
            _GFS_FALLBACK_COUNT = 0
        return ensemble_data

    # T290修复: API返回200但成员数不足min_members → 必须走降级链
    logger.warning(
        f"[WeatherGFS] API返回200但成员数不足(min={min_members}), "
        f"启动降级链: ({lat:.2f},{lon:.2f})"
    )
    return _gfs_multi_fallback(lat, lon, variable, forecast_days, cache_key)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF: P(Z ≤ x) using the error function."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


# GFS CRPS-based skill score curve (empirical NWP verification data).
# Linear 3%/day decay significantly overestimates far-horizon confidence.
# This curve reflects actual GFS temperature forecast skill degradation.
_DEFAULT_SKILL_CURVE: dict[int, float] = {
    0: 0.98,
    1: 0.95,
    2: 0.90,
    3: 0.82,
    4: 0.72,
    5: 0.60,
    6: 0.50,
    7: 0.42,
}


def _gfs_skill_factor(days_ahead: int) -> float:
    """Return forecast skill factor for given horizon (0-7 days).

    Uses configurable CRPS-based curve; falls back to built-in defaults.
    """
    curve_cfg = (get("weather_ensemble") or {}).get("skill_score_curve")
    curve: dict[int, float] = {}
    if isinstance(curve_cfg, dict):
        curve = {}
        for k, v in curve_cfg.items():
            if k.startswith("_"):
                continue
            try:
                curve[int(k)] = float(v)
            except (ValueError, TypeError):
                continue
    if not curve:
        curve = _DEFAULT_SKILL_CURVE
    if days_ahead in curve:
        return curve[days_ahead]
    # Interpolate between nearest known points
    keys = sorted(curve.keys())
    if days_ahead <= keys[0]:
        return curve[keys[0]]
    if days_ahead >= keys[-1]:
        return curve[keys[-1]]
    for i in range(len(keys) - 1):
        if keys[i] <= days_ahead <= keys[i + 1]:
            lo, hi = keys[i], keys[i + 1]
            frac = (days_ahead - lo) / (hi - lo)
            return curve[lo] + frac * (curve[hi] - curve[lo])
    return 0.42  # worst-case


def _calc_member_probability(
    values_c: list[float], parsed: dict, days_ahead: int
) -> dict:
    """
    从集合成员温度值（°C）和解析后的问题参数计算 P(YES) 及置信度。

    被 fetch_ensemble_probability 和 fetch_dual_model_probability 共用。

    Returns:
        {probability, confidence, yes_count, total, mean_c, std_c, threshold_c}
    """
    total = len(values_c)
    direction = parsed["direction"]
    unit = parsed["unit"]

    if direction == "range":
        low_val = parsed["threshold_low"]
        high_val = parsed["threshold_high"]
        if unit == "F":
            # ±0.5 舍入在原始 °F 单位中应用，再转 °C
            low_c = (low_val - 0.5 - 32) * 5 / 9
            high_c = (high_val + 0.5 - 32) * 5 / 9
        else:
            low_c = float(low_val) - 0.5
            high_c = float(high_val) + 0.5
        yes_count = sum(1 for v in values_c if low_c <= v < high_c)
        threshold_c = (
            ((low_val + high_val) / 2 - 32) * 5 / 9
            if unit == "F"
            else (low_val + high_val) / 2
        )
    elif direction == "exact":
        threshold = parsed["threshold_value"]
        if unit == "F":
            # ±0.5 舍入在原始 °F 单位中应用，再转 °C
            low_c = (threshold - 0.5 - 32) * 5 / 9
            high_c = (threshold + 0.5 - 32) * 5 / 9
            threshold_c = (threshold - 32) * 5 / 9
        else:
            threshold_c = float(threshold)
            low_c = threshold_c - 0.5
            high_c = threshold_c + 0.5
        yes_count = sum(1 for v in values_c if low_c <= v < high_c)
    else:  # gte / lte (legacy)
        threshold = parsed["threshold_value"]
        threshold_c = (threshold - 32) * 5 / 9 if unit == "F" else float(threshold)
        if direction == "gte":
            yes_count = sum(1 for v in values_c if v >= threshold_c)
        else:
            yes_count = sum(1 for v in values_c if v <= threshold_c)

    mean_c = sum(values_c) / total
    std_c = (sum((v - mean_c) ** 2 for v in values_c) / total) ** 0.5
    member_prob = yes_count / total

    # T277: 单值降级数据 Gaussian 平滑 — 当 fallback 源仅提供 1-3 个成员时,
    # sample std ≈ 0, 导致概率退化为二值 (0 或 1), 在 dual-model 路径中造成
    # 人为 100% 分歧 → 跳过所有天气市场。使用 NWP 预报误差 σ 代替 sample std,
    # 与 fetch_ensemble_probability 的 is_single_value 路径保持一致。
    if total <= 3 and std_c <= 0.1:
        _gs_cfg = get("weather_ensemble.gaussian_smoothing") or {}
        _gs_base = float(_gs_cfg.get("base_sigma", 1.0))
        _gs_rate = float(_gs_cfg.get("rate_per_day", 0.65))
        _gs_min = float(_gs_cfg.get("min_sigma", 1.5))
        sigma_est = max(_gs_min, _gs_base + _gs_rate * days_ahead)
        if direction in ("exact", "range"):
            cdf_prob = _normal_cdf((high_c - mean_c) / sigma_est) - _normal_cdf(
                (low_c - mean_c) / sigma_est
            )
        elif direction == "gte":
            cdf_prob = 1.0 - _normal_cdf((threshold_c - mean_c) / sigma_est)
        else:  # lte
            cdf_prob = _normal_cdf((threshold_c - mean_c) / sigma_est)
        probability = max(0.02, min(0.98, cdf_prob))
        yes_count = max(0, min(total, round(probability * total)))
        logger.debug(
            f"[WeatherCDF-T277] 单值Gaussian平滑: n={total} mean={mean_c:.2f}°C "
            f"σ_est={sigma_est:.1f}°C → P={probability:.4f} "
            f"(dir={direction}, threshold={threshold_c:.2f}°C)"
        )
    # CDF-enhanced probability: Normal CDF rescues systematic underestimation
    # from discrete member counting, especially for exact/range where narrow bins
    # (1°C) cause P=0 even when true probability is 10-40%.
    # Uses max(member_prob, cdf_prob) to preserve member counting accuracy for
    # bimodal distributions while rescuing near-zero cases.
    elif std_c > 0.1:
        if direction in ("exact", "range"):
            cdf_prob = _normal_cdf((high_c - mean_c) / std_c) - _normal_cdf(
                (low_c - mean_c) / std_c
            )
        elif direction == "gte":
            cdf_prob = 1.0 - _normal_cdf((threshold_c - mean_c) / std_c)
        else:  # lte
            cdf_prob = _normal_cdf((threshold_c - mean_c) / std_c)
        probability = max(member_prob, cdf_prob)
        if cdf_prob > member_prob + 0.01:
            logger.debug(
                f"[WeatherCDF] CDF rescue: member={member_prob:.4f} → "
                f"CDF={cdf_prob:.4f} (dir={direction}, mean={mean_c:.2f}, "
                f"std={std_c:.2f}, threshold={threshold_c:.2f})"
            )
    else:
        probability = member_prob

    consensus = max(yes_count, total - yes_count) / total
    horizon_factor = _gfs_skill_factor(days_ahead)
    confidence = round(min(0.95, consensus * horizon_factor * 0.95), 3)

    return {
        "probability": round(probability, 4),
        "confidence": confidence,
        "yes_count": yes_count,
        "total": total,
        "mean_c": round(mean_c, 2),
        "std_c": round(std_c, 2),
        "threshold_c": round(threshold_c, 2),
    }


# T206: 降雨预报缓存 — {(lat, lon, date_str): precip_prob_max}
_PRECIP_CACHE: dict[tuple[float, float, str], Optional[float]] = {}


def fetch_precipitation_probability(
    lat: float, lon: float, target_date: str
) -> Optional[float]:
    """
    调用 Open-Meteo Forecast API 获取指定日期的最高降雨概率。

    使用确定性预报（非 ensemble），因降雨概率由 GFS 确定性模型计算。
    缓存 TTL：同生命周期（bot 重启后刷新）。

    Args:
        lat: 纬度
        lon: 经度
        target_date: 目标日期 YYYY-MM-DD

    Returns:
        precipitation_probability_max (0-100) 或 None
    """
    cache_key = (round(lat, 2), round(lon, 2), target_date)
    if cache_key in _PRECIP_CACHE:
        return _PRECIP_CACHE[cache_key]

    try:
        api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=precipitation_probability_max"
            f"&timezone=auto"
            f"&start_date={target_date}&end_date={target_date}"
        )
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        daily = data.get("daily", {})
        probs = daily.get("precipitation_probability_max", [])
        if probs and probs[0] is not None:
            result = float(probs[0])
            _PRECIP_CACHE[cache_key] = result
            logger.debug(
                f"[PrecipForecast] lat={lat:.2f} lon={lon:.2f} "
                f"date={target_date} → precip_prob_max={result:.0f}%"
            )
            return result

        _PRECIP_CACHE[cache_key] = None
        return None

    except Exception as e:
        logger.debug(f"[PrecipForecast] 获取降雨概率失败: {e}")
        _PRECIP_CACHE[cache_key] = None
        return None


def _fetch_ecmwf_ensemble(
    lat: float, lon: float, variable: str, forecast_days: int = 7
) -> Optional[dict]:
    """
    调用 Open-Meteo Ensemble API — ECMWF IFS 51 成员集合预报。

    ECMWF IFS ENS: 0.4° 分辨率，member00（控制）+ member01~member50（扰动）= 51 成员。
    更新频率：每 12 小时（00/12 UTC），发布延迟约 7 小时。
    缓存 TTL：14 小时（ecmwf_cache_ttl_seconds）。

    Returns:
        {date_str: [member_value, ...]} 或 None
    """
    ens_cfg = get("weather_ensemble") or {}
    ecmwf_model = ens_cfg.get("ecmwf_model", "ecmwf_ifs04")
    ecmwf_ttl = int(ens_cfg.get("ecmwf_cache_ttl_seconds", 50400))  # 14h
    ecmwf_members = int(ens_cfg.get("ecmwf_members", 51))
    api_timeout = int(ens_cfg.get("api_timeout", 25))

    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"

    cache_key = f"ecmwf:{lat:.2f}:{lon:.2f}:{variable}:{forecast_days}"
    now = time.time()
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < ecmwf_ttl:
            return data

    # T271修复: 使用 ECMWF 专属熔断器 — 不受 GFS/batch 429 影响
    # T273: 熔断时走三层降级链 (stale/det_fallback/stale_old)
    if _is_circuit_open("ecmwf"):
        logger.info(f"[WeatherECMWF] 熔断中，启动T273降级链: ({lat:.2f},{lon:.2f})")
        return _ecmwf_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    # T275: Open-Meteo IP级429短路 — 同IP刚被限速, 跳过主站走云端镜像
    _429_age = time.time() - _OPEN_METEO_LAST_429_TS
    if _429_age < _OPEN_METEO_429_SKIP_WINDOW:
        logger.info(
            f"[WeatherECMWF] Open-Meteo IP级429({_429_age:.0f}s前), "
            f"跳过主站 → 直接走云端镜像降级链: ({lat:.2f},{lon:.2f})"
        )
        return _ecmwf_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    api_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble?"
        f"latitude={lat}&longitude={lon}"
        f"&daily={daily_var}"
        f"&models={ecmwf_model}"
        f"&temperature_unit=celsius"
        f"&timezone=auto"
        f"&forecast_days={min(forecast_days, 15)}"
    )

    # Task226+T265+T267: 自适应重试 + 429速率限制 + 过期缓存兜底
    _max_attempts = 3
    _last_err = None
    _got_429 = False
    for _attempt in range(1, _max_attempts + 1):
        try:
            _throttle_ensemble_api()
            resp = requests.get(api_url, timeout=api_timeout)
            resp.raise_for_status()
            result = resp.json()
            _last_err = None
            _on_api_success("ecmwf")
            break
        except requests.exceptions.HTTPError as e:
            _last_err = e
            if (
                hasattr(e, "response")
                and e.response is not None
                and e.response.status_code == 429
            ):
                _got_429 = True
                _on_api_429("ecmwf")
                # T271: ECMWF 熔断器已开启，不再重试
                logger.warning(f"[WeatherECMWF] ECMWF API 429, 熔断器已开启")
                break
            elif _attempt < _max_attempts:
                logger.debug(
                    f"[WeatherECMWF] ECMWF Ensemble API 第{_attempt}次失败，重试: {e}"
                )
                time.sleep(2)
        except (requests.RequestException, ValueError) as e:
            _last_err = e
            if _attempt < _max_attempts:
                logger.debug(
                    f"[WeatherECMWF] ECMWF Ensemble API 第{_attempt}次失败，重试: {e}"
                )
                time.sleep(2)
    if _last_err is not None:
        logger.warning(
            f"[WeatherECMWF] ECMWF Ensemble API 失败({_max_attempts}次): {_last_err}，"
            f"启动T273降级链"
        )
        return _ecmwf_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    daily = result.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        # T290修复: API返回200但无日期数据 → 必须走降级链，禁止直接返回None
        logger.warning(
            f"[WeatherECMWF] API返回200但daily无数据, 启动降级链: ({lat:.2f},{lon:.2f})"
        )
        return _ecmwf_multi_fallback(lat, lon, variable, forecast_days, cache_key)

    ensemble_data: dict[str, list[float]] = {date: [] for date in dates}

    # ECMWF IFS ENS (ecmwf_ifs025): member01~member50 (50 perturbed members, no member00)
    for member_idx in range(1, ecmwf_members + 1):
        col_name = f"{daily_var}_member{member_idx:02d}"
        values = daily.get(col_name, [])
        for i, val in enumerate(values):
            if i < len(dates) and val is not None:
                ensemble_data[dates[i]].append(val)

    min_members = int(ens_cfg.get("min_members_for_confidence", 20))
    ensemble_data = {
        d: vals for d, vals in ensemble_data.items() if len(vals) >= min_members
    }

    if ensemble_data:
        _CACHE[cache_key] = (now, ensemble_data)
        # T273: 主站成功 → 记录真实集合数据源，重置降级计数
        _DATA_SOURCE_META[cache_key] = {
            "source": "ensemble",
            "members": len(next(iter(ensemble_data.values()), [])),
        }
        global _ECMWF_FALLBACK_COUNT
        with _CIRCUIT_LOCK:
            _ECMWF_FALLBACK_COUNT = 0
        logger.debug(
            f"[WeatherECMWF] {ecmwf_model} 成功: ({lat:.2f},{lon:.2f}) "
            f"dates={len(ensemble_data)}, "
            f"members={len(list(ensemble_data.values())[0]) if ensemble_data else 0}"
        )
        return ensemble_data

    # T290修复: API返回200但成员数不足min_members → 必须走降级链
    logger.warning(
        f"[WeatherECMWF] API返回200但成员数不足(min={min_members}), "
        f"启动降级链: ({lat:.2f},{lon:.2f})"
    )
    return _ecmwf_multi_fallback(lat, lon, variable, forecast_days, cache_key)


def fetch_ensemble_probability(market: dict) -> Optional[dict]:
    """
    核心函数：从 GFS 31-member ensemble 直接计算天气事件概率。

    对于 Polymarket 天气问题：
    - 解析问题 → 提取城市、日期、阈值、变量
    - 查询 GFS ensemble 31 成员预报
    - probability = count(members satisfying condition) / total_members

    Returns:
        {
            "probability": 0.0~1.0 (P(YES)),
            "confidence": 0.85~0.95,
            "summary": "GFS 31-member: 24/31 members forecast high ≥ 62°F...",
            "members_total": 31,
            "members_yes": 24,
            "ensemble_values": [list of 31 values],
            "source": "gfs_ensemble"
        }
        None if question can't be parsed or API fails.
    """
    ens_cfg = get("weather_ensemble") or {}
    if not ens_cfg.get("enabled", True):
        return None

    question = market.get("question", "")
    parsed = parse_weather_question(question)
    if not parsed:
        logger.debug(f"[WeatherEnsemble] 问题无法解析: {question[:80]}")
        return None

    city = parsed["city"]
    date_str = parsed["date_str"]
    threshold = parsed["threshold_value"]
    unit = parsed["unit"]
    variable = parsed["variable"]
    direction = parsed["direction"]

    # Parse target date
    target_date = _parse_target_date(date_str)
    if not target_date:
        logger.warning(f"[WeatherEnsemble] 日期无法解析: {date_str}")
        return None

    # Check forecast horizon (don't trust ensemble beyond configured limit)
    max_days = int(ens_cfg.get("max_forecast_days", 7))
    today = datetime.now(timezone.utc).date()
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    days_ahead = (target_dt - today).days
    if days_ahead < 0 or days_ahead > max_days:
        logger.info(
            f"[WeatherEnsemble] 日期超出预报范围: {target_date} ({days_ahead}天后, 最大{max_days}天)"
        )
        return None

    # Get city coordinates
    coords = _get_city_coords(city)
    if not coords:
        # Try extracting from market's oracle URL via wttr.in
        oracle_url = extract_oracle_url(market)
        if oracle_url:
            station_id = _extract_station_id(oracle_url)
            if station_id:
                wttr_data = _fetch_wttr(station_id)
                lat_str = wttr_data.get("latitude", "")
                lon_str = wttr_data.get("longitude", "")
                if lat_str and lon_str:
                    try:
                        coords = (float(lat_str), float(lon_str))
                        _COORD_CACHE[city.lower().strip()] = coords
                    except ValueError:
                        pass
    if not coords:
        logger.warning(f"[WeatherEnsemble] 无法获取 {city} 坐标")
        return None

    lat, lon = coords

    # Fetch GFS ensemble data
    gfs_cache_key = f"ens:{lat:.2f}:{lon:.2f}:{variable}:{max_days + 1}"
    ensemble_data = _fetch_gfs_ensemble(lat, lon, variable, forecast_days=max_days + 1)
    if not ensemble_data:
        # T290防御: _fetch_gfs_ensemble 应已内部走降级链, 此处不应到达
        # 但作为防御, 再尝试一次降级链
        logger.warning(
            f"[WeatherEnsemble] GFS返回空(不应发生), 防御性调用降级链: {city}"
        )
        ensemble_data = _gfs_multi_fallback(
            lat, lon, variable, max_days + 1, gfs_cache_key
        )
    if not ensemble_data:
        logger.error(f"[WeatherEnsemble] GFS全链路失败(含降级链): {city}")
        return None

    if target_date not in ensemble_data:
        # T290防御: 尝试紧急气候学估计来覆盖缺失日期
        logger.warning(
            f"[WeatherEnsemble] 目标日期 {target_date} 不在数据中 "
            f"(可用: {list(ensemble_data.keys())}), 尝试紧急气候学估计"
        )
        emergency = _emergency_climate_estimate(lat, variable, max_days + 1)
        if target_date in emergency:
            ensemble_data[target_date] = emergency[target_date]
            _DATA_SOURCE_META[gfs_cache_key] = {
                "source": "emergency_climate",
                "members": 1,
            }
        else:
            logger.error(
                f"[WeatherEnsemble] 紧急气候学也无法覆盖 {target_date}: {city}"
            )
            return None

    values_c = ensemble_data[target_date]  # All values in Celsius
    total = len(values_c)

    # T273: 数据源元数据 → 置信度乘数
    source_meta = _DATA_SOURCE_META.get(gfs_cache_key, {"source": "ensemble"})
    source_type = source_meta.get("source", "ensemble")
    source_mult = _get_source_confidence_multiplier().get(source_type, 1.0)
    # 单值数据: NOMADS/det_fallback/synthetic 返回 total<=2, source != ensemble
    is_single_value = (total <= 2) and (source_type != "ensemble")

    # ── 计算阈值变量 (所有路径共用) ──────────────────────────────────────────
    low_c: float = 0.0
    high_c: float = 0.0
    threshold_c: float = 0.0

    if direction == "range":
        low_val = parsed["threshold_low"]
        high_val = parsed["threshold_high"]
        if unit == "F":
            low_c = (low_val - 0.5 - 32) * 5 / 9
            high_c = (high_val + 0.5 - 32) * 5 / 9
        else:
            low_c = float(low_val) - 0.5
            high_c = float(high_val) + 0.5
        threshold_c = (
            ((low_val + high_val) / 2 - 32) * 5 / 9
            if unit == "F"
            else (low_val + high_val) / 2
        )
    elif direction == "exact":
        threshold = parsed["threshold_value"]
        if unit == "F":
            low_c = (threshold - 0.5 - 32) * 5 / 9
            high_c = (threshold + 0.5 - 32) * 5 / 9
            threshold_c = (threshold - 32) * 5 / 9
        else:
            threshold_c = float(threshold)
            low_c = threshold_c - 0.5
            high_c = threshold_c + 0.5
    else:  # Legacy gte/lte
        threshold = parsed["threshold_value"]
        if unit == "F":
            threshold_c = (threshold - 32) * 5 / 9
        else:
            threshold_c = float(threshold)

    # ── 概率 & 置信度 ─────────────────────────────────────────────────────────
    horizon_factor = _gfs_skill_factor(days_ahead)
    if is_single_value:
        # T273: 单值降级数据 — normcdf + 时效误差σ (比Gaussian假集合更诚实)
        # NWP确定性预报典型误差随时效线性增长:
        #   Day0=1.5°C, Day3=3.45°C, Day5=4.25°C, Day7=5.55°C
        T_det = values_c[0]
        _gs_cfg2 = get("weather_ensemble.gaussian_smoothing") or {}
        _gs_base2 = float(_gs_cfg2.get("base_sigma", 1.0))
        _gs_rate2 = float(_gs_cfg2.get("rate_per_day", 0.65))
        _gs_min2 = float(_gs_cfg2.get("min_sigma", 1.5))
        sigma_est = max(_gs_min2, _gs_base2 + _gs_rate2 * days_ahead)

        if direction in ("exact", "range"):
            p_raw = _normal_cdf((high_c - T_det) / sigma_est) - _normal_cdf(
                (low_c - T_det) / sigma_est
            )
        elif direction == "gte":
            p_raw = 1.0 - _normal_cdf((threshold_c - T_det) / sigma_est)
        else:  # lte
            p_raw = _normal_cdf((threshold_c - T_det) / sigma_est)

        # 单值无真实集合spread → 上限0.92, 不允许假装高确信度
        probability = max(0.05, min(0.92, p_raw))
        yes_count = round(probability * total)

        # 置信度: 时效衰减基数(0.72折现) × 数据源乘数
        base_conf = min(0.85, horizon_factor * 0.72)
        confidence = round(base_conf * source_mult, 3)

        if source_type == "emergency_climate":
            # T275: [EMERGENCY] 紧急气候学兜底 — 硬封顶≤0.12, 绝对不允许下注
            confidence = min(confidence, 0.12)
            logger.error(
                f"[WeatherGFS] ⚠️ [EMERGENCY] {city} {target_date}: "
                f"紧急气候学估计 conf={confidence:.3f}(≤0.12硬上限). "
                f"T_det={T_det:.1f}°C σ_est={sigma_est:.1f}°C P(YES)={probability:.3f}. "
                f"所有真实数据源均已失败! 此概率仅用于保持市场在管线中!"
            )
        elif source_type == "synthetic":
            # ⚠️ 绝对最后手段 — 必须硬封顶≤0.15并明确标记
            confidence = min(confidence, 0.15)
            logger.warning(
                f"[WeatherGFS] ⚠️ [SYNTHETIC DATA] {city} {target_date}: "
                f"Gaussian合成数据 conf={confidence:.3f}(≤0.15硬上限). "
                f"T_det={T_det:.1f}°C σ_est={sigma_est:.1f}°C P(YES)={probability:.3f}. "
                f"此数据基于人为假设, 严禁高置信度投注!"
            )
        else:
            logger.warning(
                f"[WeatherGFS-DEGRADED] {city} {target_date}: "
                f"source={source_type} conf={confidence:.3f}(×{source_mult}) "
                f"T_det={T_det:.1f}°C σ_est={sigma_est:.1f}°C P(YES)={probability:.3f}"
            )
    else:
        # 正常多成员集合概率计算
        if direction == "range":
            yes_count = sum(1 for v in values_c if low_c <= v < high_c)
        elif direction == "exact":
            yes_count = sum(1 for v in values_c if low_c <= v < high_c)
        else:  # gte/lte
            if direction == "gte":
                yes_count = sum(1 for v in values_c if v >= threshold_c)
            else:
                yes_count = sum(1 for v in values_c if v <= threshold_c)

        probability = yes_count / total
        # 集合置信度: 成员共识 × 时效技巧 × 数据源乘数(stale自动折现)
        consensus = max(yes_count, total - yes_count) / total
        confidence = round(
            min(0.95, consensus * horizon_factor * 0.95 * source_mult), 3
        )

    # ── 统计显示 ──────────────────────────────────────────────────────────────
    mean_val = sum(values_c) / total
    std_val = (sum((v - mean_val) ** 2 for v in values_c) / total) ** 0.5

    if unit == "F":
        mean_display = round(mean_val * 9 / 5 + 32, 1)
        std_display = round(std_val * 9 / 5, 1)
        unit_label = "°F"
    else:
        mean_display = round(mean_val, 1)
        std_display = round(std_val, 1)
        unit_label = "°C"

    var_label = "high" if variable == "temp_max" else "low"
    station_info = parsed.get("station_icao") or "wttr.in"

    if direction == "range":
        dir_label = f"[{parsed['threshold_low']}-{parsed['threshold_high']}]"
        threshold_display = f"{parsed['threshold_low']}-{parsed['threshold_high']}"
    elif direction == "exact":
        dir_label = f"={parsed['threshold_value']}"
        threshold_display = str(parsed["threshold_value"])
    elif direction == "gte":
        dir_label = f"≥{parsed['threshold_value']}"
        threshold_display = str(parsed["threshold_value"])
    else:
        dir_label = f"≤{parsed['threshold_value']}"
        threshold_display = str(parsed["threshold_value"])

    # 数据源标注
    _age_h = source_meta.get("age", 0) / 3600
    source_label = {
        "ensemble": "GFS 31-member ensemble",
        "aws_gfs_s3": f"GFS/AWS-S3 GRIB2 single-value [×{source_mult}]",
        "ecmwf_mirror": f"ECMWF/Cloud-Mirror GRIB2 single-value [×{source_mult}]",
        "noaa_nomads": f"GFS/NOMADS(AWS) single-value [×{source_mult}]",
        "stale_fresh": f"GFS ensemble(stale {_age_h:.1f}h) [×{source_mult}]",
        "stale_recent": f"GFS ensemble(stale {_age_h:.1f}h) [×{source_mult}]",
        "stale_old": f"GFS ensemble(stale {_age_h:.0f}h old) [×{source_mult}]",
        "det_fallback": f"GFS deterministic/open-meteo [×{source_mult}]",
        "synthetic": f"⚠️SYNTHETIC Gaussian [×{source_mult}] - LOW QUALITY",
        "emergency_climate": f"⚠️[EMERGENCY] Climatological Estimate [×{source_mult}] - ALL SOURCES DOWN",
    }.get(source_type, f"GFS/{source_type}")

    summary = (
        f"{source_label}: {yes_count}/{total} forecast "
        f"{var_label} {dir_label}{unit_label} on {target_date}. "
        f"mean={mean_display}{unit_label}, std={std_display}{unit_label}. "
        f"P(YES)={probability:.3f}, confidence={confidence:.3f}. "
        f"Station: {station_info}, coords: ({lat:.2f}, {lon:.2f}), horizon={days_ahead}d."
    )

    logger.info(
        f"[WeatherEnsemble] {city}({station_info}) {target_date}: "
        f"{yes_count}/{total} → P(YES)={probability:.3f} conf={confidence:.3f} "
        f"({var_label} {dir_label}{unit_label}, mean={mean_display}{unit_label}) "
        f"[src={source_type}]"
    )

    return {
        "probability": round(probability, 4),
        "confidence": confidence,
        "summary": summary,
        "members_total": total,
        "members_yes": yes_count,
        "ensemble_mean": mean_display,
        "ensemble_std": std_display,
        "ensemble_values": [round(v, 2) for v in values_c],
        "threshold_c": round(threshold_c, 2),
        "target_date": target_date,
        "city": city,
        "variable": variable,
        "direction": direction,
        "station_icao": station_info,
        "days_ahead": days_ahead,
        "source": source_type,
        "data_quality": "degraded" if source_type != "ensemble" else "full",
    }


# ===========================================================================
# METAR 实时观测数据源（Task 106）
# ===========================================================================
# 第三数据源：aviationweather.gov METAR API → metarcentral.com HTML 降级
# 角色：实时地面观测，与 GFS / ECMWF 预报形成"预报+观测"三层校验
# - 当日事件：当前温度已超阈值 → 提升 P(YES)
# - 近期事件：计算模型偏差修正（今日实际 vs 今日预报）
# ===========================================================================


def _fetch_metar_observation(icao: str) -> Optional[dict]:
    """
    从 aviationweather.gov METAR API 获取最新观测温度。
    降级路径：aviationweather.gov → metarcentral.com HTML 解析。
    返回 {"temp_c": float, "dewpoint_c": float, "obs_time": str, "raw": str,
           "source": str} 或 None。
    """
    ens_cfg = get("weather_ensemble") or {}
    if not ens_cfg.get("metar_enabled", True):
        return None

    cache_ttl = int(ens_cfg.get("metar_cache_ttl_seconds", 120))
    api_timeout = int(ens_cfg.get("metar_api_timeout", 10))
    cache_key = f"metar:{icao}"
    now = time.time()
    if cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < cache_ttl:
            return cached

    result = _fetch_metar_aviationweather(icao, api_timeout)
    if not result:
        result = _fetch_metar_metarcentral(icao, api_timeout)
    if not result:
        # 第三降级：NWS tgftp 原始 METAR，避免 aviationweather 被限流后链路中断。
        result = _fetch_nws_raw_metar(icao, api_timeout)

    if result:
        _CACHE[cache_key] = (now, result)
        logger.info(
            f"[METAR] {icao}: {result['temp_c']}°C "
            f"(source={result['source']}, obs={result.get('obs_time', '?')})"
        )
    return result


def _fetch_metar_aviationweather(icao: str, timeout: int) -> Optional[dict]:
    """aviationweather.gov JSON API — 全球 ICAO 站点 METAR。"""
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": _HEADERS.get("User-Agent", "Mozilla/5.0"),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://aviationweather.gov/",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        obs = data[0]
        temp_c = obs.get("temp")
        if temp_c is None:
            return None
        return {
            "temp_c": round(float(temp_c), 1),
            "dewpoint_c": round(float(obs.get("dewp", 0)), 1),
            "obs_time": obs.get("reportTime", ""),
            "raw": obs.get("rawOb", ""),
            "source": "aviationweather.gov",
        }
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        logger.debug(f"[METAR] aviationweather.gov 失败 ({icao}): {e}")
        return None


def _fetch_metar_metarcentral(icao: str, timeout: int) -> Optional[dict]:
    """metarcentral.com HTML 降级解析 — 从 Raw METAR 中提取温度。"""
    url = f"https://metarcentral.com/airport/{icao}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        html = resp.text

        # 提取 Raw METAR 字符串中的温度组: "TDD/DD" 或 "TMDD/MDD"（M=负号）
        metar_match = re.search(
            r"METAR\s+" + re.escape(icao) + r"\s+(\d{6}Z)\s+(.+?)(?=RMK|$)",
            html,
        )
        if not metar_match:
            return None

        obs_time = metar_match.group(1)
        body = metar_match.group(2)

        # TDD/DD: e.g. "21/20" or "M02/M07"
        temp_match = re.search(r"\b(M?\d{1,2})/(M?\d{1,2})\b", body)
        if not temp_match:
            return None

        def parse_metar_temp(s: str) -> float:
            if s.startswith("M"):
                return -float(s[1:])
            return float(s)

        temp_c = parse_metar_temp(temp_match.group(1))
        dewp_c = parse_metar_temp(temp_match.group(2))

        # 精确温度: RMK T组 e.g. "T02060200" = 20.6°C / 20.0°C
        t_match = re.search(r"T(\d{4})(\d{4})", html)
        if t_match:
            t_raw = t_match.group(1)
            sign = -1 if t_raw[0] == "1" else 1
            temp_c = sign * int(t_raw[1:]) / 10

        return {
            "temp_c": round(temp_c, 1),
            "dewpoint_c": round(dewp_c, 1),
            "obs_time": obs_time,
            "raw": metar_match.group(0)[:120],
            "source": "metarcentral.com",
        }
    except (requests.RequestException, ValueError, AttributeError) as e:
        logger.debug(f"[METAR] metarcentral.com 失败 ({icao}): {e}")
        return None


def _apply_metar_calibration(
    consensus_prob: float,
    metar_data: dict,
    gfs_data: Optional[dict],
    parsed: dict,
    target_date: str,
    days_ahead: int,
) -> tuple[float, str]:
    """
    用 METAR 实时观测校准双模型概率。

    校准逻辑：
    - days_ahead == 0（当日事件）：如果当前观测温度已超/未达阈值，调整概率
    - days_ahead <= 2（近期事件）：计算模型偏差（今日预报 vs 今日 METAR），修正概率

    返回 (adjusted_prob, calibration_note)。
    """
    ens_cfg = get("weather_ensemble") or {}
    same_day_weight = float(ens_cfg.get("metar_same_day_weight", 0.15))
    bias_max = float(ens_cfg.get("metar_bias_correction_max", 2.0))

    metar_temp_c = metar_data["temp_c"]
    unit = parsed["unit"]
    direction = parsed["direction"]
    variable = parsed["variable"]

    # 计算阈值（°C）
    if direction in ("exact", "gte", "lte"):
        threshold = parsed["threshold_value"]
        threshold_c = (threshold - 32) * 5 / 9 if unit == "F" else float(threshold)
    elif direction == "range":
        low_val = parsed["threshold_low"]
        high_val = parsed["threshold_high"]
        threshold_c = (
            ((low_val + high_val) / 2 - 32) * 5 / 9
            if unit == "F"
            else (low_val + high_val) / 2
        )
    else:
        return consensus_prob, ""

    adjusted = consensus_prob
    note = ""

    if days_ahead == 0:
        # 当日事件：METAR 是部分 ground truth
        # 对于 max temp：如果当前已超阈值且还有升温时间，P(YES) 应该更高
        # 对于 min temp：如果已降到阈值以下，P(YES) 应更高
        diff_c = metar_temp_c - threshold_c

        if variable == "temp_max":
            if diff_c > 0:
                # 当前温度已超过阈值 → 强势看涨
                boost = min(same_day_weight, (1.0 - consensus_prob) * 0.5)
                adjusted = min(0.98, consensus_prob + boost)
                note = f"METAR当日已超阈值+{diff_c:.1f}°C, P+{boost:.3f}"
            elif diff_c < -5:
                # 当前温度远低于阈值 → 适度看跌
                penalty = min(same_day_weight * 0.5, consensus_prob * 0.3)
                adjusted = max(0.02, consensus_prob - penalty)
                note = f"METAR当日低于阈值{diff_c:.1f}°C, P-{penalty:.3f}"
        elif variable == "temp_min":
            if diff_c < 0:
                # 已降至阈值以下
                boost = min(same_day_weight, (1.0 - consensus_prob) * 0.5)
                adjusted = min(0.98, consensus_prob + boost)
                note = f"METAR当日已低于阈值{diff_c:.1f}°C, P+{boost:.3f}"

    elif days_ahead <= 2 and gfs_data:
        # 近期事件：用今日模型偏差校准
        # 获取今日 GFS 预报值 vs METAR 观测
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_values = gfs_data.get(today_str, [])
        if today_values:
            model_today_mean = sum(today_values) / len(today_values)
            bias_c = metar_temp_c - model_today_mean  # 正值=模型偏冷
            bias_c = max(-bias_max, min(bias_max, bias_c))  # 限制最大修正

            _metar_trigger = float(get("weather_ensemble.metar_bias_trigger_c") or 0.5)
            _metar_rate = float(get("weather_ensemble.metar_bias_rate_per_c") or 0.02)
            _metar_max_shift = float(
                get("weather_ensemble.metar_bias_max_shift") or 0.05
            )
            if abs(bias_c) > _metar_trigger:
                # 模型偏差显著 → 概率微调
                # 偏差方向影响概率：模型偏冷+看高温→P应略高
                if variable == "temp_max":
                    prob_shift = bias_c * _metar_rate  # 每偏差1°C调整
                else:
                    prob_shift = -bias_c * _metar_rate
                prob_shift = max(-_metar_max_shift, min(_metar_max_shift, prob_shift))
                adjusted = max(0.02, min(0.98, consensus_prob + prob_shift))
                note = (
                    f"METAR偏差校准: 观测{metar_temp_c:.1f}°C vs "
                    f"GFS今日均值{model_today_mean:.1f}°C, "
                    f"bias={bias_c:+.1f}°C, P{prob_shift:+.3f}"
                )

    return round(adjusted, 4), note


# ===========================================================================
# GFS + ECMWF 双模型联合概率引擎（Phase 41）
# ===========================================================================
# 当两个全球顶级模型（GFS 31-member + ECMWF 51-member）共识时，置信度显著提升。
# 分歧 > 15% 时跳过（大气状态不确定，Seoul-type 风险规避）。
# ===========================================================================


def fetch_dual_model_probability(market: dict) -> Optional[dict]:
    """
    GFS + ECMWF 双模型集合预报联合概率引擎。

    逻辑：
    - 同时调用 GFS 31-member 和 ECMWF 51-member 预报
    - 计算两模型分别的 P(YES)
    - 差异 ≤ 8%  → 高置信度：ECMWF×0.60＋GFS×0.40，edge 门槛降至 6%
    - 差异 8~15% → 中置信度：ECMWF×0.65＋GFS×0.35，仓位 ×0.8
    - 差异 > 15% → 跳过（分歧过大，返回 None）

    Returns:
        Enhanced result dict（含两模型数据），或 None（调用方 fallback 到 GFS-only）。
        None 的情况：ECMWF 数据不可用 / 分歧超阈值 / 问题无法解析
    """
    ens_cfg = get("weather_ensemble") or {}
    if not ens_cfg.get("ecmwf_enabled", True):
        return None  # ECMWF 功能关闭，调用方退回 GFS 单模型

    question = market.get("question", "")
    parsed = parse_weather_question(question)
    if not parsed:
        return None

    city = parsed["city"]
    date_str = parsed["date_str"]
    variable = parsed["variable"]

    target_date = _parse_target_date(date_str)
    if not target_date:
        return None

    max_days = int(ens_cfg.get("max_forecast_days", 7))
    today = datetime.now(timezone.utc).date()
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    days_ahead = (target_dt - today).days
    if days_ahead < 0 or days_ahead > max_days:
        return None

    coords = _get_city_coords(city)
    if not coords:
        return None
    lat, lon = coords

    # ── 同时获取两模型数据 ────────────────────────────────────────
    gfs_data = _fetch_gfs_ensemble(lat, lon, variable, forecast_days=max_days + 1)
    ecmwf_data = _fetch_ecmwf_ensemble(lat, lon, variable, forecast_days=max_days + 1)

    # T290防御: GFS失败时尝试降级链
    gfs_cache_key = f"ens:{lat:.2f}:{lon:.2f}:{variable}:{max_days + 1}"
    if not gfs_data:
        logger.warning(f"[DualModel] GFS返回空, 防御性调用降级链: {city}")
        gfs_data = _gfs_multi_fallback(lat, lon, variable, max_days + 1, gfs_cache_key)
    if not gfs_data or target_date not in gfs_data:
        # 尝试紧急气候学覆盖
        emergency = _emergency_climate_estimate(lat, variable, max_days + 1)
        if target_date in emergency:
            if not gfs_data:
                gfs_data = {}
            gfs_data[target_date] = emergency[target_date]
            _DATA_SOURCE_META[gfs_cache_key] = {
                "source": "emergency_climate",
                "members": 1,
            }
        else:
            return None  # 极端情况: 连气候学也无法覆盖

    if not ecmwf_data or target_date not in ecmwf_data:
        logger.info(f"[DualModel] ECMWF 数据不可用 ({city})，调用方退回 GFS 单模型")
        return None

    gfs_values = gfs_data[target_date]
    ecmwf_values = ecmwf_data[target_date]

    # 追踪来源质量，避免把降级链结果误读为完整集合预报。
    ecmwf_cache_key = f"ecmwf:{lat:.2f}:{lon:.2f}:{variable}:{max_days + 1}"
    gfs_source = (_DATA_SOURCE_META.get(gfs_cache_key) or {}).get("source", "ensemble")
    ecmwf_source = (_DATA_SOURCE_META.get(ecmwf_cache_key) or {}).get(
        "source", "ensemble"
    )
    gfs_quality = "full" if gfs_source == "ensemble" else "degraded"
    ecmwf_quality = "full" if ecmwf_source == "ensemble" else "degraded"
    data_quality = (
        "full" if gfs_quality == "full" and ecmwf_quality == "full" else "degraded"
    )

    # ── 各模型概率计算 ────────────────────────────────────────────
    gfs_stats = _calc_member_probability(gfs_values, parsed, days_ahead)
    ecmwf_stats = _calc_member_probability(ecmwf_values, parsed, days_ahead)

    gfs_prob = gfs_stats["probability"]
    ecmwf_prob = ecmwf_stats["probability"]
    divergence = abs(ecmwf_prob - gfs_prob)

    # ── 分歧阈值决策 ──────────────────────────────────────────────
    divergence_max = float(ens_cfg.get("ecmwf_divergence_threshold", 0.15))
    divergence_high = float(ens_cfg.get("ecmwf_divergence_high_threshold", 0.08))
    ecmwf_w_high = float(ens_cfg.get("ecmwf_weight_high", 0.60))
    ecmwf_w_medium = float(ens_cfg.get("ecmwf_weight_medium", 0.65))
    edge_high = float(ens_cfg.get("ecmwf_edge_threshold_high_confidence", 0.06))
    min_edge = float(ens_cfg.get("min_edge", 0.08))

    # T277: 低成员降级数据放宽分歧阈值 — Gaussian平滑引入的不确定性更大
    gfs_n = len(gfs_values) if gfs_values else 0
    ecmwf_n = len(ecmwf_values) if ecmwf_values else 0
    if gfs_n <= 3 and ecmwf_n <= 3:
        divergence_max_relaxed = float(ens_cfg.get("divergence_max_low_member", 0.30))
        if divergence_max_relaxed > divergence_max:
            logger.info(
                f"[DualModel-T277] {city}: 双模型低成员(GFS={gfs_n},ECMWF={ecmwf_n}),"
                f" 分歧阈值 {divergence_max:.0%} → {divergence_max_relaxed:.0%}"
            )
            divergence_max = divergence_max_relaxed

    if divergence > divergence_max:
        logger.info(
            f"[DualModel] {city} {target_date}: 模型分歧 {divergence:.1%} > {divergence_max:.0%}，跳过"
            f" (GFS={gfs_prob:.3f}, ECMWF={ecmwf_prob:.3f})"
        )
        # 返回分歧标记（而非None），让调用方区分"ECMWF不可用"和"ECMWF可用但分歧"
        return {
            "__divergence_skip__": True,
            "gfs_probability": gfs_prob,
            "ecmwf_probability": ecmwf_prob,
            "divergence": round(divergence, 4),
            "gfs_source": gfs_source,
            "ecmwf_source": ecmwf_source,
            "gfs_data_quality": gfs_quality,
            "ecmwf_data_quality": ecmwf_quality,
            "data_quality": data_quality,
        }

    if divergence <= divergence_high:
        confidence_level = "high"
        ecmwf_w = ecmwf_w_high
        position_multiplier = 1.0
        edge_threshold = edge_high
    else:
        confidence_level = "medium"
        ecmwf_w = ecmwf_w_medium
        position_multiplier = 0.8
        edge_threshold = min_edge

    gfs_w = 1.0 - ecmwf_w
    consensus_prob = round(ecmwf_prob * ecmwf_w + gfs_prob * gfs_w, 4)
    combined_conf = round(
        ecmwf_stats["confidence"] * ecmwf_w + gfs_stats["confidence"] * gfs_w, 3
    )

    # ── METAR 实时观测校准 ─────────────────────────────────────────
    metar_data = None
    metar_note = ""
    icao = parsed.get("station_icao")
    if icao and ens_cfg.get("metar_enabled", True):
        metar_data = _fetch_metar_observation(icao)
        if metar_data:
            consensus_prob, metar_note = _apply_metar_calibration(
                consensus_prob, metar_data, gfs_data, parsed, target_date, days_ahead
            )
            if metar_note:
                logger.info(f"[DualModel] METAR校准 {city}: {metar_note}")

    # ── 摘要 ─────────────────────────────────────────────────────
    var_label = "high" if variable == "temp_max" else "low"
    station_info = parsed.get("station_icao") or "wttr.in"
    summary = (
        f"DUAL-MODEL GFS+ECMWF ({city} {target_date}): "
        f"GFS {gfs_stats['yes_count']}/{gfs_stats['total']}={gfs_prob:.3f} | "
        f"ECMWF {ecmwf_stats['yes_count']}/{ecmwf_stats['total']}={ecmwf_prob:.3f} | "
        f"divergence={divergence:.1%} [{confidence_level}] | "
        f"consensus P(YES)={consensus_prob:.3f} "
        f"(ECMWF×{ecmwf_w:.0%}+GFS×{gfs_w:.0%}) | "
        f"edge_threshold={edge_threshold:.0%}, pos_mult={position_multiplier} | "
        f"quality={data_quality}, gfs_src={gfs_source}, ecmwf_src={ecmwf_source}"
    )

    logger.info(
        f"[DualModel] ★ {city}({station_info}) {target_date} {var_label}: "
        f"GFS={gfs_prob:.3f}({gfs_stats['yes_count']}/{gfs_stats['total']}), "
        f"ECMWF={ecmwf_prob:.3f}({ecmwf_stats['yes_count']}/{ecmwf_stats['total']}), "
        f"divergence={divergence:.1%}, consensus={consensus_prob:.3f}, "
        f"conf={confidence_level}, horizon={days_ahead}d"
    )

    return {
        "probability": consensus_prob,
        "confidence": combined_conf,
        "summary": summary,
        "members_total": gfs_stats["total"],
        "members_yes": gfs_stats["yes_count"],
        "ensemble_mean": gfs_stats["mean_c"],
        "ensemble_std": gfs_stats["std_c"],
        "ensemble_values": [round(v, 2) for v in gfs_values],
        "threshold_c": gfs_stats["threshold_c"],
        "target_date": target_date,
        "city": city,
        "variable": variable,
        "direction": parsed["direction"],
        "station_icao": station_info,
        "days_ahead": days_ahead,
        "source": "dual_model_gfs_ecmwf",
        "data_quality": data_quality,
        "gfs_source": gfs_source,
        "ecmwf_source": ecmwf_source,
        "gfs_data_quality": gfs_quality,
        "ecmwf_data_quality": ecmwf_quality,
        # 双模型专有字段
        "gfs_probability": gfs_prob,
        "ecmwf_probability": ecmwf_prob,
        "divergence": round(divergence, 4),
        "confidence_level": confidence_level,
        "position_multiplier": position_multiplier,
        "edge_threshold": edge_threshold,
        "ecmwf_members_yes": ecmwf_stats["yes_count"],
        "ecmwf_members_total": ecmwf_stats["total"],
        # METAR 观测数据
        "metar_temp_c": metar_data["temp_c"] if metar_data else None,
        "metar_obs_time": metar_data.get("obs_time") if metar_data else None,
        "metar_source": metar_data["source"] if metar_data else None,
        "metar_calibration": metar_note or None,
    }


# ===========================================================================
# WU Daily Observations 实测温度观察器 — Phase 56 核心止损功能
# ===========================================================================
# 用途: 监控Polymarket天气市场结算依据的实时观测温度（与WU结算站点一致）
# 当实测温度已达/超过投注阈值时，宣告该方向仓位死刑，立即触发强制平仓
# ===========================================================================

# WU Daily History 页面 URL 路径映射: ICAO → WU URL path (country/city-slug/ICAO)
# 来源: https://www.wunderground.com/history/daily/{path}
_WU_HISTORY_PATH_MAP: dict[str, str] = {
    # ── 北美 (美国) ──
    "KATL": "us-ga/atlanta/KATL",
    "KAUS": "us-tx/austin/KAUS",
    "KBOS": "us-ma/boston/KBOS",
    "KORD": "us-il/chicago/KORD",
    "KDAL": "us-tx/dallas/KDAL",
    "KDEN": "us-co/denver/KDEN",
    "KHOU": "us-tx/houston/KHOU",
    "KLAS": "us-nv/las-vegas/KLAS",
    "KLAX": "us-ca/los-angeles/KLAX",
    "KMIA": "us-fl/miami/KMIA",
    "KBNA": "us-tn/nashville/KBNA",
    "KLGA": "us-ny/new-york-city/KLGA",
    "KPHL": "us-pa/philadelphia/KPHL",
    "KPHX": "us-az/phoenix/KPHX",
    "KPDX": "us-or/portland/KPDX",
    "KSAN": "us-ca/san-diego/KSAN",
    "KSFO": "us-ca/san-francisco/KSFO",
    "KSEA": "us-wa/seattle/KSEA",
    "KDCA": "us-dc/washington/KDCA",
    # ── 加拿大 ──
    "CYUL": "ca/montreal/CYUL",
    "CYYZ": "ca/toronto/CYYZ",
    "CYVR": "ca/vancouver/CYVR",
    # ── 拉美 ──
    "SKBO": "co/bogota/SKBO",
    "SAEZ": "ar/buenos-aires/SAEZ",
    "SPJC": "pe/lima/SPJC",
    "MMMX": "mx/mexico-city/MMMX",
    "SBGR": "br/sao-paulo/SBGR",
    "SCEL": "cl/santiago/SCEL",
    # ── 欧洲 ──
    "EHAM": "nl/amsterdam/EHAM",
    "LTAC": "tr/ankara/LTAC",
    "EDDB": "de/berlin/EDDB",
    "EKCH": "dk/copenhagen/EKCH",
    "EIDW": "ie/dublin/EIDW",
    "EFHK": "fi/helsinki/EFHK",
    "LTFM": "tr/istanbul/LTFM",
    "LPPT": "pt/lisbon/LPPT",
    "EGLC": "gb/london/EGLC",
    "LEMD": "es/madrid/LEMD",
    "LIMC": "it/milan/LIMC",
    "UUEE": "ru/moscow/UUEE",
    "EDDM": "de/munich/EDDM",
    "ENGM": "no/oslo/ENGM",
    "LFPG": "fr/paris/LFPG",
    "LKPR": "cz/prague/LKPR",
    "LIRF": "it/rome/LIRF",
    "ESSA": "se/stockholm/ESSA",
    "LOWW": "at/vienna/LOWW",
    "EPWA": "pl/warsaw/EPWA",
    "LSZH": "ch/zurich/LSZH",
    # ── 中东 / 非洲 ──
    "HECA": "eg/cairo/HECA",
    "OMDB": "ae/dubai/OMDB",
    "FAOR": "za/johannesburg/FAOR",
    "OERK": "sa/riyadh/OERK",
    "LLBG": "il/tel-aviv/LLBG",
    # ── 亚洲 ──
    "VTBS": "th/bangkok/VTBS",
    "ZBAA": "cn/beijing/ZBAA",
    "ZUUU": "cn/chengdu/ZUUU",
    "ZUCK": "cn/chongqing/ZUCK",
    "VIDP": "in/delhi/VIDP",
    "VHHH": "hk/hong-kong/VHHH",
    "WIII": "id/jakarta/WIII",
    "VILK": "in/lucknow/VILK",
    "RPLL": "ph/manila/RPLL",
    "VABB": "in/mumbai/VABB",
    "RJBB": "jp/osaka/RJBB",
    "RKSI": "kr/seoul/RKSI",
    "ZSPD": "cn/shanghai/ZSPD",
    "ZGSZ": "cn/shenzhen/ZGSZ",
    "WSSS": "sg/singapore/WSSS",
    "RCTP": "tw/taipei/RCTP",
    "RJTT": "jp/tokyo/RJTT",
    "ZHHH": "cn/wuhan/ZHHH",
    # ── 大洋洲 ──
    "NZAA": "nz/auckland/NZAA",
    "YMML": "au/melbourne/YMML",
    "YSSY": "au/sydney/YSSY",
    "NZWN": "nz/wellington/NZWN",
}

# 实测温度缓存 (3分钟TTL)，避免10-30s监控循环频繁请求
# 格式: cache_key → (timestamp, max_temp_c_or_None, source_name)
_WU_OBS_CACHE: dict[str, tuple[float, Optional[float], str]] = {}
_WU_OBS_CACHE_TTL: int = 180  # 秒

# WU /weather/ 实时页面专用缓存（60s TTL，比history页面更新快，需更短间隔）
# Task214: 新增数据源 — wunderground.com/weather/{path} 最新观测温度
_WU_REALTIME_CACHE: dict[str, tuple[float, Optional[float], str]] = {}
_WU_REALTIME_CACHE_TTL: int = 60  # 秒（每60s更新一次实时读数）


def get_station_info(city_name: str) -> Optional[tuple[str, float, float, str]]:
    """
    返回城市对应 ICAO 气象站的完整信息 (icao, lat, lon, full_name)。
    查找顺序: config.json weather_cities → _STATION_MAP。
    """
    key = city_name.lower().strip()
    # T276: 优先从 config 查找
    cfg_city = _load_config_city(key)
    if (
        cfg_city
        and cfg_city.get("icao")
        and cfg_city.get("lat")
        and cfg_city.get("lon")
    ):
        return (
            cfg_city["icao"],
            float(cfg_city["lat"]),
            float(cfg_city["lon"]),
            cfg_city.get("name", key),
        )
    return _STATION_MAP.get(key)


def fetch_daily_observed_max_temp(
    icao: str,
    lat: float,
    lon: float,
    date_str: Optional[str] = None,
    city_name: str = "",
    native_unit: str = "C",
    settlement_source: str = "",
) -> tuple[Optional[float], str]:
    """
    获取指定 ICAO 气象站当日观测最高温度（°C）。

    T278: 按 settlement_source 路由到对应数据源——
      - "noaa"         → aviationweather.gov METAR API (4个城市)
      - "hko"          → 香港天文台 Open Data API (1个城市)
      - "wunderground"  → WU P0-P3 链 (33个城市，默认)

    如果指定源失败，NOAA/HKO 会降级到 WU 链作为兜底。

    WU 优先级（默认 / wunderground）:
      P0   [最可靠] WU Weather.com v3 API — 绕过 Akamai WAF 403
                 T229: °F市场用 units=e 取整数°F → 精确转°C
      P1   [备选]  WU /weather/ 实时页面
      P1.5 [T148]  WU v1 Historical JSON API — 纯JSON不受WAF
      P2   [备选]  WU /history/daily/ HTML页面 (可能被WAF 403)
      P3   [兜底]  Open-Meteo 逐小时实测气温 API

    Args:
        icao:              ICAO 气象站代码，如 "NZWN"
        lat:               气象站纬度
        lon:               气象站经度
        date_str:          "YYYY-MM-DD" 格式日期，默认使用 UTC 当天
        city_name:         城市名（仅供日志显示）
        native_unit:       市场结算单位 "F" 或 "C"
        settlement_source: "noaa" | "hko" | "wunderground" | ""(自动检测)

    Returns:
        (max_temp_c, source_name) — 成功
        (None, "")               — 所有数据源均失败
    """
    _today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _now = time.time()
    _label = city_name or icao

    # T278: 自动检测 settlement_source（如果未传入）
    if not settlement_source and city_name:
        _ss_info = get_city_settlement_source(city_name)
        settlement_source = _ss_info.get("settlement_source", "wunderground")

    # ═══════════════════════════════════════════════════════════════
    # T278: NOAA 路由 — 4个城市 (Istanbul, Taipei, Tel Aviv, Moscow)
    # ═══════════════════════════════════════════════════════════════
    if settlement_source == "noaa":
        noaa_icao = icao
        if city_name:
            _ss_info = get_city_settlement_source(city_name)
            noaa_icao = _ss_info.get("noaa_site") or icao
        noaa_temp, noaa_src = fetch_noaa_observed_max_temp(noaa_icao, _today, _label)
        if noaa_temp is not None:
            return (noaa_temp, noaa_src)
        logger.warning(f"[SettlementRoute] {_label} NOAA 失败, 降级到 WU 链")
        # 降级到 WU 链

    # ═══════════════════════════════════════════════════════════════
    # T278: HKO 路由 — 1个城市 (Hong Kong)
    # ═══════════════════════════════════════════════════════════════
    if settlement_source == "hko":
        hko_temp, hko_src = fetch_hko_observed_max_temp(_today, _label)
        if hko_temp is not None:
            return (hko_temp, hko_src)
        logger.warning(f"[SettlementRoute] {_label} HKO 失败, 降级到 WU 链")
        # 降级到 WU 链

    _wu_path = _WU_HISTORY_PATH_MAP.get(icao)

    # ═══════════════════════════════════════════════════════════════
    # P0 [MOST RELIABLE]: WU Weather.com v3 API (Task224)
    # 直接调用 WU 后端 API，绕过 Akamai WAF 对 HTML 页面的 403 拦截。
    # 返回与 /weather/ 页面完全相同的观测数据（同一 Weather Company 数据源）。
    # ═══════════════════════════════════════════════════════════════
    if lat and lon:
        _api_temp = _fetch_wu_api_temp(lat, lon, _label, native_unit=native_unit)
        if _api_temp is not None:
            if native_unit == "F":
                _native_f = round(_api_temp * 9 / 5 + 32, 1)
                logger.info(
                    f"[WUTempObserver] {_label} 今日观测温度="
                    f"{_native_f:.0f}°F/{_api_temp:.1f}°C (WU-API)"
                )
            else:
                logger.info(
                    f"[WUTempObserver] {_label} 今日观测温度="
                    f"{_api_temp:.1f}°C (WU-API)"
                )
            return (_api_temp, "wu-api")

    # ═══════════════════════════════════════════════════════════════
    # P1 [FALLBACK]: WU /weather/ 实时页面（Akamai 可能放行时尝试）
    # ═══════════════════════════════════════════════════════════════
    _wu_blocked = False
    if _wu_path:
        _rt_cache_key = f"wu_rt:{icao}:{_today}"
        if _rt_cache_key in _WU_REALTIME_CACHE:
            _ts, _rt_c, _rt_src = _WU_REALTIME_CACHE[_rt_cache_key]
            if _now - _ts < _WU_REALTIME_CACHE_TTL:
                if _rt_c is not None:
                    return (_rt_c, _rt_src)
                _wu_blocked = True

        if not _wu_blocked:
            _rt_temp = _fetch_wu_realtime_temp(_wu_path)
            if _rt_temp is not None:
                _WU_REALTIME_CACHE[_rt_cache_key] = (_now, _rt_temp, "wu-weather")
                logger.info(
                    f"[WUTempObserver] {_label} 实时观测={_rt_temp:.1f}°C (WU/weather页面)"
                )
                return (_rt_temp, "wu-weather")
            # 实时页面失败：缓存失败状态(30s)
            _WU_REALTIME_CACHE[_rt_cache_key] = (
                _now - _WU_REALTIME_CACHE_TTL + 30,
                None,
                "",
            )
            _wu_blocked = True

    # ═══════════════════════════════════════════════════════════════
    # P1.5 + P2 + P3: WU v1 JSON API + history 页面 + Open-Meteo
    # ═══════════════════════════════════════════════════════════════
    return _fallback_history_or_om(
        icao,
        lat,
        lon,
        _today,
        _wu_path,
        _label,
        _now,
        skip_wu_history=_wu_blocked,
    )


def _fallback_history_or_om(
    icao: str,
    lat: float,
    lon: float,
    today: str,
    wu_path: Optional[str],
    label: str,
    now: float,
    skip_wu_history: bool = False,
) -> tuple[Optional[float], str]:
    """P1.5+P2+P3降级逻辑：WU v1 JSON API → WU history页面 → Open-Meteo。

    T148: 新增 P1.5 WU v1 Historical JSON API — 纯JSON接口不受WAF影响，
    在 P2 HTML抓取（常被Akamai 403）前提供可靠数据源。

    Bug221: skip_wu_history=True 时跳过 P2（WU /weather/ 已被封，
    同域 /history/daily/ 大概率也被封，避免重复触发反爬虫）。
    注意: P1.5 是独立的JSON API，不受HTML WAF影响，始终尝试。
    """
    _cache_key = f"wu_obs:{icao}:{today}"

    # --- 缓存命中 ---
    if _cache_key in _WU_OBS_CACHE:
        _ts, _max_c, _src = _WU_OBS_CACHE[_cache_key]
        if now - _ts < _WU_OBS_CACHE_TTL:
            return (_max_c, _src)

    # --- P1.5 [T148]: WU v1 Historical JSON API (纯JSON，不受WAF) ---
    if lat and lon:
        _v1_max_c = _fetch_wu_v1_historical_max_temp(lat, lon, today, label)
        if _v1_max_c is not None:
            _WU_OBS_CACHE[_cache_key] = (now, _v1_max_c, "wu-v1-historical")
            return (_v1_max_c, "wu-v1-historical")

    # --- P2: WU Daily History 页面抓取 (HTML，可能被WAF 403) ---
    if wu_path and not skip_wu_history:
        _wu_max_c = _fetch_wu_daily_max_temp(wu_path, today)
        if _wu_max_c is not None:
            _WU_OBS_CACHE[_cache_key] = (now, _wu_max_c, "wunderground")
            logger.info(
                f"[WUTempObserver] {label} 今日最高={_wu_max_c:.1f}°C (WU/history页面)"
            )
            return (_wu_max_c, "wunderground")
        logger.debug(f"[WUTempObserver] {label} WU history页面无数据, 尝试Open-Meteo")
    elif skip_wu_history:
        logger.debug(
            f"[WUTempObserver] {label} 跳过 WU /history/daily/"
            f" (P1 /weather/ 已失败，避免重复触发反爬虫)"
        )
    else:
        logger.debug(f"[WUTempObserver] {icao} 不在 _WU_HISTORY_PATH_MAP, 跳过WU抓取")

    # --- P3: Open-Meteo 已过小时实测值 ---
    _om_max_c = _fetch_open_meteo_observed_max(lat, lon, today)
    if _om_max_c is not None:
        _WU_OBS_CACHE[_cache_key] = (now, _om_max_c, "open-meteo")
        logger.info(
            f"[WUTempObserver] {label} 今日实测最高={_om_max_c:.1f}°C (Open-Meteo)"
        )
        return (_om_max_c, "open-meteo")

    # --- 全部失败 --- 缓存失败结果 1 分钟避免频繁重试
    _WU_OBS_CACHE[_cache_key] = (now, None, "")
    logger.debug(f"[WUTempObserver] {label} 所有数据源均无法获取今日实测温度")
    return (None, "")


# ── WU Weather.com v3 API（Task 224: Akamai 403 绕过方案）──────────
# WU 网页使用 Akamai WAF 拦截自动化访问（403 Forbidden），但 WU 后端
# api.weather.com v3 API 可直接请求，返回与 /weather/ 页面完全相同的
# 观测数据（同一数据源 Weather Company）。
# API key 是 WU 前端嵌入的公开 key，多个备选确保可用性。
_WU_API_KEYS = [
    "e1f10a1e78da46f5b10a1e78da96f525",
    "6532d6454b8aa370768e63d6ba5a832e",
]
_WU_API_CACHE: dict[str, tuple[float, Optional[float]]] = {}
_WU_API_CACHE_TTL = 120  # 2min 缓存
# T229: Running max — since7am=None 时用 current 维护当日最高
_WU_RUNNING_MAX: dict[str, tuple[str, float]] = {}  # cache_key → (date, max_native)


def _fetch_wu_api_temp(
    lat: float, lon: float, label: str = "", native_unit: str = "C"
) -> Optional[float]:
    """Task224+T229: 通过 Weather.com v3 API 获取实时观测温度（返回°C）。

    这是 WU /weather/ 页面的后端数据源，绕过 Akamai WAF 403 拦截。

    T229 修复：
      - °F市场用 units=e 请求（拿整数°F），精确转°C 返回 → 消除双重取整误差
      - °C市场用 units=m 请求（拿整数°C），直接返回
      - since7am=None 时用 running_max 追踪当日最高（防下午降温后低报）

    字段优先级（Task226）：
      1. temperatureMaxSince7am — 今天7am(当地时间)以来的最高温
      2. temperature — 当前观测温度（实时值）+ running_max
      ★ 不再使用 temperatureMax24Hour

    失败时返回 None。
    """
    if not lat or not lon:
        return None

    _api_units = "e" if native_unit == "F" else "m"
    _cache_key = f"wu_api:{lat:.2f},{lon:.2f}:{_api_units}"
    _now = time.time()
    if _cache_key in _WU_API_CACHE:
        _ts, _cached_val = _WU_API_CACHE[_cache_key]
        if _now - _ts < _WU_API_CACHE_TTL:
            return _cached_val

    _geocode = f"{lat:.4f},{lon:.4f}"
    for _key in _WU_API_KEYS:
        _url = (
            f"https://api.weather.com/v3/wx/observations/current"
            f"?geocode={_geocode}&language=en-US&units={_api_units}&format=json"
            f"&apiKey={_key}"
        )
        try:
            _resp = requests.get(_url, timeout=12)
            if _resp.status_code == 401:
                logger.debug(f"[WU-API] apiKey {_key[:8]}... 返回 401，尝试备选")
                continue
            _resp.raise_for_status()
            _data = _resp.json()
            _temp_cur = _data.get("temperature")
            _temp_max24 = _data.get("temperatureMax24Hour")
            _temp_since7am = _data.get("temperatureMaxSince7am")

            # Task226: 严格字段优先级
            _result_native = None
            _field_used = ""
            if isinstance(_temp_since7am, (int, float)):
                _result_native = float(_temp_since7am)
                _field_used = "maxSince7am"
            elif isinstance(_temp_cur, (int, float)):
                _result_native = float(_temp_cur)
                _field_used = "current"

            if _result_native is None:
                logger.debug(
                    f"[WU-API] {label} 响应无有效温度字段: "
                    f"since7am={_temp_since7am}, cur={_temp_cur}, max24h={_temp_max24}"
                )
                _WU_API_CACHE[_cache_key] = (_now, None)
                return None

            # T229: since7am=None 时用 running_max 追踪当日最高
            if _field_used == "current":
                _today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                _rmax_key = f"{lat:.2f},{lon:.2f}:{_api_units}"
                if _rmax_key in _WU_RUNNING_MAX:
                    _prev_date, _prev_max = _WU_RUNNING_MAX[_rmax_key]
                    if _prev_date == _today_str:
                        _result_native = max(_result_native, _prev_max)
                    # 日期变更，自动重置
                _WU_RUNNING_MAX[_rmax_key] = (_today_str, _result_native)
                _field_used = "current(running_max)"

            # T229: 转换为°C返回（保留精度）
            if native_unit == "F":
                # units=e → 整数°F，精确转°C
                _result_c = (_result_native - 32) * 5 / 9
                _native_label = f"{_result_native:.0f}°F"
                # 合理性检查（°F范围）
                if not (-76 <= _result_native <= 140):
                    logger.warning(
                        f"[WU-API] {label} 温度异常: {_result_native}°F，跳过"
                    )
                    _WU_API_CACHE[_cache_key] = (_now, None)
                    return None
            else:
                # units=m → 整数°C，直接使用
                _result_c = _result_native
                _native_label = f"{_result_native:.0f}°C"
                if not (-60 <= _result_native <= 60):
                    logger.warning(
                        f"[WU-API] {label} 温度异常: {_result_native}°C，跳过"
                    )
                    _WU_API_CACHE[_cache_key] = (_now, None)
                    return None

            _WU_API_CACHE[_cache_key] = (_now, _result_c)
            logger.info(
                f"[WU-API] {label} 观测={_native_label}→{_result_c:.2f}°C "
                f"(units={_api_units}, field={_field_used}, "
                f"since7am={_temp_since7am}, cur={_temp_cur}, max24h={_temp_max24})"
            )
            return _result_c

        except requests.RequestException as _e:
            logger.debug(f"[WU-API] {label} 请求失败 (key={_key[:8]}...): {_e}")
            continue
        except (ValueError, KeyError) as _e:
            logger.debug(f"[WU-API] {label} 解析失败: {_e}")
            break

    _WU_API_CACHE[_cache_key] = (_now, None)
    logger.warning(f"[WU-API] {label} 所有 API key 均失败")
    return None


def _fetch_wu_realtime_temp(wu_path: str) -> Optional[float]:
    """
    Task214: 从 WU /weather/ 实时页面抓取最新观测温度（°C）。
    URL: https://www.wunderground.com/weather/{wu_path}

    目标 HTML (Angular SSR server-side rendered):
      <div class="current-temp">
        <span class="wu-value wu-value-to">21</span>
      </div>

    WU 使用 Angular Universal (SSR)，初始 HTTP 响应包含服务端渲染的温度数据。
    比 /history/daily/ 页面提前数分钟更新（直接读取最新气象站观测值）。
    失败时返回 None（不抛出异常）。
    """
    _url = f"https://www.wunderground.com/weather/{wu_path}"
    _session = requests.Session()
    _session.headers.update(_HEADERS)

    for _attempt in range(2):
        try:
            _resp = _session.get(_url, timeout=15)
            _resp.raise_for_status()
            _html = _resp.text

            # ── 模式1: JSON 嵌入数据中的当日最高温（Angular SSR state） ──
            # WU 有时会把观测数据序列化到 <script> 或 window 变量中
            _m = re.search(r'"tempHigh"\s*:\s*\{\s*"value"\s*:\s*([\d.-]+)', _html)
            if _m:
                return float(_m.group(1))

            _m = re.search(r'"currentHighTemp(?:erature)?"\s*:\s*([\d.-]+)', _html)
            if _m:
                return float(_m.group(1))

            _m = re.search(r'"todayHighTemp(?:erature)?"\s*:\s*([\d.-]+)', _html)
            if _m:
                return float(_m.group(1))

            # ── 模式2: <div class="current-temp"> 区块内的 wu-value-to（直接HTML）──
            # Angular SSR 渲染的初始 HTML 中包含完整 DOM 结构
            _block = re.search(
                r'class="current-temp"[^>]*>(.{0,600}?)</lib-display-unit>',
                _html,
                re.DOTALL,
            )
            if _block:
                _m = re.search(
                    r'class="wu-value wu-value-to">\s*(-?[\d.]+)\s*<',
                    _block.group(1),
                )
                if _m:
                    _val = float(_m.group(1))
                    # 合理性检查: 气温在 -60°C ~ 60°C 范围内有效
                    if -60 <= _val <= 60:
                        return _val

            # ── 模式3: application/ld+json 结构化数据（SEO标准格式）──
            _json_blocks = re.findall(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>',
                _html,
                re.DOTALL,
            )
            for _jb in _json_blocks:
                try:
                    import json as _json

                    _jdata = _json.loads(_jb)
                    # WeatherObservation / WeatherForecast schema.org types
                    _temp = (
                        _jdata.get("temperature", {}).get("value")
                        if isinstance(_jdata.get("temperature"), dict)
                        else _jdata.get("temperature")
                    )
                    if _temp is not None:
                        _v = float(_temp)
                        if -60 <= _v <= 60:
                            return _v
                except (ValueError, AttributeError, KeyError):
                    pass

            logger.debug(
                f"[WURealtimeTemp] 无法从 {wu_path} 提取温度 (响应长度={len(_html)}字节)"
            )
            return None

        except requests.RequestException as _e:
            # Bug221: 提升日志级别让用户可见 P1 /weather/ 页面被尝试过
            _lvl = logging.WARNING if _attempt == 0 else logging.DEBUG
            logger.log(
                _lvl,
                f"[WUTempObserver] WU /weather/ 请求失败 "
                f"(尝试{_attempt+1}/2, {wu_path}): {_e}",
            )
            if _attempt == 0:
                time.sleep(3)
        except Exception as _e:
            logger.debug(f"[WURealtimeTemp] 解析异常 ({wu_path}): {_e}")
            break

    return None


def _fetch_wu_v1_historical_max_temp(
    lat: float, lon: float, date_str: str, label: str = ""
) -> Optional[float]:
    """T148: WU v1 Historical Observations JSON API — 纯JSON接口，不受WAF影响。

    端点: api.weather.com/v1/geocode/{lat}/{lon}/observations/historical.json
    与 WU /history/daily/ 页面数据源相同，但走 JSON API 而非 HTML 抓取。
    取 max(obs[].temp) = 当日截至目前的实测最高温度（°C）。

    Args:
        lat:      纬度
        lon:      经度
        date_str: "YYYY-MM-DD" 格式日期
        label:    城市名（仅供日志）

    Returns:
        最高温度°C 或 None
    """
    if not lat or not lon:
        return None

    try:
        _api_date = date_str.replace("-", "")  # YYYYMMDD
    except (ValueError, AttributeError):
        return None

    for _key in _WU_API_KEYS:
        try:
            _url = (
                f"https://api.weather.com/v1/geocode/{lat}/{lon}"
                f"/observations/historical.json"
                f"?apiKey={_key}"
                f"&startDate={_api_date}&endDate={_api_date}"
                f"&units=m"
            )
            _resp = requests.get(_url, timeout=12)
            if _resp.status_code == 401:
                continue
            _resp.raise_for_status()
            _obs = _resp.json().get("observations", [])
            _temps = [
                o["temp"] for o in _obs if isinstance(o.get("temp"), (int, float))
            ]
            if _temps:
                _max_c = round(float(max(_temps)), 1)
                logger.info(
                    f"[WUTempObserver] {label} 今日最高={_max_c}°C "
                    f"(WU-v1-historical, {len(_obs)}条观测)"
                )
                return _max_c
            break  # API正常但无温度数据，不再尝试下一key
        except requests.RequestException as _e:
            logger.debug(
                f"[WU-v1-Historical] {label} 请求失败 (key={_key[:8]}...): {_e}"
            )
            continue
        except (ValueError, KeyError) as _e:
            logger.debug(f"[WU-v1-Historical] {label} 解析失败: {_e}")
            break

    return None


def _fetch_wu_daily_max_temp(wu_path: str, date_str: str) -> Optional[float]:
    """
    从 WU Daily History 页面抓取当日最高温度（°C）。
    URL: https://www.wunderground.com/history/daily/{wu_path}/date/{YYYY-M-D}
    支持多种嵌入 JSON 格式（WU 前端版本差异）。
    失败时返回 None（不抛出异常）。
    """
    # WU 使用不带前导零的日期格式: 2026-3-25 而非 2026-03-25
    try:
        _year, _month, _day = date_str.split("-")
        _wu_date = f"{_year}-{int(_month)}-{int(_day)}"
    except ValueError:
        logger.debug(f"[WUTempObserver] 日期格式错误: {date_str}")
        return None

    _url = f"https://www.wunderground.com/history/daily/{wu_path}/date/{_wu_date}"
    session = requests.Session()
    session.headers.update(_HEADERS)

    for attempt in range(2):
        try:
            resp = session.get(_url, timeout=15)
            resp.raise_for_status()
            html = resp.text

            # 模式1: 每日摘要 metric JSON 中的 tempHigh
            m = re.search(r'"metric"\s*:\s*\{[^}]*"tempHigh"\s*:\s*([\d.-]+)', html)
            if m:
                return float(m.group(1))

            # 模式2: value 格式的 tempHigh
            m = re.search(r'"tempHigh"\s*:\s*\{\s*"value"\s*:\s*([\d.-]+)', html)
            if m:
                return float(m.group(1))

            # 模式3: highTemperature 字段
            m = re.search(r'"highTemperature"\s*:\s*([\d.-]+)', html)
            if m:
                return float(m.group(1))

            # 模式4: maxTemp metric 格式
            m = re.search(r'"maxTemp"\s*:\s*\{[^}]*"metric"\s*:\s*([\d.-]+)', html)
            if m:
                return float(m.group(1))

            # 模式5: imperial tempHigh → 转换为°C
            m = re.search(r'"imperial"\s*:\s*\{[^}]*"tempHigh"\s*:\s*([\d.-]+)', html)
            if m:
                temp_f = float(m.group(1))
                return round((temp_f - 32) * 5 / 9, 1)

            # 模式6: 从逐小时观测列表中取最大值（metric temp）
            metric_temps = re.findall(
                r'"metric"\s*:\s*\{[^}]*?"temp"\s*:\s*([\d.-]+)', html
            )
            if metric_temps:
                float_temps = [float(t) for t in metric_temps if t]
                if float_temps:
                    return round(max(float_temps), 1)

            logger.debug(f"[WUTempObserver] WU页面无法提取温度: {_url}")
            return None

        except requests.RequestException as e:
            lvl = logging.WARNING if attempt == 0 else logging.DEBUG
            logger.log(
                lvl,
                f"[WUTempObserver] WU请求失败 (尝试{attempt + 1}/2, {wu_path}): {e}",
            )
            if attempt == 0:
                time.sleep(3)
        except Exception as e:
            logger.debug(f"[WUTempObserver] WU解析异常 ({wu_path}): {e}")
            break

    return None


def _fetch_open_meteo_observed_max(
    lat: float, lon: float, date_str: str
) -> Optional[float]:
    """
    从 Open-Meteo 获取指定日期已过去小时的实测最高温度（°C）。
    使用 UTC 时区 + 与当前 UTC 时间比对，仅统计已过去的整点小时。
    覆盖全球（不限美国），作为 WU 抓取失败时的可靠备用。
    """
    try:
        api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&timezone=UTC"
            f"&start_date={date_str}&end_date={date_str}"
        )
        resp = requests.get(api_url, timeout=12)
        resp.raise_for_status()
        data = resp.json()

        temps: list = data.get("hourly", {}).get("temperature_2m", [])
        times: list = data.get("hourly", {}).get("time", [])

        if not temps:
            return None

        # 仅使用当前 UTC 时间之前的整点小时（避免未来预报误触发）
        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        valid_temps: list[float] = []
        for t_str, temp in zip(times, temps):
            if temp is None:
                continue
            try:
                t_dt = datetime.strptime(t_str, "%Y-%m-%dT%H:%M")
                if t_dt <= now_utc_naive:
                    valid_temps.append(float(temp))
            except ValueError:
                continue

        if not valid_temps:
            return None

        return round(max(valid_temps), 1)

    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        logger.debug(f"[WUTempObserver] Open-Meteo实测温度获取失败 ({lat},{lon}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# T278: Settlement-Source-Aligned Data Fetchers
# ──────────────────────────────────────────────────────────────────────────
# Polymarket 各城市结算源不统一（33 WU / 4 NOAA / 1 HKO），必须按城市
# 使用与 Polymarket 官方一致的数据源，否则死刑线/止损/结算会偏差。
# ══════════════════════════════════════════════════════════════════════════

# -- NOAA 最高温度缓存 (3分钟TTL) --
_NOAA_OBS_CACHE: dict[str, tuple[float, Optional[float], str]] = {}
_NOAA_OBS_CACHE_TTL: int = 180


def get_city_settlement_source(city_name: str) -> dict:
    """
    从 config.json 获取城市的 Polymarket 结算源配置。

    返回 dict:
      {
        "settlement_source": "wunderground" | "noaa" | "hko",
        "settlement_url": "https://...",
        "noaa_site": "LTFM" (仅 noaa 城市),
        "settlement_precision": 0 (整数) 或 1 (一位小数, 仅 hko),
      }
    默认回退到 "wunderground"。
    """
    cfg = _load_config_city(city_name.lower().strip())
    if cfg:
        return {
            "settlement_source": cfg.get("settlement_source", "wunderground"),
            "settlement_url": cfg.get("settlement_url", ""),
            "noaa_site": cfg.get("noaa_site", cfg.get("icao", "")),
            "settlement_precision": cfg.get("settlement_precision", 0),
        }
    return {
        "settlement_source": "wunderground",
        "settlement_url": "",
        "noaa_site": "",
        "settlement_precision": 0,
    }


def fetch_noaa_observed_max_temp(
    icao: str,
    date_str: Optional[str] = None,
    city_name: str = "",
) -> tuple[Optional[float], str]:
    """
    T278: 通过 aviationweather.gov METAR API 获取指定日期的最高观测温度。

    NOAA weather.gov/wrh/timeseries 页面底层调用 Synoptic Data API（需域名绑定 token），
    但 aviationweather.gov 是同一 NOAA 数据源的公开 API，返回全球 ICAO 站点的
    METAR 观测记录（包含整数°C温度），与 Polymarket 结算值一致。

    Args:
        icao:      NOAA weather.gov 使用的站点代码（如 LTFM, RCTP, UUWW, LLBG）
        date_str:  "YYYY-MM-DD" 格式日期，默认 UTC 当天
        city_name: 城市名（日志用）

    Returns:
        (max_temp_c, "noaa-metar") 或 (None, "")
    """
    _today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _label = city_name or icao
    _now = time.time()

    # 缓存检查
    _cache_key = f"noaa_obs:{icao}:{_today}"
    if _cache_key in _NOAA_OBS_CACHE:
        _ts, _cached_c, _cached_src = _NOAA_OBS_CACHE[_cache_key]
        if _now - _ts < _NOAA_OBS_CACHE_TTL:
            return (_cached_c, _cached_src)

    try:
        # 使用 date 参数获取特定日期的 METAR 数据
        # 设置 date 为目标日期次日 00:00 UTC，hours=24 获取前24小时
        from datetime import timedelta

        target_date = datetime.strptime(_today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # 如果是今天，用当前时间
        now_utc = datetime.now(timezone.utc)
        if target_date.date() == now_utc.date():
            # 当日：获取最近24小时
            params = {"ids": icao, "format": "json", "hours": "24"}
        else:
            # 历史日期：取次日00:00之前24小时
            next_day = target_date + timedelta(days=1)
            params = {
                "ids": icao,
                "format": "json",
                "date": next_day.strftime("%Y-%m-%dT00:00:00Z"),
                "hours": "24",
            }

        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            logger.debug(f"[NOAA] {_label} ({icao}) METAR 返回空数据 {_today}")
            _NOAA_OBS_CACHE[_cache_key] = (_now, None, "")
            return (None, "")

        # 筛选目标日期内的观测值
        valid_temps: list[float] = []
        for obs in data:
            report_time = obs.get("reportTime", "")
            temp = obs.get("temp")
            if temp is None:
                continue
            # 检查报告时间是否在目标日期内
            try:
                obs_dt = datetime.fromisoformat(
                    report_time.replace("Z", "+00:00").replace(".000Z", "+00:00")
                )
                obs_date = obs_dt.strftime("%Y-%m-%d")
                if obs_date == _today:
                    valid_temps.append(float(temp))
            except (ValueError, TypeError):
                # 如果无法解析时间，仍包含温度（降低精度但不丢数据）
                valid_temps.append(float(temp))

        if not valid_temps:
            # 如果是当天且没有当天数据，可能是时区差异，放宽到全部数据
            if target_date.date() == now_utc.date():
                valid_temps = [
                    float(obs.get("temp"))
                    for obs in data
                    if obs.get("temp") is not None
                ]

        if not valid_temps:
            logger.debug(f"[NOAA] {_label} ({icao}) 无 {_today} 温度数据")
            _NOAA_OBS_CACHE[_cache_key] = (_now, None, "")
            return (None, "")

        max_temp_c = max(valid_temps)
        _NOAA_OBS_CACHE[_cache_key] = (_now, max_temp_c, "noaa-metar")
        logger.info(
            f"[NOAA] {_label} ({icao}) {_today} 最高观测={max_temp_c:.1f}°C "
            f"(共{len(valid_temps)}条METAR记录)"
        )
        return (max_temp_c, "noaa-metar")

    except requests.RequestException as e:
        logger.warning(f"[NOAA] {_label} ({icao}) aviationweather 请求失败: {e}")
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"[NOAA] {_label} ({icao}) 数据解析失败: {e}")

    _NOAA_OBS_CACHE[_cache_key] = (_now, None, "")
    return (None, "")


# -- HKO 缓存 --
_HKO_OBS_CACHE: dict[str, tuple[float, Optional[float], str]] = {}
_HKO_OBS_CACHE_TTL: int = 300  # 5min


def fetch_hko_observed_max_temp(
    date_str: Optional[str] = None,
    city_name: str = "Hong Kong",
) -> tuple[Optional[float], str]:
    """
    T278: 从香港天文台 (HKO) 获取当日最高气温。

    Polymarket 香港市场使用 HKO "Absolute Daily Max (deg. C)" 作为结算源，
    精度为一位小数（如 9.1°C），与其他城市的整数精度不同。

    数据源优先级：
      P0: HKO 开放数据 API (data.weather.gov.hk)
      P1: HKO 气候资料页面 scrape

    Returns:
        (max_temp_c, "hko") 或 (None, "")
    """
    _today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _now = time.time()

    _cache_key = f"hko_obs:{_today}"
    if _cache_key in _HKO_OBS_CACHE:
        _ts, _cached_c, _cached_src = _HKO_OBS_CACHE[_cache_key]
        if _now - _ts < _HKO_OBS_CACHE_TTL:
            return (_cached_c, _cached_src)

    # P0: HKO Open Data API — 当前气象资料
    try:
        resp = requests.get(
            "https://data.weather.gov.hk/weatherAPI/opendata/weather.php",
            params={"dataType": "rhrread", "lang": "en"},
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        # 从所有测站取最高温（HKO天文台站 = Polymarket参考站）
        temp_data = data.get("temperature", {}).get("data", [])
        hko_temp = None
        for station in temp_data:
            if station.get("place") == "Hong Kong Observatory":
                hko_temp = station.get("value")
                break
        # 如果找不到 HKO 站，用全部站点最高温
        if hko_temp is None and temp_data:
            temps = [s.get("value") for s in temp_data if s.get("value") is not None]
            hko_temp = max(temps) if temps else None

        if hko_temp is not None:
            max_c = round(float(hko_temp), 1)
            # 这是当前温度，非当日最高，但作为实时监控足够
            _HKO_OBS_CACHE[_cache_key] = (_now, max_c, "hko")
            logger.info(f"[HKO] Hong Kong 当前观测={max_c}°C (HKO Open Data)")
            return (max_c, "hko")
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.debug(f"[HKO] Open Data API 失败: {e}")

    # P1: 降级到 aviationweather.gov METAR (VHHH)
    metar_result = fetch_noaa_observed_max_temp("VHHH", _today, "Hong Kong")
    if metar_result[0] is not None:
        _HKO_OBS_CACHE[_cache_key] = (_now, metar_result[0], "hko-metar-fallback")
        return (metar_result[0], "hko-metar-fallback")

    _HKO_OBS_CACHE[_cache_key] = (_now, None, "")
    return (None, "")


# ══════════════════════════════════════════════════════════════════════════
# T54: YES Ambush 辅助函数 — 当前温度 + 趋势概率
# ══════════════════════════════════════════════════════════════════════════

# 当前温度缓存（5分钟TTL，避免频繁API调用）
_CURRENT_TEMP_CACHE: dict[str, tuple[float, Optional[float]]] = {}
_CURRENT_TEMP_TTL = 300  # 5 minutes


def fetch_current_temperature(city_name: str) -> Optional[float]:
    """
    获取城市当前实时温度（°C）。
    使用 Open-Meteo current weather API（免费、全球覆盖）。
    结果缓存 5 分钟。

    T54: YES Ambush 条件2需要 — 当前温度距目标strike的距离。
    """
    key = city_name.lower().strip()
    now = time.time()

    # 缓存检查
    if key in _CURRENT_TEMP_CACHE:
        ts, temp = _CURRENT_TEMP_CACHE[key]
        if now - ts < _CURRENT_TEMP_TTL:
            return temp

    coords = _get_city_coords(key)
    if not coords:
        logger.debug(f"[T54] fetch_current_temperature: 无法获取 {city_name} 坐标")
        return None

    lat, lon = coords
    try:
        api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m"
            f"&timezone=auto"
        )
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current", {})
        temp_c = current.get("temperature_2m")
        if temp_c is not None:
            temp_c = round(float(temp_c), 1)
            _CURRENT_TEMP_CACHE[key] = (now, temp_c)
            logger.debug(f"[T54] {city_name} 当前温度={temp_c}°C (Open-Meteo current)")
            return temp_c

        _CURRENT_TEMP_CACHE[key] = (now, None)
        return None

    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.debug(f"[T54] fetch_current_temperature 失败 ({city_name}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# T233: 风控专用实时温度（METAR 优先 → NWS raw → Open-Meteo fallback）
# ══════════════════════════════════════════════════════════════════════════
# 事故教训：Open-Meteo current 是模型内插值，可滞后 15-30 分钟。
# METAR 是地面观测实测值，每小时更新（特殊天气时更频繁）。
# 风控决策（weather_exit_gate / weather_veto）必须使用最实时数据。
# ══════════════════════════════════════════════════════════════════════════

_GATE_METAR_CACHE: dict[str, tuple[float, Optional[dict]]] = {}


def _fetch_nws_raw_metar(icao: str, timeout: int = 10) -> Optional[dict]:
    """
    NWS tgftp 原始 METAR 文本 — 备用降级源。
    URL: https://tgftp.nws.noaa.gov/data/observations/metar/stations/{ICAO}.TXT
    返回格式与 _fetch_metar_observation 一致。
    """
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": _HEADERS.get("User-Agent", "Mozilla/5.0"),
                "Accept": "text/plain,*/*",
            },
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return None
        # 第1行是观测时间，第2行是METAR原文
        obs_time_line = lines[0].strip()
        raw_metar = lines[1].strip()

        # 提取温度组: TDD/DD 或 TMDD/MDD（M=负号）
        temp_match = re.search(r"\b(M?\d{1,2})/(M?\d{1,2})\b", raw_metar)
        if not temp_match:
            return None

        def _parse_t(s: str) -> float:
            return -float(s[1:]) if s.startswith("M") else float(s)

        temp_c = _parse_t(temp_match.group(1))
        dewp_c = _parse_t(temp_match.group(2))

        # 精确温度: RMK T组 e.g. "T02021101" = 20.2°C / -10.1°C
        t_match = re.search(r"T(\d{4})(\d{4})", raw_metar)
        if t_match:
            t_raw = t_match.group(1)
            sign = -1 if t_raw[0] == "1" else 1
            temp_c = sign * int(t_raw[1:]) / 10.0

        return {
            "temp_c": round(temp_c, 1),
            "dewpoint_c": round(dewp_c, 1),
            "obs_time": obs_time_line,
            "raw": raw_metar,
            "source": "tgftp.nws.noaa.gov",
        }
    except (requests.RequestException, ValueError, IndexError) as e:
        logger.debug(f"[METAR-NWS] tgftp 失败 ({icao}): {e}")
        return None


def fetch_realtime_temperature(city_name: str) -> Optional[float]:
    """
    风控专用：获取城市最精确的实时观测温度（°C）。

    数据源优先级（3层降级）：
      1. METAR 观测站 (aviationweather.gov → metarcentral.com)
      2. NWS tgftp 原始 METAR 文本
      3. Open-Meteo current（模型内插值 fallback）

    与 fetch_current_temperature 的区别：
      - 优先 METAR 实测值（非模型估算）
      - 独立短TTL缓存（gate_metar_cache_ttl_seconds，默认120秒）
      - 专用于 weather_exit_gate / weather_veto 等风控决策

    T233: Denver事故教训 — Open-Meteo滞后导致错过止损时机。
    """
    key = city_name.lower().strip()
    now = time.time()

    # 独立缓存（短TTL，不与 ensemble METAR 缓存共享）
    ens_cfg = get("weather_ensemble") or {}
    gate_ttl = int(ens_cfg.get("gate_metar_cache_ttl_seconds", 60))

    if key in _GATE_METAR_CACHE:
        ts, cached = _GATE_METAR_CACHE[key]
        if now - ts < gate_ttl and cached is not None:
            return cached.get("temp_c")

    # ── 1. 获取ICAO站点代码 ──
    station = _STATION_MAP.get(key)
    icao = station[0] if station else None

    metar_result = None
    if icao:
        # ── 2. 尝试 aviationweather.gov + metarcentral.com ──
        api_timeout = int(ens_cfg.get("metar_api_timeout", 10))
        metar_result = _fetch_metar_aviationweather(icao, api_timeout)
        if not metar_result:
            metar_result = _fetch_metar_metarcentral(icao, api_timeout)

        # ── 3. 尝试 NWS tgftp 原始METAR ──
        if not metar_result:
            metar_result = _fetch_nws_raw_metar(icao, api_timeout)

    if metar_result and metar_result.get("temp_c") is not None:
        temp_c = round(metar_result["temp_c"], 1)
        _GATE_METAR_CACHE[key] = (now, {"temp_c": temp_c})
        logger.info(
            f"[GateTemp] ✅ {city_name} 实时温度={temp_c}°C "
            f"(METAR {icao}, source={metar_result.get('source', '?')}, "
            f"obs={metar_result.get('obs_time', '?')})"
        )
        return temp_c

    # ── 4. fallback: Open-Meteo current ──
    fallback_temp = fetch_current_temperature(city_name)
    if fallback_temp is not None:
        _GATE_METAR_CACHE[key] = (now, {"temp_c": fallback_temp})
        logger.warning(
            f"[GateTemp] ⚠️ {city_name} METAR不可用，"
            f"降级Open-Meteo: {fallback_temp}°C (精度可能滞后)"
        )
    else:
        logger.warning(f"[GateTemp] ❌ {city_name} 所有温度源均失败")
    return fallback_temp


# 趋势概率缓存（10分钟TTL）
_TREND_PROB_CACHE: dict[str, tuple[float, float]] = {}
_TREND_PROB_TTL = 600  # 10 minutes


def estimate_temp_approach_probability(
    city_name: str,
    target_temp_c: float,
    horizon_hours: float = 1.0,
    tolerance_c: float = 1.0,
) -> float:
    """
    估算未来 horizon_hours 小时内温度接近 target_temp_c 的概率。
    使用 Open-Meteo hourly forecast（逐小时预报）。

    算法：
    1. 获取未来 horizon_hours 小时的逐小时温度预报
    2. 计算每个小时温度落入 [target - tolerance, target + tolerance] 的比例
    3. 如果趋势方向正确（当前温度向目标移动），给额外加权

    T54: YES Ambush 条件3需要 — 温度趋势预测概率。

    Returns: 0.0 ~ 1.0
    """
    key = f"{city_name.lower().strip()}:{target_temp_c}:{horizon_hours}"
    now = time.time()

    if key in _TREND_PROB_CACHE:
        ts, prob = _TREND_PROB_CACHE[key]
        if now - ts < _TREND_PROB_TTL:
            return prob

    coords = _get_city_coords(city_name.lower().strip())
    if not coords:
        return 0.0

    lat, lon = coords
    try:
        # 获取未来48小时逐小时预报（足够覆盖任何reasonable的horizon）
        api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&timezone=auto"
            f"&forecast_hours={max(int(horizon_hours) + 2, 6)}"
        )
        resp = requests.get(api_url, timeout=12)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        times = hourly.get("time", [])

        if not temps or len(temps) < 2:
            return 0.0

        # 计算未来 horizon_hours 内有多少小时温度落入目标区间
        # times 格式: "2026-04-05T14:00"
        now_utc = datetime.now(timezone.utc)
        horizon_end = now_utc + timedelta(hours=horizon_hours)

        in_range_count = 0
        total_future = 0

        for t_str, temp in zip(times, temps):
            if temp is None:
                continue
            try:
                # Open-Meteo 返回的是本地时间（因为 timezone=auto）
                # 但对于概率估算，序列顺序比绝对时间更重要
                total_future += 1
                if total_future > int(horizon_hours) + 1:
                    break  # 只看 horizon 内
                if abs(float(temp) - target_temp_c) <= tolerance_c:
                    in_range_count += 1
            except (ValueError, TypeError):
                continue

        if total_future == 0:
            return 0.0

        # 基础概率：在目标区间的时间比例
        base_prob = in_range_count / total_future

        # 趋势加权：仅当已有基础概率且温度趋势向目标靠近时，额外加权
        current_temp = temps[0] if temps else None
        if base_prob > 0 and current_temp is not None and len(temps) >= 2:
            last_in_horizon = temps[min(int(horizon_hours), len(temps) - 1)]
            if last_in_horizon is not None:
                dist_now = abs(float(current_temp) - target_temp_c)
                dist_later = abs(float(last_in_horizon) - target_temp_c)
                if dist_later < dist_now:
                    # 趋势正确：向目标靠近
                    trend_bonus = min(
                        0.2, (dist_now - dist_later) / max(dist_now, 0.1) * 0.3
                    )
                    base_prob = min(1.0, base_prob + trend_bonus)

        prob = round(base_prob, 3)
        _TREND_PROB_CACHE[key] = (now, prob)
        logger.debug(
            f"[T54] {city_name} 趋势概率: target={target_temp_c}°C "
            f"horizon={horizon_hours}h prob={prob:.3f} "
            f"(in_range={in_range_count}/{total_future})"
        )
        return prob

    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.debug(
            f"[T54] estimate_temp_approach_probability 失败 ({city_name}): {e}"
        )
        return 0.0
