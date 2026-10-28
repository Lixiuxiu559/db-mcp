"""PostgreSQL 工具：只读查询、写操作、元数据浏览。

安全模型（三层，缺一不可）：
1. **实例权限**：`permissions: read` 的实例，所有写工具直接拒绝（本模块校验）
2. **SQL 静态分析**：sqlguard 拒绝多语句、写关键字、危险函数、SELECT INTO 等
3. **只读事务兜底**：read 实例的查询跑在 `BEGIN READ ONLY` 里，即便 1/2 漏判，
   PostgreSQL 自己会拒绝写入（见 db/pg.py）

参数化执行：`params` 支持占位符传值（asyncpg 用 $1/$2），避免把用户输入拼进 SQL。
"""

from __future__ import annotations

import asyncio
import time

from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from ..config import PostgresInstance
from ..db.pool import as_timeout_error
from ..dependencies import get_state
from ..errors import (
    ConfirmationRequiredError,
    ReadOnlySQLRequiredError,
    StatementNotAllowedError,
)
from ..models import (
    ColumnInfo,
    PgDescribeResult,
    PgExecuteResult,
    PgIndexInfo,
    PgIndexListResult,
    PgQueryResult,
    PgTableListResult,
    SchemaListResult,
    TableInfo,
)
from ..sqlguard import analyze
from ..state import DbState
from ._render import render_table, truncate_text

# 只读查询超时上限（元数据查询很快，但大表 SELECT 可能慢）
_DEFAULT_READ_TIMEOUT = 60

_TABLE_TYPE_MAP = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "FOREIGN": "foreign_table",
}


def _pg(state: DbState, name: str) -> PostgresInstance:
    """取实例并确认它是 postgres 类型。"""
    inst = state.config.resolve_instance(name)
    if not isinstance(inst, PostgresInstance):
        raise StatementNotAllowedError(
            f"实例 {name!r} 是 {inst.type} 类型，PostgreSQL 工具不能用于它。"
            "请改用 list_instances 查看有哪些 PostgreSQL 实例。"
        )
    return inst


def _require_write(state: DbState, name: str, inst: PostgresInstance) -> None:
    if inst.permissions != "write":
        raise ReadOnlySQLRequiredError(
            f"实例 {name!r} 的权限为 read，禁止执行写操作。"
            "请在权限为 write 的实例上执行，或用管理端把该实例改为 write。"
        )


