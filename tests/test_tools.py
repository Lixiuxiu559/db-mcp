"""MCP 工具端到端测试：in-memory Client + Fake 驱动。

重点覆盖安全路径：read 实例拒绝写、高危 DDL 需 confirm、危险函数被拒、
只读事务兜底、审计明文落库。
"""

from __future__ import annotations

import pytest
from fastmcp import Client


async def call(gw, name: str, **args):
    async with Client(gw.mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


# ---------------------------------------------------------------- 清单


async def test_list_instances(gw):
    result = await call(gw, "list_instances")
    assert not result.is_error
    names = [i.name for i in result.data.instances]
    assert names == ["pg-read", "pg-write", "redis-read", "redis-write", "pg-disabled"]
    by_name = {i.name: i for i in result.data.instances}
    assert by_name["pg-read"].permissions == "read"
    assert by_name["pg-write"].permissions == "write"
    assert by_name["pg-disabled"].enabled is False
    # 绝不泄露密码
    assert "pg-pw" not in str(result.data)


async def test_db_ping_pg(gw):
    result = await call(gw, "db_ping", instance="pg-read")
    assert not result.is_error
    assert result.data.status == "ok"
    assert "PostgreSQL" in result.data.version


async def test_db_ping_redis(gw):
    result = await call(gw, "db_ping", instance="redis-read")
    assert not result.is_error
    assert result.data.status == "ok"
    assert result.data.version == "7.2.0"


async def test_db_ping_reports_failure_as_result(gw, harness):
    import asyncpg

    harness.pg_fail_connect = asyncpg.PostgresError("connection refused")
    result = await call(gw, "db_ping", instance="pg-read")
    assert not result.is_error  # 连通性检查自身不报错，结果里带 error
    assert result.data.status == "error"
    assert "refused" in result.data.error


async def test_db_ping_disabled_instance(gw):
    result = await call(gw, "db_ping", instance="pg-disabled")
    assert result.is_error
    assert "已禁用" in result.content[0].text


# ---------------------------------------------------------------- PG 只读


async def test_postgres_query_ok(gw, harness):
    harness.pg_row_map["FROM users"] = [[1, "alice"], [2, "bob"]]
    harness.pg_column_map["FROM users"] = ["id", "name"]
    result = await call(gw, "postgres_query", instance="pg-read", sql="SELECT * FROM users")
    assert not result.is_error
    assert result.data.columns == ["id", "name"]
    assert result.data.rows == [[1, "alice"], [2, "bob"]]
    assert result.data.row_count == 2


async def test_postgres_query_on_write_instance_returns_data(gw, harness):
    """write 实例上的只读查询也必须返回行列数据。

    回归测试：曾把"是否走只读执行路径"错绑到实例权限上
    （`read_only=inst.permissions == "read"`），导致 write 实例的 SELECT
    走了写路径（execute()），row_count 有值但 columns/rows 全空——
    即 write 实例上任何查询都查不到数据。线上实测才发现（read 实例正常，
    write 实例全空）。
    """
    harness.pg_row_map["FROM users"] = [[1, "alice"]]
    harness.pg_column_map["FROM users"] = ["id", "name"]
    result = await call(gw, "postgres_query", instance="pg-write", sql="SELECT * FROM users")
    assert not result.is_error
    assert result.data.columns == ["id", "name"], "write 实例的查询丢失了列名"
    assert result.data.rows == [[1, "alice"]], "write 实例的查询丢失了行数据"
    assert result.data.row_count == 1


async def test_postgres_metadata_on_write_instance(gw, harness):
    """元数据工具在 write 实例上也必须返回数据（同一个 bug 的另一面）。"""
    harness.pg_row_map["information_schema.schemata"] = [["public"]]
    result = await call(gw, "postgres_list_schemas", instance="pg-write")
    assert not result.is_error
    assert result.data.schemas == ["public"]


async def test_postgres_query_rejects_write(gw):
    """read 实例 + 写语句：在 SQL 静态分析层就被拒，且提示改用 execute。"""
    result = await call(gw, "postgres_query", instance="pg-read", sql="DELETE FROM t")
    assert result.is_error
    assert "只读" in result.content[0].text
    assert "postgres_execute" in result.content[0].text


async def test_postgres_query_rejects_cte_write(gw):
    """data-modifying CTE 必须被拒（只按前缀匹配会漏）。"""
    result = await call(
        gw, "postgres_query", instance="pg-read",
        sql="WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    )
    assert result.is_error
    assert "只读" in result.content[0].text


async def test_postgres_query_rejects_multi_statement(gw):
    result = await call(gw, "postgres_query", instance="pg-read", sql="SELECT 1; DROP TABLE t")
    assert result.is_error
    assert "多条语句" in result.content[0].text


async def test_postgres_query_rejects_dangerous_function(gw):
    result = await call(
        gw, "postgres_query", instance="pg-read", sql="SELECT pg_read_file('/etc/passwd')"
    )
    assert result.is_error
    assert "禁用" in result.content[0].text


async def test_postgres_query_truncates(gw, harness):
    """max_rows=3：4 行结果只返回 3 行并标记截断。"""
    harness.pg_row_map["FROM big"] = [[i] for i in range(4)]
    harness.pg_column_map["FROM big"] = ["n"]
    result = await call(gw, "postgres_query", instance="pg-read", sql="SELECT * FROM big")
    assert not result.is_error
    assert result.data.row_count == 3
    assert result.data.truncated


async def test_postgres_query_unknown_instance(gw):
    result = await call(gw, "postgres_query", instance="nope", sql="SELECT 1")
    assert result.is_error
    assert "可用实例" in result.content[0].text


async def test_postgres_query_on_redis_instance_rejected(gw):
    result = await call(gw, "postgres_query", instance="redis-read", sql="SELECT 1")
    assert result.is_error
    assert "redis" in result.content[0].text


# ---------------------------------------------------------------- PG 写


async def test_postgres_execute_requires_write_permission(gw):
    result = await call(gw, "postgres_execute", instance="pg-read", sql="INSERT INTO t VALUES (1)")
    assert result.is_error
    assert "read" in result.content[0].text


async def test_postgres_execute_ok(gw, harness):
    harness.pg_execute_result = "UPDATE 7"
    result = await call(
        gw, "postgres_execute", instance="pg-write", sql="UPDATE t SET a = 1"
    )
    assert not result.is_error
    assert result.data.row_count == 7
    assert result.data.command == "UPDATE"


async def test_postgres_execute_rejects_read_statement(gw):
    result = await call(gw, "postgres_execute", instance="pg-write", sql="SELECT 1")
    assert result.is_error
    assert "postgres_query" in result.content[0].text


@pytest.mark.parametrize("sql", ["DROP TABLE t", "TRUNCATE t", "ALTER TABLE t ADD c int"])
async def test_high_risk_ddl_requires_confirm(gw, sql):
    """高危 DDL 未确认时拒绝，且提示加 confirm=true。"""
    result = await call(gw, "postgres_execute", instance="pg-write", sql=sql)
    assert result.is_error
    assert "confirm" in result.content[0].text


async def test_high_risk_ddl_with_confirm_runs(gw, harness):
    harness.pg_execute_result = "DROP TABLE"
    result = await call(
        gw, "postgres_execute", instance="pg-write", sql="DROP TABLE t", confirm=True
    )
    assert not result.is_error
    assert result.data.command == "DROP TABLE"


async def test_postgres_execute_multi_statement_rejected(gw):
    result = await call(
        gw, "postgres_execute", instance="pg-write", sql="DELETE FROM t; DROP TABLE t"
    )
    assert result.is_error
    assert "多条语句" in result.content[0].text


# ---------------------------------------------------------------- PG 只读事务兜底


async def test_read_instance_uses_readonly_transaction(gw, harness):
    """只读事务是最后一道防线：即便静态分析漏判，数据库也会拒绝写。

    这里直接对 pool 跑一条写语句，绕开 sqlguard，验证 readonly=True 生效。
    """
    import asyncpg

    from db_mcp.db import pg as pgmod

    inst = gw.state.config.get_instance("pg-read")

    async def op(pool):
        return await pgmod.run(pool, "UPDATE t SET a = 1", None, read_only=True, max_rows=10)

    with pytest.raises(Exception) as exc:
        await gw.state.pool.run_with_retry("pg-read", inst, op)
    assert "read-only transaction" in str(exc.value)
    assert isinstance(getattr(exc.value, "__cause__", None), asyncpg.PostgresError)


async def test_readonly_error_translated_to_readable(gw, harness):
    """只读事务被拒时，原始报错要翻译成面向 LLM 的可读提示。"""
    from db_mcp.db import pg as pgmod
    from db_mcp.errors import PermissionDeniedError

    inst = gw.state.config.get_instance("pg-read")

    async def op(pool):
        return await pgmod.run(pool, "UPDATE t SET a = 1", None, read_only=True, max_rows=10)

    with pytest.raises(PermissionDeniedError) as exc:
        await gw.state.pool.run_with_retry("pg-read", inst, op)
    assert "read" in str(exc.value)


# ---------------------------------------------------------------- PG 元数据


async def test_postgres_list_tables(gw, harness):
    harness.pg_row_map["FROM pg_class"] = [
        ["users", "BASE TABLE", 100],
        ["user_view", "VIEW", 0],
    ]
    result = await call(gw, "postgres_list_tables", instance="pg-read")
    assert not result.is_error
    assert result.data.schema_name == "public"
    assert result.data.tables[0].name == "users"
    assert result.data.tables[0].type == "table"
    assert result.data.tables[1].type == "view"


async def test_postgres_describe_table(gw, harness):
    harness.pg_row_map["FROM pg_attribute"] = [
        ["id", "integer", False, "nextval('t_id_seq')", "主键"],
        ["name", "text", True, None, None],
    ]
    result = await call(gw, "postgres_describe_table", instance="pg-read", table="users")
    assert not result.is_error
    assert result.data.exists
    assert result.data.columns[0].name == "id"
    assert result.data.columns[0].nullable is False
    assert result.data.columns[1].nullable is True


async def test_postgres_describe_missing_table(gw, harness):
    harness.pg_row_map["FROM pg_attribute"] = []
    result = await call(gw, "postgres_describe_table", instance="pg-read", table="nope")
    assert not result.is_error
    assert result.data.exists is False
    assert result.data.columns == []


async def test_postgres_list_schemas(gw, harness):
    harness.pg_row_map["information_schema.schemata"] = [["public"], ["app"]]
    result = await call(gw, "postgres_list_schemas", instance="pg-read")
    assert not result.is_error
    assert result.data.schemas == ["public", "app"]


async def test_postgres_list_indexes(gw, harness):
    harness.pg_row_map["FROM pg_index"] = [
        ["users_pkey", True, True, "CREATE UNIQUE INDEX users_pkey ON users (id)", "id"],
    ]
    result = await call(gw, "postgres_list_indexes", instance="pg-read", table="users")
    assert not result.is_error
    assert result.data.indexes[0].primary
    assert result.data.indexes[0].columns == ["id"]


# ---------------------------------------------------------------- Redis 读


async def test_redis_get_and_set(gw, harness):
    harness.redis_store.setdefault(0, {})["user:1"] = {
        "type": "string", "value": "alice", "ttl": -1
    }
    result = await call(gw, "redis_get", instance="redis-read", key="user:1")
    assert not result.is_error
    assert result.data.value == "alice"


async def test_redis_get_missing_key(gw):
    result = await call(gw, "redis_get", instance="redis-read", key="nope")
    assert not result.is_error
    assert result.data.value is None
    assert result.data.exists is False


async def test_redis_type_and_ttl(gw, harness):
    harness.redis_store.setdefault(0, {})["k"] = {"type": "list", "value": ["a"], "ttl": 60}
    result = await call(gw, "redis_type", instance="redis-read", key="k")
    assert not result.is_error
    assert result.data.type == "list"
    assert result.data.ttl == 60

    result = await call(gw, "redis_ttl", instance="redis-read", key="k")
    assert result.data.ttl == 60


async def test_redis_mget(gw, harness):
    store = harness.redis_store.setdefault(0, {})
    store["a"] = {"type": "string", "value": "1", "ttl": -1}
    store["b"] = {"type": "string", "value": "2", "ttl": -1}
    result = await call(gw, "redis_mget", instance="redis-read", keys=["a", "b", "c"])
    assert not result.is_error
    assert result.data.values == ["1", "2", "(nil)"]


async def test_redis_keys_and_scan(gw, harness):
    store = harness.redis_store.setdefault(0, {})
    store["user:1"] = {"type": "string", "value": "a", "ttl": -1}
    store["user:2"] = {"type": "string", "value": "b", "ttl": -1}
    store["other"] = {"type": "string", "value": "c", "ttl": -1}

    result = await call(gw, "redis_keys", instance="redis-read", pattern="user:*")
    assert not result.is_error
    assert sorted(result.data.keys) == ["user:1", "user:2"]

    result = await call(gw, "redis_scan", instance="redis-read", match="user:*")
    assert not result.is_error
    assert sorted(result.data.keys) == ["user:1", "user:2"]
    assert result.data.finished


async def test_redis_hash_get(gw, harness):
    harness.redis_store.setdefault(0, {})["h"] = {
        "type": "hash", "value": {"name": "alice", "age": "30"}, "ttl": -1
    }
    result = await call(gw, "redis_hash_get", instance="redis-read", key="h")
    assert not result.is_error
    assert result.data.fields == {"name": "alice", "age": "30"}

    result = await call(gw, "redis_hash_get", instance="redis-read", key="h", fields=["name"])
    assert result.data.fields == {"name": "alice"}


async def test_redis_list_and_set_and_zset_read(gw, harness):
    store = harness.redis_store.setdefault(0, {})
    store["l"] = {"type": "list", "value": ["a", "b", "c"], "ttl": -1}
    store["s"] = {"type": "set", "value": {"x", "y"}, "ttl": -1}
    store["z"] = {"type": "zset", "value": {"a": 1.0, "b": 2.0}, "ttl": -1}

    result = await call(gw, "redis_list_range", instance="redis-read", key="l")
    assert result.data.values == ["a", "b", "c"]
    assert result.data.length == 3

    result = await call(gw, "redis_set_members", instance="redis-read", key="s")
    assert result.data.members == ["x", "y"]

    result = await call(gw, "redis_zset_range", instance="redis-read", key="z")
    assert [m["member"] for m in result.data.members] == ["a", "b"]
    assert result.data.members[1]["score"] == 2.0


# ---------------------------------------------------------------- Redis 写权限


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("redis_set", {"key": "k", "value": "v"}),
        ("redis_del", {"keys": ["k"]}),
        ("redis_expire", {"key": "k", "seconds": 60}),
        ("redis_persist", {"key": "k"}),
        ("redis_hash_set", {"key": "h", "mapping": {"a": "1"}}),
        ("redis_hash_del", {"key": "h", "fields": ["a"]}),
        ("redis_list_push", {"key": "l", "values": ["a"]}),
        ("redis_list_pop", {"key": "l"}),
        ("redis_set_add", {"key": "s", "members": ["a"]}),
        ("redis_set_remove", {"key": "s", "members": ["a"]}),
        ("redis_zset_add", {"key": "z", "members": {"a": 1.0}}),
        ("redis_zset_remove", {"key": "z", "members": ["a"]}),
    ],
)
async def test_redis_writes_rejected_on_read_instance(gw, tool, kwargs):
    """每个 Redis 写工具都必须在 read 实例上被拒。"""
    result = await call(gw, tool, instance="redis-read", **kwargs)
    assert result.is_error, tool
    assert "read" in result.content[0].text


