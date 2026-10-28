"""测试公共设施：Fake asyncpg 池 + Fake Redis 客户端。

FakePgPool / FakeRedis 模拟 asyncpg 与 redis.asyncio 的接口，monkeypatch
`asyncpg.create_pool` 与 `redis.asyncio.Redis`，使连接池与网页"测试连接"
都走假实现。

**设计原则：对齐真实驱动的行为**。ssh-mcp 曾因 mock 比真实实现宽松而掩盖了
bug（不变量 2），所以这里：
- asyncpg 的 cursor 只吐 max_rows+1 行后停下（真实游标就是流式的）
- `prepare()` 返回带 get_attributes() 的对象（真实用法）
- redis 的 `decode_responses=True` 决定返回 str，这里也返回 str
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from db_mcp.config import ConfigFile
from db_mcp.server import create_mcp

_ALICE_TOKEN = "tok-alice"
_BOB_TOKEN = "tok-bob"

_CONFIG = """
server:
  host: 127.0.0.1
  port: 8807
auth:
  users:
    - username: alice
      token: ${TEST_ALICE_TOKEN}
      admin: true
    - username: bob
      token: ${TEST_BOB_TOKEN}
audit:
  path: __AUDIT_PATH__/audit.db
  max_statement_chars: 200
limits:
  max_rows: 3
  max_result_chars: 2000
  query_timeout_seconds: 5
  connect_timeout_seconds: 3
instances:
  pg-read:
    type: postgres
    host: 10.0.0.30
    port: 5432
    database: appdb
    user: reader
    password: ${TEST_PG_PASSWORD}
    permissions: read
    schema: public
  pg-write:
    type: postgres
    host: 10.0.0.30
    port: 5432
    database: appdb
    user: writer
    password: "pw-write"
    permissions: write
  redis-read:
    type: redis
    host: 10.0.0.40
    port: 6379
    password: "rd-pw"
    database: 0
    permissions: read
  redis-write:
    type: redis
    host: 10.0.0.40
    port: 6379
    password: "rd-pw"
    database: 1
    permissions: write
  pg-disabled:
    type: postgres
    host: 10.0.0.31
    database: x
    user: y
    permissions: read
    enabled: false