def register(mcp: FastMCP) -> None:
    # ==================== postgres_query ====================
    @mcp.tool
    async def postgres_query(
        instance: str,
        sql: str,
        params: list[object] | None = None,
        timeout_seconds: int = _DEFAULT_READ_TIMEOUT,
        state: DbState = Depends(get_state),
    ) -> PgQueryResult:
        """在 PostgreSQL 实例上执行只读查询（SELECT / WITH / SHOW / EXPLAIN / TABLE / VALUES）。

        结果以 Markdown 表格返回；超过服务端行数上限会被截断。
        写操作会被拒绝——请改用 postgres_execute。

        Args:
            instance: 实例名称（先用 list_instances 获取）。
            sql: 只读 SQL 语句。只允许单条语句，且不得包含写关键字。
            params: 可选参数列表，对应 SQL 里的 $1/$2 占位符（推荐用它传值，不要拼接字符串）。
            timeout_seconds: 超时秒数（受服务端上限约束）。

        Returns:
            列名、数据行、行数与耗时。

        Examples:
            - "查 pg-main 的 users 表前 10 行" → postgres_query(instance="pg-main", sql="SELECT * FROM users LIMIT 10")
            - "按 id 查" → postgres_query(instance="pg-main", sql="SELECT * FROM users WHERE id = $1", params=[42])

        Error Handling:
            - 实例不存在/已禁用 → 提示可用实例
            - 语句含写操作或多语句 → 说明原因并建议用 postgres_execute
            - 实例权限不足 → 提示只读实例不能写
        """
        inst = _pg(state, instance)
        limits = state.config.limits

        analysis = analyze(sql)
        if not analysis.is_read:
            if analysis.is_write:
                raise ReadOnlySQLRequiredError(
                    f"postgres_query 仅允许只读查询，检测到写操作（{analysis.reason}）。"
                    "如需写入请用 postgres_execute。"
                )
            raise StatementNotAllowedError(f"SQL 被安全策略拒绝: {analysis.reason}")

        timeout = min(max(1, timeout_seconds), limits.max_query_timeout_seconds)
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _execute_read(state, instance, inst, sql, params, limits.max_rows),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise as_timeout_error(instance, timeout) from None
        duration = round((time.monotonic() - start) * 1000, 1)

        text = f"**{instance}** 查询成功，返回 {result.row_count} 行"
        if result.truncated:
            text += f"（超过上限 {limits.max_rows} 行，已截断；请加 LIMIT/WHERE 收窄）"
        text += f"\n\n{render_table(result.columns, result.rows)}"
        text, text_truncated = truncate_text(
            text, limits.max_result_chars, hint="请收窄查询范围。"
        )

        return PgQueryResult(
            instance=instance,
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            command=result.command,
            truncated=result.truncated or text_truncated,
            duration_ms=duration,
        )

    # ==================== postgres_execute ====================
    @mcp.tool
    async def postgres_execute(
        instance: str,
        sql: str,
        params: list[object] | None = None,
        confirm: bool = False,
        timeout_seconds: int = _DEFAULT_READ_TIMEOUT,
        state: DbState = Depends(get_state),
    ) -> PgExecuteResult:
        """在 PostgreSQL 实例上执行写操作（INSERT / UPDATE / DELETE / DDL）。

        只读查询请用 postgres_query。**高危结构变更（DROP / TRUNCATE / ALTER）
        必须显式传 confirm=true 才会执行**，以防误操作。

        Args:
            instance: 实例名称，其权限必须为 write。
            sql: 写 SQL 语句（单条）。
            params: 可选参数列表，对应 $1/$2 占位符（推荐）。
            confirm: 高危语句（DROP/TRUNCATE/ALTER）的二次确认开关，默认 false。
            timeout_seconds: 超时秒数（受服务端上限约束）。

        Returns:
            命令类型与影响行数。

        Examples:
            - 插入 → postgres_execute(instance="pg-ops", sql="INSERT INTO t (a) VALUES ($1)", params=[1])
            - 删表（需确认）→ postgres_execute(instance="pg-ops", sql="DROP TABLE t", confirm=True)

        Error Handling:
            - 实例权限为 read → 直接拒绝
            - 高危语句未确认 → 提示加 confirm=true
            - 多语句 → 拒绝（请拆成多次调用）
        """
        inst = _pg(state, instance)
        _require_write(state, instance, inst)
        limits = state.config.limits

        analysis = analyze(sql)
        if not analysis.is_write:
            if analysis.is_read:
                raise StatementNotAllowedError(
                    "postgres_execute 用于写操作，检测到只读语句。请改用 postgres_query。"
                )
            raise StatementNotAllowedError(f"SQL 被安全策略拒绝: {analysis.reason}")
        if analysis.is_high_risk and not confirm:
            raise ConfirmationRequiredError(
                f"该语句属于高危结构变更（{analysis.first}），执行前请确认影响范围，"
                "确认无误后请以 confirm=true 重新调用。"
            )

        timeout = min(max(1, timeout_seconds), limits.max_query_timeout_seconds)
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _execute_write(state, instance, inst, sql, params, limits.max_rows),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise as_timeout_error(instance, timeout) from None
        duration = round((time.monotonic() - start) * 1000, 1)
        return PgExecuteResult(
            instance=instance,
            command=result.command,
            row_count=result.row_count,
            rows=result.rows,
            columns=result.columns,
            duration_ms=duration,
        )

    # ==================== postgres_list_schemas ====================
    @mcp.tool
    async def postgres_list_schemas(
        instance: str,
        state: DbState = Depends(get_state),
    ) -> SchemaListResult:
        """列出 PostgreSQL 实例里的所有 schema（排除系统 schema）。

        Args:
            instance: 实例名称。
        """
        inst = _pg(state, instance)
        sql = (
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
            "AND schema_name NOT LIKE 'pg_toast%' AND schema_name NOT LIKE 'pg_temp%' "
            "ORDER BY schema_name"
        )
        result = await _run_meta(state, instance, inst, sql)
        return SchemaListResult(
            instance=instance, schemas=[row[0] for row in result.rows]
        )

    # ==================== postgres_list_tables ====================
    @mcp.tool
    async def postgres_list_tables(
        instance: str,
        schema: str | None = None,
        state: DbState = Depends(get_state),
    ) -> PgTableListResult:
        """列出 PostgreSQL 实例某 schema 下的表与视图。

        Args:
            instance: 实例名称。
            schema: schema 名称；不填则用实例配置里的默认 schema（通常 public）。
        """
        inst = _pg(state, instance)
        target = schema or inst.schema_
        sql = """
            SELECT c.relname,
                   CASE c.relkind
                     WHEN 'r' THEN 'BASE TABLE' WHEN 'p' THEN 'BASE TABLE'
                     WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                     WHEN 'f' THEN 'FOREIGN' ELSE 'OTHER' END AS kind,
                   c.reltuples::bigint AS row_estimate
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            ORDER BY c.relname
        """
        result = await _run_meta(state, instance, inst, sql, [target])
        tables = [
            TableInfo(
                schema_name=target,
                name=row[0],
                type=_TABLE_TYPE_MAP.get(row[1], "other"),  # type: ignore[arg-type]
                row_estimate=int(row[2]) if row[2] is not None and int(row[2]) >= 0 else None,
            )
            for row in result.rows
        ]
        return PgTableListResult(instance=instance, schema_name=target, tables=tables)

    # ==================== postgres_describe_table ====================
    @mcp.tool
    async def postgres_describe_table(
        instance: str,
        table: str,
        schema: str | None = None,
        state: DbState = Depends(get_state),
    ) -> PgDescribeResult:
        """查看表结构：列名、类型、是否可空、默认值与注释。

        Args:
            instance: 实例名称。
            table: 表名。
            schema: schema 名称；不填则用实例默认 schema。
        """
        inst = _pg(state, instance)
        target = schema or inst.schema_
        sql = """
            SELECT a.attname,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   NOT a.attnotnull AS nullable,
                   pg_get_expr(d.adbin, d.adrelid) AS default_value,
                   col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """
        result = await _run_meta(state, instance, inst, sql, [target, table])
        columns = [
            ColumnInfo(
                name=row[0],
                data_type=row[1],
                nullable=bool(row[2]),
                default=row[3],
                comment=row[4],
            )
            for row in result.rows
        ]
        return PgDescribeResult(
            instance=instance,
            schema_name=target,
            table=table,
            exists=bool(columns),
            columns=columns,
        )

    # ==================== postgres_list_indexes ====================
    @mcp.tool
    async def postgres_list_indexes(
        instance: str,
        table: str,
        schema: str | None = None,
        state: DbState = Depends(get_state),
    ) -> PgIndexListResult:
        """查看某张表的索引：名称、列、是否唯一/主键、完整定义。

        Args:
            instance: 实例名称。
            table: 表名。
            schema: schema 名称；不填则用实例默认 schema。
        """
        inst = _pg(state, instance)
        target = schema or inst.schema_
        sql = """
            SELECT i.relname AS index_name,
                   ix.indisunique AS is_unique,
                   ix.indisprimary AS is_primary,
                   pg_get_indexdef(ix.indexrelid) AS definition,
                   array_to_string(
                       ARRAY(SELECT pg_get_indexdef(ix.indexrelid, k + 1, true)
                             FROM generate_subscripts(ix.indkey, 1) AS k
                             ORDER BY k), ',') AS columns
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = $1 AND t.relname = $2
            ORDER BY i.relname
        """
        result = await _run_meta(state, instance, inst, sql, [target, table])
        indexes = [
            PgIndexInfo(
                name=row[0],
                unique=bool(row[1]),
                primary=bool(row[2]),
                definition=row[3],
                columns=[c for c in (row[4] or "").split(",") if c],
            )
            for row in result.rows
        ]
        return PgIndexListResult(
            instance=instance, schema_name=target, table=table, indexes=indexes
        )