async def test_redis_set_works_on_write_instance(gw, harness):
    result = await call(gw, "redis_set", instance="redis-write", key="k", value="v", ttl=30)
    assert not result.is_error
    assert result.data.ttl == 30
    # 落到 db 1（redis-write 配的 database: 1），不是 db 0
    assert harness.redis_store[1]["k"]["value"] == "v"


async def test_redis_write_on_postgres_instance_rejected(gw):
    result = await call(gw, "redis_set", instance="pg-write", key="k", value="v")
    assert result.is_error
    assert "postgres" in result.content[0].text


async def test_redis_writes_full_cycle(gw, harness):
    """写工具端到端：hash / list / set / zset 的增删读写闭环。"""
    r = await call(gw, "redis_hash_set", instance="redis-write", key="h", mapping={"a": "1"})
    assert not r.is_error
    r = await call(gw, "redis_hash_get", instance="redis-write", key="h")
    assert r.data.fields == {"a": "1"}
    r = await call(gw, "redis_hash_del", instance="redis-write", key="h", fields=["a"])
    assert r.data.deleted == 1

    r = await call(gw, "redis_list_push", instance="redis-write", key="l", values=["x", "y"])
    assert r.data.length == 2
    r = await call(gw, "redis_list_pop", instance="redis-write", key="l", left=False)
    assert r.data.values == ["y"]

    r = await call(gw, "redis_set_add", instance="redis-write", key="s", members=["m1", "m2"])
    assert r.data.count == 2
    r = await call(gw, "redis_set_remove", instance="redis-write", key="s", members=["m1"])
    assert r.data.deleted == 1

    r = await call(gw, "redis_zset_add", instance="redis-write", key="z", members={"a": 5.0})
    assert not r.is_error
    r = await call(gw, "redis_zset_range", instance="redis-write", key="z", desc=True)
    assert r.data.members[0]["member"] == "a"
    r = await call(gw, "redis_zset_remove", instance="redis-write", key="z", members=["a"])
    assert r.data.deleted == 1


async def test_redis_set_rejects_nonpositive_ttl(gw):
    result = await call(gw, "redis_set", instance="redis-write", key="k", value="v", ttl=0)
    assert result.is_error
    assert "必须大于 0" in result.content[0].text


async def test_redis_expire_rejects_nonpositive(gw):
    result = await call(gw, "redis_expire", instance="redis-write", key="k", seconds=0)
    assert result.is_error
    assert "必须大于 0" in result.content[0].text


async def test_redis_del_empty_keys_rejected(gw):
    result = await call(gw, "redis_del", instance="redis-write", keys=[])
    assert result.is_error
    assert "不能为空" in result.content[0].text


async def test_redis_persist(gw, harness):
    harness.redis_store.setdefault(1, {})["k"] = {"type": "string", "value": "v", "ttl": 60}
    result = await call(gw, "redis_persist", instance="redis-write", key="k")
    assert not result.is_error
    assert result.data.exists
    assert harness.redis_store[1]["k"]["ttl"] == -1