"""


class FakeAttribute:
    def __init__(self, name: str):
        self.name = name


class FakePreparedStatement:
    def __init__(self, columns: list[str]):
        self._columns = columns

    def get_attributes(self) -> list[FakeAttribute]:
        return [FakeAttribute(c) for c in self._columns]


class FakeCursorFactory:
    """对齐真实 asyncpg：`conn.cursor(...)` 返回的是异步可迭代的 CursorFactory。

    真实 CursorFactory 没有 `async with` / `fetch(n)`，只有 `__aiter__`。
    这里必须照实实现——否则测试会比真实实现宽松，掩盖"用错游标 API"这类 bug
    （实测踩过：async with conn.cursor(...) 只在真库里才炸）。
    """

    def __init__(self, harness: Harness, sql: str):
        self._harness = harness
        self._sql = sql
        self._rows = harness.pg_rows(sql)

    def __aiter__(self):
        columns = self._harness.pg_columns(self._sql)

        async def gen():
            for row in self._rows:
                yield dict(zip(columns, row))

        return gen()


class FakePgConn:
    def __init__(self, harness: Harness):
        self._harness = harness
        self._readonly = False

    def _check(self, sql: str) -> None:
        """真实 asyncpg 在只读事务里执行写语句会抛 PostgresError，假实现也要对齐。

        这是"只读事务兜底"那一层的测试入口：即便 sqlguard 漏判，数据库也会拒绝。
        """
        if not self._readonly:
            return
        from db_mcp.sqlguard import analyze

        if analyze(sql).is_write:
            raise asyncpg.PostgresError(
                "cannot execute UPDATE in a read-only transaction"
            )

    def _columns_for(self, sql: str) -> list[str]:
        return self._harness.pg_columns(sql)

    async def prepare(self, sql: str, **kwargs) -> FakePreparedStatement:
        """对齐真实 asyncpg：Connection.prepare 是 async 的。"""
        return FakePreparedStatement(self._columns_for(sql))

    def cursor(self, sql: str, *args, **kwargs) -> FakeCursorFactory:
        self._check(sql)
        return FakeCursorFactory(self._harness, sql)

    async def fetch(self, sql: str, *args):
        self._check(sql)
        return _rows_to_records(self._harness.pg_rows(sql))

    async def fetchval(self, sql: str, *args):
        self._check(sql)
        if "version()" in sql:
            return "PostgreSQL 16.0 (fake)"
        rows = self._harness.pg_rows(sql)
        if rows:
            return rows[0][0]
        return self._harness.pg_fetchval

    async def execute(self, sql: str, *args) -> str:
        self._check(sql)
        return self._harness.pg_execute(sql)

    def transaction(self, **kwargs):
        return _FakeTransaction(self, readonly=bool(kwargs.get("readonly")))


class _FakeTransaction:
    """进入时按 readonly 开关打开只读保护，退出时复位。"""

    def __init__(self, conn: FakePgConn, readonly: bool):
        self._conn = conn
        self._readonly = readonly

    async def __aenter__(self):
        self._conn._readonly = self._readonly
        return self

    async def __aexit__(self, *args):
        self._conn._readonly = False
        return False


class FakeAcquire:
    def __init__(self, conn: FakePgConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class FakePgPool:
    def __init__(self, harness: Harness):
        self._harness = harness

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(FakePgConn(self._harness))

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class FakeRedis:
    """对齐 redis.asyncio + decode_responses=True：返回 str，不是 bytes。"""

    def __init__(self, harness: Harness, **kwargs):
        self._harness = harness
        self._db = kwargs.get("db", 0)
        self.store = harness.redis_store.setdefault(self._db, {})

    async def ping(self):
        if self._harness.redis_fail_connect:
            raise self._harness.redis_fail_connect
        return True

    async def aclose(self):
        pass

    def close(self):
        pass

    async def type(self, key):
        return self.store.get(key, {}).get("type", "none")

    async def ttl(self, key):
        if key not in self.store:
            return -2
        return self.store[key].get("ttl", -1)

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def get(self, key):
        entry = self.store.get(key)
        if not entry or entry["type"] != "string":
            return None
        return entry["value"]

    async def set(self, key, value, ex=None):
        self.store[key] = {"type": "string", "value": str(value), "ttl": ex or -1}
        return True

    async def mget(self, keys):
        return [self.store.get(k, {}).get("value") if self.store.get(k, {}).get("type") == "string" else None for k in keys]

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    async def expire(self, key, seconds):
        if key not in self.store:
            return 0
        self.store[key]["ttl"] = seconds
        return 1

    async def persist(self, key):
        if key not in self.store:
            return 0
        self.store[key]["ttl"] = -1
        return 1

    async def keys(self, pattern):
        import fnmatch

        return [k for k in self.store if fnmatch.fnmatch(k, pattern)]

    async def scan(self, cursor=0, match="*", count=100):
        import fnmatch

        keys = [k for k in self.store if fnmatch.fnmatch(k, match)]
        return "0", keys

    async def hgetall(self, key):
        entry = self.store.get(key)
        return dict(entry["value"]) if entry and entry["type"] == "hash" else {}

    async def hmget(self, key, fields):
        entry = self.store.get(key)
        if not entry or entry["type"] != "hash":
            return [None] * len(fields)
        return [entry["value"].get(f) for f in fields]

    async def hset(self, key, mapping=None, **kwargs):
        data = mapping or kwargs
        entry = self.store.setdefault(key, {"type": "hash", "value": {}, "ttl": -1})
        entry["value"].update({k: str(v) for k, v in data.items()})
        return len(data)

    async def hdel(self, key, *fields):
        entry = self.store.get(key)
        if not entry:
            return 0
        n = 0
        for f in fields:
            if f in entry["value"]:
                del entry["value"][f]
                n += 1
        return n

    async def lrange(self, key, start, stop):
        entry = self.store.get(key)
        return list(entry["value"]) if entry and entry["type"] == "list" else []

    async def llen(self, key):
        entry = self.store.get(key)
        return len(entry["value"]) if entry and entry["type"] == "list" else 0

    async def lpush(self, key, *values):
        entry = self.store.setdefault(key, {"type": "list", "value": [], "ttl": -1})
        for v in values:
            entry["value"].insert(0, str(v))
        return len(entry["value"])

    async def rpush(self, key, *values):
        entry = self.store.setdefault(key, {"type": "list", "value": [], "ttl": -1})
        entry["value"].extend(str(v) for v in values)
        return len(entry["value"])

    async def lpop(self, key, count=None):
        entry = self.store.get(key)
        if not entry or not entry["value"]:
            return None
        if count is None:
            return entry["value"].pop(0)
        return [entry["value"].pop(0) for _ in range(min(count, len(entry["value"])))]

    async def rpop(self, key, count=None):
        entry = self.store.get(key)
        if not entry or not entry["value"]:
            return None
        if count is None:
            return entry["value"].pop()
        return [entry["value"].pop() for _ in range(min(count, len(entry["value"])))]

    async def smembers(self, key):
        entry = self.store.get(key)
        return set(entry["value"]) if entry and entry["type"] == "set" else set()

    async def sadd(self, key, *members):
        entry = self.store.setdefault(key, {"type": "set", "value": set(), "ttl": -1})
        before = len(entry["value"])
        entry["value"].update(str(m) for m in members)
        return len(entry["value"]) - before

    async def srem(self, key, *members):
        entry = self.store.get(key)
        if not entry:
            return 0
        n = 0
        for m in members:
            if m in entry["value"]:
                entry["value"].discard(m)
                n += 1
        return n

    async def zadd(self, key, mapping):
        entry = self.store.setdefault(key, {"type": "zset", "value": {}, "ttl": -1})
        added = 0
        for m, score in mapping.items():
            if m not in entry["value"]:
                added += 1
            entry["value"][m] = float(score)
        return added

    async def zrem(self, key, *members):
        entry = self.store.get(key)
        if not entry:
            return 0
        n = 0
        for m in members:
            if m in entry["value"]:
                del entry["value"][m]
                n += 1
        return n

    async def zrange(self, key, start, stop, withscores=False):
        return self._zrange(key, start, stop, withscores, reverse=False)

    async def zrevrange(self, key, start, stop, withscores=False):
        return self._zrange(key, start, stop, withscores, reverse=True)

    def _zrange(self, key, start, stop, withscores, reverse):
        entry = self.store.get(key)
        if not entry or entry["type"] != "zset":
            return []
        items = sorted(entry["value"].items(), key=lambda kv: kv[1], reverse=reverse)
        items = items[start : (None if stop == -1 else stop + 1)]
        if withscores:
            return items
        return [m for m, _ in items]

    async def info(self):
        return {"redis_version": "7.2.0"}


def _rows_to_records(rows: list[list], columns: list[str] | None = None) -> list[dict]:
    """把行列表转成 dict（真实 asyncpg.Record 支持 keys()/values()，dict 够用）。"""
    if not columns:
        return [dict(r) for r in rows]
    return [dict(zip(columns, r)) for r in rows]


class Harness:
    """承载一次测试的全部假状态。"""

    def __init__(self):
        # pg_rows: 依 SQL 关键字返回行；默认给一个通用结果
        self.pg_row_map: dict[str, list[list]] = {}
        self.pg_column_map: dict[str, list[str]] = {}
        self.pg_execute_result = "UPDATE 5"
        self.pg_statements: list[str] = []
        self.pg_fetchval = None
        self.pg_fail_connect: Exception | None = None
        self.redis_store: dict[int, dict] = {}
        self.redis_fail_connect: Exception | None = None
        self.connect_calls: list[dict] = []

    # ---- PG ----
    def _rows_for(self, sql: str) -> list[list]:
        """按关键字匹配预设行；不记录调用（供列宽推断用）。"""
        for key, rows in self.pg_row_map.items():
            if key in sql:
                return rows
        return [[1]]

    def pg_rows(self, sql: str) -> list[list]:
        self.pg_statements.append(sql)
        return self._rows_for(sql)

    def pg_columns(self, sql: str) -> list[str]:
        for key, cols in self.pg_column_map.items():
            if key in sql:
                return cols
        # 未显式指定列名时，按该 SQL 会返回的行宽度造占位列名，
        # 保证 prepare() 的列数与 cursor() 的行宽一致（真实驱动必然一致）
        rows = self._rows_for(sql)
        width = len(rows[0]) if rows and isinstance(rows[0], (list, tuple)) else 1
        return [f"c{i}" for i in range(width)]

    def pg_execute(self, sql: str) -> str:
        self.pg_statements.append(sql)
        return self.pg_execute_result

    # ---- 连接工厂 ----
    async def create_pool(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self.pg_fail_connect is not None:
            exc, self.pg_fail_connect = self.pg_fail_connect, None
            raise exc
        return FakePgPool(self)

    def make_redis_client(self, **kwargs):
        """替代 redis.asyncio.Redis：构造即校验一次（pool.py 依赖 __init__ 后 ping）。"""
        self.connect_calls.append(kwargs)
        return FakeRedis(self, **kwargs)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """替换两个驱动的入口：asyncpg.create_pool 与 redis.asyncio.Redis。

    pool.py 里是 `aioredis.Redis(...)` 的构造函数调用，所以这里要替换成
    一个**类**（而非工厂函数），FakeRedis 的 __init__ 签名需兼容关键字参数。
    """
    import asyncpg
    import redis.asyncio as aioredis

    h = Harness()
    monkeypatch.setattr(asyncpg, "create_pool", h.create_pool)
    monkeypatch.setattr(aioredis, "Redis", h.make_redis_client)
    return h


def make_mcp_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """写临时配置文件并构建 mcp/state（含环境变量）。"""
    monkeypatch.setenv("TEST_ALICE_TOKEN", _ALICE_TOKEN)
    monkeypatch.setenv("TEST_BOB_TOKEN", _BOB_TOKEN)
    monkeypatch.setenv("TEST_PG_PASSWORD", "pg-pw")

    def _make(extra_config: str = "") -> SimpleNamespace:
        config_text = _CONFIG.replace("__AUDIT_PATH__", str(tmp_path / "audit")) + extra_config
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config_text, encoding="utf-8")
        cfg_file = ConfigFile(config_path)
        mcp, state = create_mcp(cfg_file)
        return SimpleNamespace(mcp=mcp, state=state, cfg_file=cfg_file, config_path=config_path)

    return _make


@pytest.fixture
def make_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return make_mcp_factory(tmp_path, monkeypatch)


@pytest.fixture
def gw(harness, make_mcp):
    """标准网关：默认配置（4 个可用实例）+ 假驱动。"""
    return make_mcp()
