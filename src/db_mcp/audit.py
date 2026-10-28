"""审计：SQLite 存储 + 统一的工具调用中间件。

决策回顾：仅审计、不拦截。每次工具调用记一条。

与 ssh-mcp 的差异：本服务**记录 SQL / Redis 命令明文**（超长截断），
因为排查数据库问题必须知道"执行的到底是什么语句"。语句里可能含业务数据，
但管理端仅 admin 可见，且导出需 admin token。密码等凭据绝不入库。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

_PRUNE_INTERVAL_SECONDS = 3600  # 清理节流：最多每小时一次

_FIELDS = (
    "ts",
    "user",
    "tool",
    "instance",
    "kind",
    "statement",
    "statement_truncated",
    "row_count",
    "request_id",
    "status",
    "duration_ms",
    "error",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS audit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {_FIELDS[0]}   TEXT NOT NULL,
    {_FIELDS[1]}   TEXT,
    {_FIELDS[2]}   TEXT,
    {_FIELDS[3]}   TEXT,
    {_FIELDS[4]}   TEXT,
    {_FIELDS[5]}   TEXT,
    {_FIELDS[6]}   INTEGER,
    {_FIELDS[7]}   INTEGER,
    {_FIELDS[8]}   TEXT,
    {_FIELDS[9]}   TEXT,
    {_FIELDS[10]}  REAL,
    {_FIELDS[11]}  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_records(ts);
CREATE INDEX IF NOT EXISTS idx_audit_instance ON audit_records(instance);
"""

_INSERT_SQL = (
    f"INSERT INTO audit_records ({','.join(_FIELDS)}) "
    f"VALUES ({','.join('?' * len(_FIELDS))})"
)


def current_user() -> str:
    """当前认证用户（StaticTokenVerifier 的 client_id），取不到则为 anonymous。"""
    try:
        token = get_access_token()
        return token.client_id if token else "anonymous"
    except Exception:  # noqa: BLE001 - 审计兜底：任何认证上下文异常都记为 anonymous
        return "anonymous"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Redis 工具名 -> 审计里展示的命令名，让审计读起来像真实 Redis 命令
_REDIS_CMD_NAMES = {
    "redis_get": "GET",
    "redis_mget": "MGET",
    "redis_set": "SET",
    "redis_del": "DEL",
    "redis_expire": "EXPIRE",
    "redis_ttl": "TTL",
    "redis_exists": "EXISTS",
    "redis_type": "TYPE",
    "redis_keys": "KEYS",
    "redis_scan": "SCAN",
    "redis_list_range": "LRANGE",
    "redis_list_push": "RPUSH",
    "redis_list_pop": "LPOP",
    "redis_hash_get": "HGETALL",
    "redis_hash_set": "HSET",
    "redis_hash_del": "HDEL",
    "redis_set_members": "SMEMBERS",
    "redis_set_add": "SADD",
    "redis_set_remove": "SREM",
    "redis_zset_range": "ZRANGE",
    "redis_zset_add": "ZADD",
    "redis_zset_remove": "ZREM",
}


def _statement_from_args(args: dict[str, Any]) -> str | None:
    """从工具参数里拼出"人可读的语句"。"""
    if isinstance(args.get("sql"), str):
        return args["sql"]
    if isinstance(args.get("command"), str):
        return args["command"]

    tool = str(args.get("__tool__") or "")
    cmd = _REDIS_CMD_NAMES.get(tool)
    if cmd is None:
        return None
    # 展示成 `CMD <第一个非实例参数>` 的形态：GET user:1 / HSET user:1 / KEYS user:*
    target = args.get("key") or args.get("pattern") or args.get("keys")
    if target is None:
        return cmd
    return f"{cmd} {target}"


