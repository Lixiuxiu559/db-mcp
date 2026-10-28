"""PostgreSQL 查询执行封装。

两件事在这里统一处理，避免散落到各工具里：

1. **只读事务**：read 实例的查询一律跑在 `BEGIN ... READ ONLY` 事务里。
   这是 sqlguard 静态分析之外的最后一道防线——即便静态分析漏判，
   PostgreSQL 自己会以 `ERROR: cannot execute ... in a read-only transaction`
   拒绝写入。管理端/工具层再把这条原始报错翻译成可读提示。
2. **流式截断**：只读查询用服务端游标只取 `max_rows + 1` 行，避免
   `SELECT * FROM 大表` 把整表读进内存；多取的那 1 行用来判断"是否被截断"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from ..errors import PermissionDeniedError, StatementNotAllowedError

# 只读事务里被 PostgreSQL 拒绝时的报错特征，用于翻译成可读提示
_READONLY_MARKERS = ("read-only transaction", "cannot execute", "is read-only")


@dataclass
class PgResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0  # 实际返回/影响的行数
    command: str = ""
    truncated: bool = False


def _command_of(sql: str) -> str:
    from ..sqlguard import tokenize

    tokens = tokenize(sql)
    return tokens[0] if tokens else ""


def _translate_error(sql: str, exc: Exception) -> Exception:
    """把驱动的原始报错翻译成面向 LLM 的可读错误。"""
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in _READONLY_MARKERS):
        return PermissionDeniedError(
            "该实例权限为 read，数据库拒绝了这条写操作"
            f"（{text}）。写操作请在 permissions: write 的实例上执行。"
        )
    if isinstance(exc, asyncpg.PostgresSyntaxError):
        return StatementNotAllowedError(f"SQL 语法错误: {text}")
    return exc


async def run(
    pool: Any,
    sql: str,
    params: list[Any] | None,
    *,
    read_only: bool,
    max_rows: int,
) -> PgResult:
    """执行一条 SQL。调用方负责权限校验与超时控制。"""
    args = list(params or [])
    try:
        async with pool.acquire() as conn:
            if read_only:
                return await _run_read(conn, sql, args, max_rows)
            return await _run_write(conn, sql, args)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise _translate_error(sql, exc) from exc


async def _run_read(conn: Any, sql: str, args: list[Any], max_rows: int) -> PgResult:
    """只读查询：prepare 拿列名，游标只取 max_rows + 1 行（多取 1 行判断截断）。

    注意 asyncpg 的游标用法：`conn.cursor(...)` 返回的是 **CursorFactory**，
    必须用 `async for` 迭代（它没有 `async with` / `fetch(n)`）。这里把它当
    异步迭代器用，取够 max_rows + 1 行就 break，避免 `SELECT * FROM 大表`
    把整表读进内存。

    先 prepare 再 cursor：cursor 内部会复用已缓存的 prepared statement，
    不会重复解析；列名只能从 prepared statement 的 get_attributes() 拿到
    （Cursor 本身不暴露，且结果为空时无从取键）。
    """
    async with conn.transaction(readonly=True):
        stmt = await conn.prepare(sql)
        columns = [attr.name for attr in stmt.get_attributes()]
        found: list[Any] = []
        async for record in conn.cursor(sql, *args):
            found.append(record)
            if len(found) > max_rows:
                break

    truncated = len(found) > max_rows
    rows = [list(r.values()) for r in found[:max_rows]]
    return PgResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        command=_command_of(sql),
        truncated=truncated,
    )


async def _run_write(conn: Any, sql: str, args: list[Any]) -> PgResult:
    from ..sqlguard import tokenize

    has_returning = "RETURNING" in set(tokenize(sql))
    async with conn.transaction():
        if has_returning:
            records = await conn.fetch(sql, *args)
            rows = [list(r.values()) for r in records]
            columns = list(records[0].keys()) if records else []
            return PgResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                command=_command_of(sql),
            )
        status = await conn.execute(sql, *args)
        # asyncpg 的 execute 返回命令状态串，形如 "UPDATE 3" / "CREATE TABLE" / "SELECT 5"
        parts = (status or "").split()
        row_count = int(parts[-1]) if parts and parts[-1].isdigit() else 0
        command = command_from_status(status, sql)
        return PgResult(columns=[], rows=[], row_count=row_count, command=command)


def command_from_status(status: str | None, sql: str) -> str:
    """从 asyncpg 的命令状态串提取命令名，兜底取 SQL 首个 token。"""
    if status:
        parts = status.split()
        # 状态串末尾是行数（若有），命令名可能含空格（如 "CREATE TABLE"）
        if parts and parts[-1].isdigit():
            parts = parts[:-1]
        if parts:
            return " ".join(parts)
    return _command_of(sql)
