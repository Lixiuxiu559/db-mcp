"""数据库连接池：按实例名缓存 asyncpg 池 / redis 客户端。

并发模型：所有工具跑在同一事件循环上，dict 读写本身安全；真正要保护的是
"检查-再连接"竞态，用 per-instance 的 asyncio.Lock 双重检查解决。

与 ssh-mcp 的 ConnectionPool 同构，差异：
- 两种驱动（asyncpg 池 / redis 客户端）分开缓存
- 参数变化（管理端改配置）后由 state 调 invalidate 失效
- **read 实例的 PostgreSQL 查询一律跑在只读事务里**（`BEGIN READ ONLY`），
  这是 sqlguard 之外的最后一道防线：即便静态分析漏判，数据库自己会拒绝写入
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import asyncpg
import redis.asyncio as aioredis

from ..config import (
    GatewayConfig,
    PostgresInstance,
    RedisInstance,
)
from ..errors import ConnectionFailedError, QueryTimeoutError

T = TypeVar("T")

# 连接坏掉后失效重连再试一次（对齐 ssh-mcp 的做法）
_RETRYABLE = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    ConnectionResetError,
    OSError,
    aioredis.ConnectionError,
    aioredis.TimeoutError,
)


class ConnectionPool:
    def __init__(self, config_fn: Callable[[], GatewayConfig]):
        self._config_fn = config_fn
        self._pg: dict[str, Any] = {}
        self._redis: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------ 内部

    def _lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def _semaphore(self, name: str) -> asyncio.Semaphore:
        if name not in self._sems:
            limit = max(1, self._config_fn().limits.max_concurrency_per_instance)
            self._sems[name] = asyncio.Semaphore(limit)
        return self._sems[name]

    async def _connect_pg(self, name: str, inst: PostgresInstance) -> Any:
        limits = self._config_fn().limits
        try:
            return await asyncpg.create_pool(
                host=inst.host,
                port=inst.port,
                database=inst.database,
                user=inst.user,
                password=inst.password.get_secret_value() or None,
                min_size=1,
                max_size=max(1, limits.max_pool_size),
                timeout=limits.connect_timeout_seconds,
                # 服务端语句缓存在配置热更新后可能过期；保持默认（asyncpg 自动管理）
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ConnectionFailedError(
                f"连接 PostgreSQL 实例 {name!r}（{inst.host}:{inst.port}/{inst.database}）"
                f"超时（{limits.connect_timeout_seconds}s），请确认网络与实例状态。"
            ) from exc
        except (asyncpg.PostgresError, OSError) as exc:
            raise ConnectionFailedError(
                f"连接 PostgreSQL 实例 {name!r}（{inst.host}:{inst.port}/{inst.database}）"
                f"失败: {exc}"
            ) from exc

    async def _connect_redis(self, name: str, inst: RedisInstance) -> Any:
        limits = self._config_fn().limits
        client = aioredis.Redis(
            host=inst.host,
            port=inst.port,
            username=inst.username or None,
            password=inst.password.get_secret_value() or None,
            db=inst.database,
            socket_connect_timeout=limits.connect_timeout_seconds,
            socket_timeout=limits.query_timeout_seconds,
            decode_responses=True,  # 直接拿 str，省去到处 decode 的麻烦
            health_check_interval=30,
        )
        try:
            # 立即握手一次，把"连不上/认证失败"在取连接阶段就暴露成可读错误，
            # 而不是等到具体命令才炸出 Redis 原始报错
            await client.ping()
        except aioredis.AuthenticationError as exc:
            await _close_quietly(client)
            raise ConnectionFailedError(
                f"Redis 实例 {name!r}（{inst.host}:{inst.port}）认证失败，请检查密码/ACL 用户名。"
            ) from exc
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as exc:
            await _close_quietly(client)
            raise ConnectionFailedError(
                f"连接 Redis 实例 {name!r}（{inst.host}:{inst.port}）失败: {exc}"
            ) from exc
        return client

    # ------------------------------------------------------------ 对外

    async def get_pg_pool(self, name: str, inst: PostgresInstance) -> Any:
        async with self._lock(f"pg:{name}"):
            pool = self._pg.get(name)
            if pool is None:
                pool = await self._connect_pg(name, inst)
                self._pg[name] = pool
            return pool

    async def get_redis_client(self, name: str, inst: RedisInstance) -> Any:
        async with self._lock(f"redis:{name}"):
            client = self._redis.get(name)
            if client is None:
                client = await self._connect_redis(name, inst)
                self._redis[name] = client
            return client

    async def connect_one_shot(self, inst: PostgresInstance | RedisInstance) -> Any:
        """新建一次性连接（不缓存），供管理端"测试连接"使用。"""
        if isinstance(inst, PostgresInstance):
            return await self._connect_pg(f"test:{inst.host}", inst)
        return await self._connect_redis(f"test:{inst.host}", inst)

    async def close_one_shot(self, conn: Any) -> None:
        await _close_quietly(conn)

    async def invalidate(self, name: str) -> None:
        """配置变化或连接失效时丢弃缓存；下次调用会重建。"""
        pool = self._pg.pop(name, None)
        if pool is not None:
            await _close_quietly(pool)
        client = self._redis.pop(name, None)
        if client is not None:
            await _close_quietly(client)

    async def close_all(self) -> None:
        pools = list(self._pg.values())
        clients = list(self._redis.values())
        self._pg.clear()
        self._redis.clear()
        for obj in [*pools, *clients]:
            await _close_quietly(obj)

    async def run_with_retry(
        self, name: str, inst: PostgresInstance | RedisInstance, op: Callable[[Any], Awaitable[T]]
    ) -> T:
        """在缓存连接上执行操作；遇连接级错误则失效重连再试一次。"""
        async with self._semaphore(name):
            conn = await self._get(name, inst)
            try:
                return await op(conn)
            except _RETRYABLE:
                await self.invalidate(name)
                conn = await self._get(name, inst)
                try:
                    return await op(conn)
                except _RETRYABLE as exc2:
                    raise ConnectionFailedError(
                        f"在实例 {name!r} 上操作失败（重连后仍失败）: {exc2}"
                    ) from exc2

    async def _get(self, name: str, inst: PostgresInstance | RedisInstance) -> Any:
        if isinstance(inst, PostgresInstance):
            return await self.get_pg_pool(name, inst)
        return await self.get_redis_client(name, inst)


async def _close_quietly(obj: Any) -> None:
    """尽力关闭连接/池。

    - asyncpg 池：close() 是同步的且立即返回，需再 await wait_closed() 才算真正释放
    - redis 客户端：优先用 aclose()（redis-py 5+ 的异步关闭），退回 close()
    """
    try:
        if hasattr(obj, "aclose"):  # redis.asyncio
            await obj.aclose()
        elif hasattr(obj, "wait_closed"):  # asyncpg.Pool
            obj.close()
            await obj.wait_closed()
        else:
            result = obj.close()
            if asyncio.iscoroutine(result):
                await result
    except Exception:  # noqa: BLE001, S110 - 关闭失败不影响正确性，连接会自行超时回收
        pass


def as_timeout_error(name: str, seconds: int) -> QueryTimeoutError:
    return QueryTimeoutError(
        f"实例 {name!r} 上的操作在 {seconds}s 后超时并已取消；"
        "请收窄查询范围（加 WHERE / LIMIT）或调大 limits.query_timeout_seconds。"
    )