class AuditLogger:
    """SQLite 审计日志：写入即落盘，按保留期自动清理，支持分页查询。"""

    def __init__(self, path: Path, retention_days: int = 30, max_statement_chars: int = 4000):
        self.path = Path(path)
        self.retention_days = max(1, retention_days)
        self.max_statement_chars = max(64, max_statement_chars)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._conn: sqlite3.Connection | None = None
        with self._lock:
            self._prune(force=True)

    def _connection(self) -> sqlite3.Connection:
        """惰性建连：close 之后再次写入会自动重开（调用方需持有 self._lock）。"""
        if self._conn is None:
            # check_same_thread=False + 显式加锁：允许在事件循环线程与 to_thread 间共用连接
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
            self._conn = conn
        return self._conn

    async def log(self, record: dict[str, Any]) -> None:
        row = [record.get(name) for name in _FIELDS]
        with self._lock:
            self._connection().execute(_INSERT_SQL, row)
            self._connection().commit()
            self._prune()

    def _prune(self, force: bool = False) -> None:
        """删除超过保留期的记录；调用方需持有 self._lock。"""
        now = time.monotonic()
        if not force and now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        cutoff = (_utc_now() - timedelta(days=self.retention_days)).isoformat(
            timespec="milliseconds"
        )
        conn = self._connection()
        conn.execute("DELETE FROM audit_records WHERE ts < ?", (cutoff,))
        conn.commit()
        self._last_prune = now

    def _filter_clause(
        self,
        instance: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since: str | None = None,
    ) -> tuple[str, list[Any]]:
        """构造 WHERE 子句：实例 / 状态 / 关键词（语句·工具·用户）/ 时间下界。"""
        where: list[str] = []
        params: list[Any] = []
        if instance:
            where.append("instance = ?")
            params.append(instance)
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("ts >= ?")
            params.append(since)
        if q:
            like = f"%{q}%"
            where.append(
                "(statement LIKE ? OR tool LIKE ? OR user LIKE ? OR instance LIKE ?)"
            )
            params.extend([like] * 4)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return clause, params

    def query(
        self,
        limit: int = 20,
        offset: int = 0,
        instance: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页查询，返回 (记录, 总数)；记录按时间倒序（新 → 旧）。"""
        clause, params = self._filter_clause(instance, status, q, since)
        with self._lock:
            conn = self._connection()
            total = conn.execute(
                f"SELECT COUNT(*) FROM audit_records{clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM audit_records{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        # sqlite3.Row 迭代得到的是值而非键，必须显式取 .keys()
        records = [{k: row[k] for k in row.keys() if k != "id"} for row in rows]  # noqa: SIM118
        return records, total

    def export_rows(
        self,
        instance: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since: str | None = None,
        cap: int = 50_000,
    ) -> list[dict[str, Any]]:
        """导出用：按同样条件取全量（带上限保护），新 → 旧。"""
        clause, params = self._filter_clause(instance, status, q, since)
        with self._lock:
            rows = self._connection().execute(
                f"SELECT * FROM audit_records{clause} ORDER BY id DESC LIMIT ?",
                [*params, cap],
            ).fetchall()
        return [{k: row[k] for k in row.keys() if k != "id"} for row in rows]  # noqa: SIM118

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class AuditMiddleware(Middleware):
    """在 on_call_tool 钩子统一记录，一处覆盖所有工具（含未来新增）。"""

    def __init__(self, audit: AuditLogger):
        self._audit = audit

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        args: dict[str, Any] = dict(message.arguments or {})
        args["__tool__"] = message.name

        # 明文语句由参数派生：SQL 工具取 sql、Redis 工具按命令名拼接，
        # 取不到（如 list_instances）就只记元数据
        statement = _statement_from_args(args)
        truncated_flag: int | None = None
        if statement is not None:
            if len(statement) > self._audit.max_statement_chars:
                statement = statement[: self._audit.max_statement_chars]
                truncated_flag = 1
            else:
                truncated_flag = 0

        record: dict[str, Any] = {
            "ts": _utc_now().isoformat(timespec="milliseconds"),
            "user": current_user(),
            "tool": message.name,
            "instance": args.get("instance"),
            "kind": _kind_of(message.name),
            "statement": statement,
            "statement_truncated": truncated_flag,
        }
        if context.fastmcp_context is not None:
            try:
                record["request_id"] = context.fastmcp_context.request_id
            except Exception:  # noqa: BLE001, S110 - 可选字段，尽力而为
                pass

        start = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        else:
            record["status"] = "ok"
            record["row_count"] = _row_count_of(result)
            return result
        finally:
            record["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
            await self._audit.log(record)


def _kind_of(tool: str) -> str:
    if tool.startswith("postgres"):
        return "sql"
    if tool.startswith("redis"):
        return "redis"
    return "meta"


def _row_count_of(result: Any) -> int | None:
    """从工具返回值里尽力取 row_count（结构化输出为 pydantic 模型时为 int）。"""
    try:
        data = getattr(result, "data", None)
        value = getattr(data, "row_count", None)
        if isinstance(value, int):
            return value
        value = getattr(data, "deleted", None)
        if isinstance(value, int):
            return value
    except Exception:  # noqa: BLE001, S110 - 可选字段，尽力而为
        pass
    return None
