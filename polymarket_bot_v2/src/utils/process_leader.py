"""process_leader.py — 跨 Gunicorn worker 的 leader 选举（基于 fcntl 文件锁）

Phase-2 切到 Gunicorn -w N 后，每个 worker 都是独立进程，模块级
`_warmer_started` 标志各自独立。如果不加约束，N 份 warmer 线程会同时打
Polymarket 上游，造成 N 倍流量、限流、缓存命中下降。

`try_acquire_leader(name)` 在系统级文件上做 `fcntl.flock(LOCK_EX|LOCK_NB)`：
- 第一个调用的 worker 拿到锁 → 返回 True，并把 fd 永久持有（不释放）
- 其它 worker 拿不到 → 返回 False，跳过启动 warmer

锁文件默认放在 `${WARMER_LOCK_PATH:-/tmp/veyra_dashboard_warmer.lock}`，
按 name 后缀分文件，互不干扰。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# 持有 fd 直到进程退出，避免被 GC 后释放锁
_held_fds: Dict[str, int] = {}


def _lock_path(name: str) -> Path:
    base = os.environ.get("WARMER_LOCK_PATH", "/tmp/veyra_dashboard_warmer.lock")
    return Path(f"{base}.{name}")


def try_acquire_leader(name: str) -> bool:
    """尝试获取以 `name` 命名的 leader 锁。同进程多次调用幂等返回 True。

    在不支持 fcntl 的平台（如 Windows）上始终返回 True，让单进程开发环境继续工作。
    """
    if name in _held_fds:
        return True

    try:
        import fcntl  # type: ignore
    except Exception:
        # 非 POSIX 环境：默认让 warmer 启动
        return True

    path = _lock_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except Exception as exc:
        logger.warning(
            "[Leader] open lock %s failed: %s — defaulting to leader", path, exc
        )
        return True

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.close(fd)
        except Exception:
            pass
        logger.info(
            "[Leader] %s already held by another worker (pid=%d skipped)",
            name,
            os.getpid(),
        )
        return False
    except Exception as exc:
        logger.warning("[Leader] flock %s failed: %s — defaulting to leader", path, exc)
        try:
            os.close(fd)
        except Exception:
            pass
        return True

    # 写入 pid 便于排障
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except Exception:
        pass

    _held_fds[name] = fd
    logger.info("[Leader] acquired %s (pid=%d)", name, os.getpid())
    return True
