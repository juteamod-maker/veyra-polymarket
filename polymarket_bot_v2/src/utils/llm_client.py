"""
llm_client.py — DeepSeek 官方 API 单点封装（T863 + T866）

设计：
- 仅调用 DeepSeek 官方 API（https://api.deepseek.com），OpenAI SDK 兼容。
- 全系统 AI 调用通过本模块统一进出，禁止其他模块直读 base_url / api_key。
- T866：支持 5 个 API key 轮询（DEEPSEEK_API_KEY + DEEPSEEK_API_KEY_1..4），缓解
  并发拥堵；逐 key fail-over，所有 key 都 401/限流才返回 None。
- 模型 ID 归一化：含 "/" 的旧 OpenRouter 风格 ID（如 deepseek/deepseek-chat-v3-0324、qwen/qwen3-8b、openai/gpt-4o）
  自动映射到 DEFAULT_LLM_MODEL，并打印一次性 warning，避免历史配置导致 400。
- 失败一律静默返回 None，调用方依赖现有的回退/降级分支。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
ENV_KEY = "DEEPSEEK_API_KEY"
ENV_KEY_VARIANTS: tuple[str, ...] = (
    ENV_KEY,
    "DEEPSEEK_API_KEY_0",
    "DEEPSEEK_API_KEY_1",
    "DEEPSEEK_API_KEY_2",
    "DEEPSEEK_API_KEY_3",
    "DEEPSEEK_API_KEY_4",
)

_WARNED_MODELS: set[str] = set()
_WARN_LOCK = threading.Lock()

# T866: 多 key 轮询状态
_RR_LOCK = threading.Lock()
_RR_COUNTER: itertools.count = itertools.count()


def _all_keys() -> list[str]:
    """按照 ENV_KEY_VARIANTS 顺序读取所有非空 key（去重，保留顺序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for name in ENV_KEY_VARIANTS:
        v = (os.environ.get(name) or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _api_key() -> str:
    """向后兼容：返回主 key（DEEPSEEK_API_KEY）；缺失时回退第一个可用 key。"""
    primary = (os.environ.get(ENV_KEY) or "").strip()
    if primary:
        return primary
    pool = _all_keys()
    return pool[0] if pool else ""


def llm_api_key_set() -> bool:
    """是否已配置至少一个 DEEPSEEK_API_KEY*。"""
    return bool(_all_keys())


def _base_url() -> str:
    try:
        from src.utils.config_loader import get  # type: ignore

        cfg = get("ai.base_url")
        if cfg and isinstance(cfg, str) and cfg.strip():
            return cfg.strip()
    except Exception:
        pass
    return DEFAULT_BASE_URL


def normalize_model(model: str | None) -> str:
    """归一化模型名：旧 provider/model 风格统一映射到 DEFAULT_LLM_MODEL。"""
    name = (model or "").strip()
    if not name:
        return DEFAULT_LLM_MODEL
    if "/" in name:
        with _WARN_LOCK:
            if name not in _WARNED_MODELS:
                _WARNED_MODELS.add(name)
                logger.warning(
                    "llm_client: legacy model id %r mapped to %s",
                    name,
                    DEFAULT_LLM_MODEL,
                )
        return DEFAULT_LLM_MODEL
    return name


def _build_client(api_key: str):
    """以指定 api_key 构造一次性 OpenAI 兼容客户端。"""
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        logger.warning("llm_client: openai SDK unavailable: %s", exc)
        return None
    try:
        return OpenAI(api_key=api_key, base_url=_base_url(), max_retries=0, timeout=30)
    except Exception as exc:
        logger.warning("llm_client: client init failed: %s", exc)
        return None


def get_llm_client():
    """返回主 key 对应的 DeepSeek 客户端；未配置返回 None。

    保留向后兼容；新代码应优先使用 chat_completion_json 走轮询路径。
    """
    key = _api_key()
    if not key:
        return None
    return _build_client(key)


def _ordered_keys_for_request() -> list[str]:
    """T866：返回本次请求的 key 尝试顺序（轮询起点 + 顺延）。"""
    pool = _all_keys()
    if not pool:
        return []
    with _RR_LOCK:
        idx = next(_RR_COUNTER) % len(pool)
    return pool[idx:] + pool[:idx]


def chat_completion_json(
    *,
    model: str | None,
    system: str,
    user: str,
    max_tokens: int = 800,
    temperature: float = 0.2,
    timeout: float = 30.0,
) -> Optional[str]:
    """单次 JSON 模式 chat completion；返回 model content 文本（已 strip）或 None。

    T866：依次轮询所有可用 DEEPSEEK_API_KEY*；遇 401/429/网络错误自动切换下一 key。
    任一 key 成功即返回；全部失败返回 None。调用方负责自行 json.loads(content)。
    """
    keys = _ordered_keys_for_request()
    if not keys:
        return None
    use_model = normalize_model(model)
    last_exc: Exception | None = None
    for idx, key in enumerate(keys):
        client = _build_client(key)
        if client is None:
            continue
        try:
            resp = client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
            return content
        except Exception as exc:
            last_exc = exc
            logger.debug(
                "llm_client: key#%d/%d %s call failed: %s",
                idx + 1,
                len(keys),
                use_model,
                exc,
            )
            continue
    if last_exc is not None:
        logger.warning(
            "llm_client: all %d DeepSeek keys failed for model %s: %s",
            len(keys),
            use_model,
            last_exc,
        )
    return None


def llm_key_pool_size() -> int:
    """T866：当前可用 DeepSeek key 数量（用于状态/诊断接口）。"""
    return len(_all_keys())


__all__ = [
    "DEFAULT_LLM_MODEL",
    "DEFAULT_BASE_URL",
    "ENV_KEY",
    "ENV_KEY_VARIANTS",
    "llm_api_key_set",
    "llm_key_pool_size",
    "get_llm_client",
    "chat_completion_json",
    "normalize_model",
]