# ---------------------------------------------------------------- 内部执行


async def _execute_read(
    state: DbState, name: str, inst: PostgresInstance, sql: str, params, max_rows: int
):
    """执行一条只读查询。

    **`read_only=True` 是"这条 SQL 走只读执行路径"，与实例权限无关**——
    只读路径会走流式游标取行（拿到 columns/rows），写路径走 `execute()`
    只拿命令状态串（没有行数据）。曾经错写成 `read_only=inst.permissions == "read"`，
    导致 **write 实例上的查询全部返回空数据**（row_count 有值但 rows/columns 为空），
    因为 write 实例的 SELECT 被当写语句执行了。
    """
    from ..db import pg as pgmod

    async def op(pool):
        return await pgmod.run(pool, sql, params, read_only=True, max_rows=max_rows)

    return await state.pool.run_with_retry(name, inst, op)


async def _execute_write(
    state: DbState, name: str, inst: PostgresInstance, sql: str, params, max_rows: int
):
    from ..db import pg as pgmod

    async def op(pool):
        return await pgmod.run(pool, sql, params, read_only=False, max_rows=max_rows)

    return await state.pool.run_with_retry(name, inst, op)


async def _run_meta(
    state: DbState, name: str, inst: PostgresInstance, sql: str, params: list | None = None
):
    """元数据查询：一律走只读路径（即使实例是 write 权限）。"""
    limits = state.config.limits
    return await asyncio.wait_for(
        _execute_read(state, name, inst, sql, params, limits.max_rows),
        timeout=limits.query_timeout_seconds,
    )
