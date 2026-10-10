"""
database.py — PostgreSQL 数据库连接管理层

替代原 sqlite3 直连，提供统一的连接池管理、事务控制和多租户 RLS 支持。

兼容性设计:
  - connect() 返回的包装器自动将 SQLite ? 占位符转换为 %s
  - 自动将 :name 具名参数转换为 %(name)s
  - executescript() 兼容 SQLite 的多语句执行
  - 查询结果支持 dict 访问（兼容 sqlite3.Row）
  - connect(db_path=...) 参数被忽略（向后兼容）

连接方式:
  - 环境变量 DATABASE_URL (优先): 完整 PostgreSQL URL
  - 环境变量 PG_* (次选): 分离式配置
  - 默认: localhost:5432, database=polymarket_v2

多租户:
  - 通过 PostgreSQL RLS (Row-Level Security) 实现数据库级隔离
  - connect(wallet_address=...) 设置 app.current_wallet 会话变量
"""

import logging
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# ── 连接池（线程安全） ──────────────────────────────────────────────
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

# SQLite → PostgreSQL 参数转换
_SQLITE_PARAM_RE = re.compile(r"\?")
_SQLITE_NAMED_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _convert_params(sql: str, params=None):
    """将 SQLite 风格的 SQL 参数转换为 PostgreSQL 风格。

    转换规则:
    1. ? → %s （位置参数，当 params 是 tuple/list 时）
    2. :name → %(name)s （具名参数，当 params 是 dict 时）
    """
    if sql is None:
        return sql, params

    if isinstance(params, dict):
        converted = _SQLITE_NAMED_RE.sub(r"%(\1)s", sql)
        return converted, params

    if params is not None:
        converted = _SQLITE_PARAM_RE.sub("%s", sql)
        return converted, params

    return sql, params


def _adapt_ddl(sql: str) -> str:
    """将 SQLite DDL 语法适配为 PostgreSQL。"""
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "SERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"INSERT\s+OR\s+IGNORE",
        "INSERT",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _get_dsn() -> str:
    """从环境变量构建 PostgreSQL DSN。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    dbname = os.environ.get("PG_DATABASE", "polymarket_v2")
    user = os.environ.get("PG_USER", "polymarket")
    password = os.environ.get("PG_PASSWORD", "")
    sslmode = os.environ.get("PG_SSLMODE", "prefer")
    dsn = f"host={host} port={port} dbname={dbname} user={user} sslmode={sslmode}"
    if password:
        dsn += f" password={password}"
    return dsn


def init_pool(minconn: int = 2, maxconn: int = 20) -> None:
    """初始化连接池（幂等，线程安全）。

    Phase-2 调整 — 切到 Gunicorn 多 worker 后，每 worker 都有独立的进程级
    池，因此**单 worker 上限**必须缩小，避免总连接数超过 PostgreSQL 的
    max_connections（默认 100/120）。

    预算：2 worker × maxconn=20 = 40 连接（含 idle）；4 worker × 20 = 80。
    PG_POOL_MIN/MAX 通过 systemd EnvironmentFile/Environment 覆盖。
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        try:
            env_min = int(os.environ.get("PG_POOL_MIN", "") or minconn)
        except Exception:
            env_min = minconn
        try:
            env_max = int(os.environ.get("PG_POOL_MAX", "") or maxconn)
        except Exception:
            env_max = maxconn
        env_min = max(1, env_min)
        env_max = max(env_min + 1, env_max)
        dsn = _get_dsn()
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=env_min,
            maxconn=env_max,
            dsn=dsn,
        )
        logger.info(f"PostgreSQL 连接池已初始化 (min={env_min}, max={env_max})")


