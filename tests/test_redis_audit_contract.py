"""通过已注册工具与 redis-py 命令编码核对审计，不复制工具的命令选择逻辑。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp import Client
from redis.asyncio.client import Redis
from redis.exceptions import ResponseError

pytestmark = pytest.mark.usefixtures("inprocess_auth")


@dataclass(frozen=True)
class Case:
    tool: str
    args: dict[str, Any]
    commands: tuple[tuple[Any, ...], ...]
    statement: str
    write: bool = False
    responses: dict[str, Any] = field(default_factory=dict)


# 期望来自公开工具契约；命令实录由 redis-py 自己编码，覆盖辅助 TTL / LLEN 查询。
CASES = (
    Case("redis_type", {"key": "k"}, (("TYPE", "k"), ("TTL", "k")), "TYPE k"),
    Case("redis_type", {"key": "missing"}, (("TYPE", "missing"),), "TYPE missing",
         responses={"TYPE": "none"}),
    Case("redis_exists", {"key": "k"}, (("EXISTS", "k"),), "EXISTS k"),
    Case("redis_ttl", {"key": "k"}, (("TTL", "k"),), "TTL k"),
    Case("redis_get", {"key": "k"}, (("GET", "k"),), "GET k"),
    Case("redis_get", {"key": ""}, (("GET", ""),), 'GET ""'),
    Case("redis_mget", {"keys": ["a", "b"]}, (("MGET", "a", "b"),), "MGET ['a', 'b']"),
    Case("redis_set", {"key": "k", "value": "private-value"},
         (("SET", "k", "private-value"),), "SET k", True),
    Case("redis_set", {"key": "k", "value": "private-value", "ttl": None},
         (("SET", "k", "private-value"),), 'SET k {"ttl": null}', True),
    Case("redis_set", {"key": "k", "value": "private-value", "ttl": 30},
         (("SET", "k", "private-value", "EX", 30),), 'SET k {"ttl": 30}', True),
    Case("redis_del", {"keys": ["a", "b"]}, (("DEL", "a", "b"),), "DEL ['a', 'b']", True),
    Case("redis_expire", {"key": "k", "seconds": 30},
         (("EXPIRE", "k", 30), ("TTL", "k")), 'EXPIRE k {"seconds": 30}', True),
    Case("redis_expire", {"key": "missing", "seconds": 30},
         (("EXPIRE", "missing", 30),), 'EXPIRE missing {"seconds": 30}', True,
         responses={"EXPIRE": False}),
    Case("redis_persist", {"key": "k"}, (("PERSIST", "k"),), "PERSIST k", True),
    Case("redis_scan", {}, (("SCAN", "0", "MATCH", "*", "COUNT", 100),),
         'SCAN {"match": "*", "count": 100, "cursor": "0"}'),
    Case("redis_scan", {"cursor": "12", "match": "user:*", "count": 7},
         (("SCAN", "12", "MATCH", "user:*", "COUNT", 7),),
         'SCAN {"match": "user:*", "count": 7, "cursor": "12"}'),
    Case("redis_keys", {}, (("KEYS", "*"),), "KEYS *"),
    Case("redis_keys", {"pattern": "user:*"}, (("KEYS", "user:*"),), "KEYS user:*"),
    Case("redis_hash_get", {"key": "h"}, (("HGETALL", "h"),), "HGETALL h"),
    Case("redis_hash_get", {"key": "h", "fields": None},
         (("HGETALL", "h"),), 'HGETALL h {"fields": null}'),
    Case("redis_hash_get", {"key": "h", "fields": []},
         (("HGETALL", "h"),), 'HGETALL h {"fields": []}'),
    Case("redis_hash_get", {"key": "h", "fields": ["a"]},
         (("HMGET", "h", "a"),), 'HMGET h {"fields": ["a"]}'),
    Case("redis_hash_get", {"key": "h", "fields": ["a", "missing"]},
         (("HMGET", "h", "a", "missing"),), 'HMGET h {"fields": ["a", "missing"]}'),
    Case("redis_hash_set", {"key": "h", "mapping": {"private-field": "private-hash-value"}},
         (("HSET", "h", "private-field", "private-hash-value"),), "HSET h", True),
    Case("redis_hash_del", {"key": "h", "fields": ["a", "b"]},
         (("HDEL", "h", "a", "b"),), 'HDEL h {"fields": ["a", "b"]}', True),
    Case("redis_list_range", {"key": "l"},
         (("LRANGE", "l", 0, -1), ("LLEN", "l")), 'LRANGE l {"start": 0, "stop": -1}'),
    Case("redis_list_range", {"key": "l", "start": 1, "stop": 2},
         (("LRANGE", "l", 1, 2), ("LLEN", "l")), 'LRANGE l {"start": 1, "stop": 2}'),
    Case("redis_list_push", {"key": "l", "values": ["private-list-value"]},
         (("RPUSH", "l", "private-list-value"),), "RPUSH l", True),
    Case("redis_list_push", {"key": "l", "values": ["private-list-value"], "left": False},
         (("RPUSH", "l", "private-list-value"),), "RPUSH l", True),
    Case("redis_list_push", {"key": "l", "values": ["private-list-value"], "left": True},
         (("LPUSH", "l", "private-list-value"),), "LPUSH l", True),
    Case("redis_list_pop", {"key": "l"}, (("LPOP", "l", 1),), 'LPOP l {"count": 1}', True),
    Case("redis_list_pop", {"key": "l", "left": True, "count": 2},
         (("LPOP", "l", 2),), 'LPOP l {"count": 2}', True),
    Case("redis_list_pop", {"key": "l", "left": False, "count": 2},
         (("RPOP", "l", 2),), 'RPOP l {"count": 2}', True),
    Case("redis_set_members", {"key": "s"}, (("SMEMBERS", "s"),), "SMEMBERS s"),
    Case("redis_set_add", {"key": "s", "members": ["private-set-member"]},
         (("SADD", "s", "private-set-member"),), "SADD s", True),
    Case("redis_set_remove", {"key": "s", "members": ["private-set-member"]},
         (("SREM", "s", "private-set-member"),), "SREM s", True),
    Case("redis_zset_range", {"key": "z"}, (("ZRANGE", "z", 0, -1, "WITHSCORES"),),
         'ZRANGE z {"start": 0, "stop": -1, "with_scores": true}'),
    Case("redis_zset_range", {"key": "z", "desc": False, "with_scores": False},
         (("ZRANGE", "z", 0, -1),),
         'ZRANGE z {"start": 0, "stop": -1, "with_scores": false}'),
    Case("redis_zset_range", {"key": "z", "desc": True},
         (("ZREVRANGE", "z", 0, -1, "WITHSCORES"),),
         'ZREVRANGE z {"start": 0, "stop": -1, "with_scores": true}'),
    Case("redis_zset_range", {"key": "z", "desc": True, "with_scores": False,
                             "start": 1, "stop": 2}, (("ZREVRANGE", "z", 1, 2),),
         'ZREVRANGE z {"start": 1, "stop": 2, "with_scores": false}'),
    Case("redis_zset_add", {"key": "z", "members": {"private-zset-member": 98.7654321}},
         (("ZADD", "z", 98.7654321, "private-zset-member"),), "ZADD z", True),
    Case("redis_zset_remove", {"key": "z", "members": ["private-zset-member"]},
         (("ZREM", "z", "private-zset-member"),), "ZREM z", True),
)


@pytest.fixture
def redis_wire(gw, monkeypatch):
    """仅替换网络边界；保留安装版本 redis-py 的方法和实际参数编码。"""
    class Recorder:
        commands: list[tuple[Any, ...]]
        starts: list[dict[str, Any]]

        def __init__(self):
            self.commands = []
            self.starts = []
            self.begins = []
            self.responses = {}
            self.error = None

    recorder = Recorder()
    begin_call = gw.state.audit.begin_call

    async def observe_begin(record):
        call_id = await begin_call(record)
        records, total = gw.state.audit.query(limit=1)
        assert total and records[0]["id"] == call_id
        assert records[0]["status"] == "running"
        recorder.begins.append(records[0])
        return call_id

    monkeypatch.setattr(gw.state.audit, "begin_call", observe_begin)

    class WireRedis(Redis):
        async def execute_command(self, *args, **options):
            command = tuple(a.decode() if isinstance(a, bytes) else a for a in args)
            # 包括首次 PING：连接握手前已经持久化 running 记录。
            records, total = gw.state.audit.query(limit=1)
            assert total and records[0]["status"] == "running"
            recorder.starts.append(records[0])
            if command[0] == "PING":
                return True
            recorder.commands.append(command)
            if recorder.error:
                raise recorder.error
            if command[0] in recorder.responses:
                return recorder.responses[command[0]]
            if command[0] in {"ZRANGE", "ZREVRANGE"}:
                return [("member", 1.0)] if "WITHSCORES" in command else ["member"]
            if command[0] == "HMGET":
                return ["value"] * (len(command) - 2)
            return {
                "TYPE": "string", "TTL": 30, "EXISTS": 1, "GET": "value",
                "MGET": ["value", None], "SET": True, "DEL": 1,
                "EXPIRE": True, "PERSIST": True, "SCAN": (0, ["k"]),
                "KEYS": ["k"], "HGETALL": {"a": "value"}, "HSET": 1,
                "HDEL": 1, "LRANGE": ["value"], "LLEN": 1, "RPUSH": 1,
                "LPUSH": 1, "LPOP": ["value"], "RPOP": ["value"],
                "SMEMBERS": {"member"}, "SADD": 1, "SREM": 1,
                "ZADD": 1, "ZREM": 1,
            }[command[0]]

    monkeypatch.setattr("redis.asyncio.Redis", WireRedis)
    return recorder


def _record(gw):
    records, total = gw.state.audit.query(limit=10)
    assert total == 1
    return records[0]


def _assert_completed(record, status):
    assert record["status"] == status
    assert record["ended_at"] is not None
    assert record["duration_ms"] >= 0


async def test_contract_covers_every_registered_redis_tool(gw):
    async with Client(gw.mcp) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools if tool.name.startswith("redis_")} == {
        case.tool for case in CASES
    }


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.tool)
async def test_redis_wire_command_matches_persisted_description(gw, redis_wire, case):
    redis_wire.responses.update(case.responses)
    instance = "redis-write" if case.write else "redis-read"
    async with Client(gw.mcp) as client:
        person_before = gw.state.audit.query(kind="person")[1]
        result = await client.call_tool(case.tool, {"instance": instance, **case.args})
        assert gw.state.audit.query(kind="person")[1] == person_before
    assert not result.is_error
    assert tuple(redis_wire.commands) == case.commands
    record = _record(gw)
    assert record["statement"] == case.statement
    assert all(start["statement"] == case.statement for start in redis_wire.starts)
    assert len(redis_wire.begins) == 1
    assert redis_wire.begins[0]["statement"] == case.statement
    for private in ("private-value", "private-field", "private-hash-value", "private-list-value",
                    "private-set-member", "private-zset-member", "98.7654321"):
        assert private not in str(record)
    assert record["tool"] == case.tool
    assert record["token_id"] == gw.token["id"]
    assert record["workspace_id"] == gw.workspace_id
    assert (record["target_type"], record["target_host"], record["target_port"],
            record["target_database"]) == ("redis", "10.0.0.40", 6379, "1" if case.write else "0")
    assert record["statement_truncated"] == 0
    _assert_completed(record, "ok")


WRITE_CASES = tuple(case for case in CASES if case.write)


@pytest.mark.parametrize("case", WRITE_CASES, ids=lambda case: case.tool)
async def test_every_write_denied_before_redis_and_keeps_attempt_description(gw, redis_wire, case):
    async with Client(gw.mcp) as client:
        result = await client.call_tool(
            case.tool, {"instance": "redis-read", **case.args}, raise_on_error=False
        )
    assert result.is_error
    assert redis_wire.commands == redis_wire.starts == []
    record = _record(gw)
    assert record["statement"] == case.statement
    assert len(redis_wire.begins) == 1
    assert redis_wire.begins[0]["statement"] == case.statement
    assert "PermissionDeniedError" in record["error"]
    _assert_completed(record, "denied")


INVALID_CASES = (
    ("redis_mget", {"keys": []}, "MGET []"),
    ("redis_del", {"keys": []}, "DEL []"),
    ("redis_set", {"key": "k", "value": "private-value", "ttl": 0}, 'SET k {"ttl": 0}'),
    ("redis_set", {"key": "k", "value": "private-value", "ttl": -1}, 'SET k {"ttl": -1}'),
    ("redis_expire", {"key": "k", "seconds": 0}, 'EXPIRE k {"seconds": 0}'),
    ("redis_hash_set", {"key": "h", "mapping": {}}, "HSET h"),
    ("redis_hash_del", {"key": "h", "fields": []}, 'HDEL h {"fields": []}'),
    ("redis_list_push", {"key": "l", "values": []}, "RPUSH l"),
    ("redis_set_add", {"key": "s", "members": []}, "SADD s"),
    ("redis_set_remove", {"key": "s", "members": []}, "SREM s"),
    ("redis_zset_add", {"key": "z", "members": {}}, "ZADD z"),
    ("redis_zset_remove", {"key": "z", "members": []}, "ZREM z"),
)


@pytest.mark.parametrize("tool,args,statement", INVALID_CASES)
async def test_invalid_business_arguments_denied_and_audited(gw, redis_wire, tool, args, statement):
    async with Client(gw.mcp) as client:
        result = await client.call_tool(
            tool, {"instance": "redis-write", **args}, raise_on_error=False
        )
    assert result.is_error
    assert redis_wire.commands == redis_wire.starts == []
    record = _record(gw)
    assert record["statement"] == statement
    assert len(redis_wire.begins) == 1
    assert redis_wire.begins[0]["statement"] == statement
    assert "StatementNotAllowedError" in record["error"]
    _assert_completed(record, "denied")


@pytest.mark.parametrize("flag", ["nx", "xx"])
async def test_set_does_not_accept_unimplemented_conditional_flags(gw, redis_wire, flag):
    async with Client(gw.mcp) as client:
        result = await client.call_tool(
            "redis_set", {"instance": "redis-write", "key": "k", "value": "private-value",
                          flag: True}, raise_on_error=False
        )
    assert result.is_error
    assert redis_wire.commands == redis_wire.starts == []
    record = _record(gw)
    assert record["statement"] == "SET k"
    assert len(redis_wire.begins) == 1
    _assert_completed(record, "error")


async def test_driver_error_preserves_operation_and_redacts_credentials(gw, redis_wire):
    redis_wire.error = ResponseError("operation refused rd-pw")
    async with Client(gw.mcp) as client:
        result = await client.call_tool(
            "redis_get", {"instance": "redis-read", "key": "k"}, raise_on_error=False
        )
    assert result.is_error
    assert redis_wire.commands == [("GET", "k")]
    record = _record(gw)
    assert record["statement"] == "GET k"
    assert len(redis_wire.begins) == 1
    assert "rd-pw" not in record["error"]
    # FastMCP 会屏蔽非领域异常；底层详情仍保留在尝试审计，并须先脱敏。
    with sqlite3.connect(gw.state.audit.path) as conn:
        attempts = conn.execute("SELECT error FROM mcp_attempts WHERE call_id=?", (record["id"],))
        errors = [row[0] for row in attempts]
    assert len(errors) == 1
    assert "rd-pw" not in errors[0]
    assert "[已脱敏]" in errors[0]
    _assert_completed(record, "error")


async def test_redis_statement_truncation_still_marks_exact_limit(gw, redis_wire):
    key = "key-" + "x" * 300
    async with Client(gw.mcp) as client:
        await client.call_tool("redis_get", {"instance": "redis-read", "key": key})
    assert redis_wire.commands == [("GET", key)]
    record = _record(gw)
    assert record["statement"] == ("GET " + key)[:200]
    assert record["statement_truncated"] == 1
    _assert_completed(record, "ok")


async def test_parameterized_sql_executes_params_without_auditing_them(gw, monkeypatch):
    from conftest import FakePgConn

    original = FakePgConn.cursor
    captured = []

    def cursor(conn, sql, *params, **kwargs):
        captured.append((sql, params))
        return original(conn, sql, *params, **kwargs)

    monkeypatch.setattr(FakePgConn, "cursor", cursor)
    sql = "SELECT $1::text"
    async with Client(gw.mcp) as client:
        result = await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": sql, "params": ["private-sql-param"]}
        )
    assert not result.is_error
    assert captured == [(sql, ("private-sql-param",))]
    record = _record(gw)
    assert record["statement"] == sql
    assert "private-sql-param" not in str(record)
    _assert_completed(record, "ok")