def close_pool() -> None:
    """关闭连接池。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            logger.info("PostgreSQL 连接池已关闭")


def _ensure_pool():
    """确保连接池已初始化。"""
    if _pool is None:
        init_pool()


# Phase-2: 池使用量监控（仅在高水位时打印，避免日志噪音）
_POOL_HIWAT_LOG_RATIO = 0.8


def _maybe_log_pool_pressure(stage: str) -> None:
    p = _pool
    if p is None:
        return
    try:
        # psycopg2.pool.ThreadedConnectionPool 的内部属性
        in_use = len(getattr(p, "_used", {}) or {})
        total = int(getattr(p, "maxconn", 0) or 0)
        if total <= 0:
            return
        if in_use >= int(total * _POOL_HIWAT_LOG_RATIO):
            logger.warning(
                "[DBPool] %s pressure: in_use=%d/%d (>=%.0f%%)",
                stage,
                in_use,
                total,
                _POOL_HIWAT_LOG_RATIO * 100,
            )
    except Exception:
        pass


@contextmanager
def connect(db_path: Optional[Path] = None, wallet_address: Optional[str] = None):
    """获取 PostgreSQL 连接（上下文管理器）。

    Args:
        db_path: 忽略（保持与旧 SQLite API 签名兼容）。
        wallet_address: 设置 RLS 多租户上下文。

    用法（兼容旧 SQLite 代码）:
        with connect() as conn:
            conn.execute("SELECT * FROM paper_bets WHERE bet_id=?", (bid,))

    返回 _ConnWrapper，自动转换 SQLite ? 参数为 %s。
    """
    _ensure_pool()
    _maybe_log_pool_pressure("getconn")
    conn = _pool.getconn()
    try:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        if wallet_address:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.current_wallet = %s", (wallet_address,))
        yield _ConnWrapper(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def db_connect(wallet_address: Optional[str] = None):
    """Dashboard 路由使用的连接上下文。"""
    with connect(wallet_address=wallet_address) as conn:
        yield conn


class _ConnWrapper:
    """连接包装器：SQLite → PostgreSQL 自动兼容层。

    核心功能：
    - 自动将 ? 参数转为 %s
    - 自动将 :name 参数转为 %(name)s
    - 支持 conn.execute() / conn.executescript()
    - PRAGMA 查询自动转换
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=None):
        """执行 SQL，自动转换 SQLite 参数风格。"""
        # 处理 PRAGMA table_info → information_schema
        pragma_match = re.match(r"\s*PRAGMA\s+table_info\((\w+)\)", sql, re.IGNORECASE)
        if pragma_match:
            table = pragma_match.group(1)
            sql = (
                "SELECT column_name AS name, data_type AS type "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "ORDER BY ordinal_position"
            )
            params = (table,)
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur

        # SQLite 日期函数 → PostgreSQL 等价
        # date(col, 'unixepoch') → to_timestamp(col)::date
        sql = re.sub(
            r"date\((\w+),\s*'unixepoch'\)",
            r"to_timestamp(\1)::date",
            sql,
            flags=re.IGNORECASE,
        )
        # datetime(col, 'unixepoch') → to_timestamp(col)
        sql = re.sub(
            r"datetime\((\w+),\s*'unixepoch'\)",
            r"to_timestamp(\1)",
            sql,
            flags=re.IGNORECASE,
        )
        # datetime(COALESCE(...)) → COALESCE(...)::timestamp
        sql = re.sub(
            r"datetime\(COALESCE\(([^)]+)\)\)",
            r"COALESCE(\1)::timestamp",
            sql,
            flags=re.IGNORECASE,
        )

        # SQLite INSERT OR IGNORE → PostgreSQL INSERT ... ON CONFLICT DO NOTHING
        _had_or_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE", sql, re.IGNORECASE))
        _had_or_replace = bool(re.search(r"INSERT\s+OR\s+REPLACE", sql, re.IGNORECASE))
        sql = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO",
            "INSERT INTO",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"INSERT\s+OR\s+REPLACE\s+INTO",
            "INSERT INTO",
            sql,
            flags=re.IGNORECASE,
        )

        sql, params = _convert_params(sql, params)

        # 添加 ON CONFLICT 子句（如果原语句有 OR IGNORE/REPLACE）
        if _had_or_ignore and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        elif _had_or_replace and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, params_list):
        """批量执行，自动转换参数。"""
        if params_list:
            sql, _ = _convert_params(sql, params_list[0])
        cur = self._conn.cursor()
        cur.executemany(sql, params_list)
        return cur

    def executescript(self, sql: str):
        """执行多条 SQL（兼容 sqlite3.executescript）。"""
        sql = _adapt_ddl(sql)
        cur = self._conn.cursor()
        # psycopg2 不支持一次执行多条语句（部分版本支持），
        # 拆分为单条执行确保兼容性
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def cursor(self):
        return self._conn.cursor()

    @property
    def raw(self):
        """访问底层 psycopg2 连接。"""
        return self._conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ── 辅助函数 ──────────────────────────────────────────────────────────


def get_table_columns(table_name: str) -> list[str]:
    """获取表的列名列表（替代 PRAGMA table_info）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position",
            (table_name,),
        ).fetchall()
        return [r["column_name"] for r in rows]


def table_exists(table_name: str) -> bool:
    """检查表是否存在。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s)",
            (table_name,),
        ).fetchone()
        return row["exists"] if row else False


# ── 错误类型兼容 ─────────────────────────────────────────────────────
OperationalError = psycopg2.OperationalError
ProgrammingError = psycopg2.ProgrammingError
DatabaseError = psycopg2.DatabaseError
IntegrityError = psycopg2.IntegrityError
