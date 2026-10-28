"""Redis 工具：string / hash / list / set / zset 的常用读写。

安全模型与 PostgreSQL 侧一致：
- `permissions: read` 的实例只放行读命令，写命令直接拒绝
- 不提供 `redis_command` 这类任意命令逃生舱——那会让权限分级形同虚设；
  常用结构都给了专用工具，够用且可控
- 统一使用 SCAN 而非 KEYS 的批量遍历（`redis_keys` 保留但会警告大数据集风险）
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from ..config import RedisInstance
from ..db.pool import as_timeout_error
from ..dependencies import get_state
from ..errors import PermissionDeniedError, StatementNotAllowedError
from ..models import (
    RedisBoolResult,
    RedisDeleteResult,
    RedisExpireResult,
    RedisHashResult,
    RedisKeysResult,
    RedisListResult,
    RedisScanResult,
    RedisSetMembersResult,
    RedisSetResult,
    RedisTtlResult,
    RedisValueResult,
    RedisZsetResult,
)
from ..state import DbState


def _redis(state: DbState, name: str) -> RedisInstance:
    """取实例并确认它是 redis 类型。"""
    inst = state.config.resolve_instance(name)
    if not isinstance(inst, RedisInstance):
        raise StatementNotAllowedError(
            f"实例 {name!r} 是 {inst.type} 类型，Redis 工具不能用于它。"
            "请改用 list_instances 查看有哪些 Redis 实例。"
        )
    return inst


def _require_write(inst: RedisInstance, name: str, command: str) -> None:
    if inst.permissions != "write":
        raise PermissionDeniedError(
            f"实例 {name!r} 的权限为 read，禁止执行写命令（{command}）。"
            "请在权限为 write 的实例上执行，或用管理端把该实例改为 write。"
        )


async def _run(
    state: DbState,
    name: str,
    inst: RedisInstance,
    op,
    timeout: int | None = None,
):
    """统一执行路径：连接池 + 超时 + 重试。"""
    limits = state.config.limits
    seconds = timeout or limits.query_timeout_seconds
    try:
        return await asyncio.wait_for(
            state.pool.run_with_retry(name, inst, op), timeout=seconds
        )
    except asyncio.TimeoutError:
        raise as_timeout_error(name, seconds) from None


def register(mcp: FastMCP) -> None:
    # ==================== 元信息 ====================
    @mcp.tool
    async def redis_type(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisValueResult:
        """查看某个 key 的数据类型（string/list/set/zset/hash/stream/none）。

        先看类型再决定用哪个读取工具，是最稳的排查路径。

        Args:
            instance: 实例名称。
            key: 键名。
        """
        inst = _redis(state, instance)
        value_type, ttl = await _run(
            state, instance, inst,
            lambda c: _type_and_ttl(c, key),
        )
        return RedisValueResult(
            instance=instance, key=key, type=value_type,
            value=None, ttl=ttl, exists=value_type != "none",
        )

    @mcp.tool
    async def redis_exists(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisBoolResult:
        """检查 key 是否存在。

        Args:
            instance: 实例名称。
            key: 键名。
        """
        inst = _redis(state, instance)
        exists = await _run(state, instance, inst, lambda c: c.exists(key))
        return RedisBoolResult(instance=instance, key=key, exists=bool(exists))

    @mcp.tool
    async def redis_ttl(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisTtlResult:
        """查看 key 的剩余生存时间（秒）。-1 = 永不过期，-2 = key 不存在。

        Args:
            instance: 实例名称。
            key: 键名。
        """
        inst = _redis(state, instance)
        ttl = await _run(state, instance, inst, lambda c: c.ttl(key))
        return RedisTtlResult(instance=instance, key=key, ttl=int(ttl))

    # ==================== string ====================
    @mcp.tool
    async def redis_get(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisValueResult:
        """读取 string 类型 key 的值。

        Args:
            instance: 实例名称。
            key: 键名。
        """
        inst = _redis(state, instance)
        value = await _run(state, instance, inst, lambda c: c.get(key))
        return RedisValueResult(
            instance=instance, key=key, type="string", value=value,
            exists=value is not None,
        )

    @mcp.tool
    async def redis_mget(
        instance: str, keys: list[str], state: DbState = Depends(get_state)
    ) -> RedisListResult:
        """批量读取多个 string key 的值（一次往返，比逐个 get 快）。

        Args:
            instance: 实例名称。
            keys: 键名列表。
        """
        inst = _redis(state, instance)
        if not keys:
            raise StatementNotAllowedError("keys 不能为空")
        values = await _run(state, instance, inst, lambda c: c.mget(keys))
        return RedisListResult(
            instance=instance, key=",".join(keys),
            values=["(nil)" if v is None else str(v) for v in values],
            length=len(values),
        )

    @mcp.tool
    async def redis_set(
        instance: str,
        key: str,
        value: str,
        ttl: int | None = None,
        state: DbState = Depends(get_state),
    ) -> RedisSetResult:
        """设置 string 值，可选过期时间（秒）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            value: 要写入的值。
            ttl: 过期秒数；不填则永不过期。

        Error Handling:
            - 实例权限为 read → 拒绝并提示。
            - ttl 非正数 → 拒绝（请改用 redis_expire 移除过期时间）。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "SET")
        if ttl is not None and ttl <= 0:
            raise StatementNotAllowedError(
                "ttl 必须大于 0；如需移除过期时间请用 redis_persist。"
            )
        if ttl:
            await _run(state, instance, inst, lambda c: c.set(key, value, ex=ttl))
        else:
            await _run(state, instance, inst, lambda c: c.set(key, value))
        return RedisSetResult(instance=instance, key=key, value=value, ttl=ttl)

    @mcp.tool
    async def redis_del(
        instance: str, keys: list[str], state: DbState = Depends(get_state)
    ) -> RedisDeleteResult:
        """删除一个或多个 key，返回实际删除数量。

        Args:
            instance: 实例名称（权限必须为 write）。
            keys: 要删除的键名列表。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "DEL")
        if not keys:
            raise StatementNotAllowedError("keys 不能为空")
        deleted = await _run(state, instance, inst, lambda c: c.delete(*keys))
        return RedisDeleteResult(
            instance=instance, deleted=int(deleted), requested=len(keys)
        )

    @mcp.tool
    async def redis_expire(
        instance: str, key: str, seconds: int, state: DbState = Depends(get_state)
    ) -> RedisExpireResult:
        """设置 key 的过期时间（秒）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            seconds: 过期秒数，必须大于 0；要撤销过期请用 redis_persist。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "EXPIRE")
        if seconds <= 0:
            raise StatementNotAllowedError(
                "seconds 必须大于 0；如需移除过期时间请用 redis_persist。"
            )
        ok = await _run(state, instance, inst, lambda c: c.expire(key, seconds))
        ttl = await _run(state, instance, inst, lambda c: c.ttl(key)) if ok else None
        return RedisExpireResult(
            instance=instance, key=key, seconds=seconds, success=bool(ok),
            ttl=int(ttl) if ttl is not None else None,
        )

    @mcp.tool
    async def redis_persist(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisBoolResult:
        """移除 key 的过期时间，使其永久有效。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "PERSIST")
        ok = await _run(state, instance, inst, lambda c: c.persist(key))
        return RedisBoolResult(instance=instance, key=key, exists=bool(ok))

    # ==================== key 遍历 ====================
    @mcp.tool
    async def redis_scan(
        instance: str,
        match: str = "*",
        count: int = 100,
        cursor: str = "0",
        state: DbState = Depends(get_state),
    ) -> RedisScanResult:
        """用 SCAN 增量遍历匹配的 key（生产环境推荐；比 KEYS 安全）。

        SCAN 是游标式遍历，单次只返回一部分。要继续遍历，把返回的 cursor
        原样传回下次调用，直到 cursor 为 "0" 表示遍历结束。

        Args:
            instance: 实例名称。
            match: 匹配模式，如 user:*。
            count: 每次扫描的提示数量（非精确值）。
            cursor: 上次返回的游标，"0" 表示从头开始。

        Returns:
            本次命中的 key 与下一个 cursor（finished=true 表示已遍历完）。
        """
        inst = _redis(state, instance)
        limits = state.config.limits
        next_cursor, keys = await _run(
            state, instance, inst,
            lambda c: c.scan(cursor=cursor, match=match, count=count),
        )
        next_cursor = next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor)
        truncated = len(keys) > limits.max_rows
        return RedisScanResult(
            instance=instance,
            cursor=next_cursor,
            keys=[str(k) for k in keys[: limits.max_rows]],
            count=len(keys),
            finished=next_cursor == "0" and not truncated,
        )

    @mcp.tool
    async def redis_keys(
        instance: str, pattern: str = "*", state: DbState = Depends(get_state)
    ) -> RedisKeysResult:
        """用 KEYS 查找匹配的 key（**大数据集上会阻塞 Redis，慎用**）。

        生产实例优先用 redis_scan。本工具适合小实例或调试场景。

        Args:
            instance: 实例名称。
            pattern: 匹配模式，如 user:*。
        """
        inst = _redis(state, instance)
        limits = state.config.limits
        all_keys = await _run(state, instance, inst, lambda c: c.keys(pattern))
        truncated = len(all_keys) > limits.max_rows
        return RedisKeysResult(
            instance=instance,
            pattern=pattern,
            keys=[str(k) for k in all_keys[: limits.max_rows]],
            count=len(all_keys),
            truncated=truncated,
        )

    # ==================== hash ====================
    @mcp.tool
    async def redis_hash_get(
        instance: str,
        key: str,
        fields: list[str] | None = None,
        state: DbState = Depends(get_state),
    ) -> RedisHashResult:
        """读取 hash 的字段。不指定 fields 则返回全部字段。

        Args:
            instance: 实例名称。
            key: 键名。
            fields: 要读取的字段名列表；不填则取全部。
        """
        inst = _redis(state, instance)
        if fields:
            values = await _run(state, instance, inst, lambda c: c.hmget(key, fields))
            data = {f: ("(nil)" if v is None else str(v)) for f, v in zip(fields, values)}
        else:
            raw = await _run(state, instance, inst, lambda c: c.hgetall(key))
            data = {str(k): str(v) for k, v in raw.items()}
        return RedisHashResult(
            instance=instance, key=key, fields=data, count=len(data)
        )

    @mcp.tool
    async def redis_hash_set(
        instance: str,
        key: str,
        mapping: dict[str, str],
        state: DbState = Depends(get_state),
    ) -> RedisSetResult:
        """设置 hash 的一个或多个字段。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            mapping: 字段名到值的映射，如 {"name": "alice", "age": "30"}。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "HSET")
        if not mapping:
            raise StatementNotAllowedError("mapping 不能为空")
        await _run(state, instance, inst, lambda c: c.hset(key, mapping=mapping))
        return RedisSetResult(instance=instance, key=key, value=mapping)

    @mcp.tool
    async def redis_hash_del(
        instance: str, key: str, fields: list[str], state: DbState = Depends(get_state)
    ) -> RedisDeleteResult:
        """删除 hash 的一个或多个字段。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            fields: 要删除的字段名列表。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "HDEL")
        if not fields:
            raise StatementNotAllowedError("fields 不能为空")
        deleted = await _run(state, instance, inst, lambda c: c.hdel(key, *fields))
        return RedisDeleteResult(
            instance=instance, deleted=int(deleted), requested=len(fields)
        )

    # ==================== list ====================
    @mcp.tool
    async def redis_list_range(
        instance: str,
        key: str,
        start: int = 0,
        stop: int = -1,
        state: DbState = Depends(get_state),
    ) -> RedisListResult:
        """读取 list 指定区间（LRANGE），默认取全部。

        Args:
            instance: 实例名称。
            key: 键名。
            start: 起始下标（0 为首，负数为倒数）。
            stop: 结束下标（-1 为末尾，闭区间）。
        """
        inst = _redis(state, instance)
        limits = state.config.limits
        values = await _run(state, instance, inst, lambda c: c.lrange(key, start, stop))
        length = await _run(state, instance, inst, lambda c: c.llen(key))
        truncated = len(values) > limits.max_rows
        return RedisListResult(
            instance=instance, key=key,
            values=[str(v) for v in values[: limits.max_rows]],
            length=int(length), truncated=truncated,
        )

    @mcp.tool
    async def redis_list_push(
        instance: str,
        key: str,
        values: list[str],
        left: bool = False,
        state: DbState = Depends(get_state),
    ) -> RedisListResult:
        """向 list 推入元素（默认尾部 RPUSH，left=true 则头部 LPUSH）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            values: 要推入的元素列表。
            left: true 用 LPUSH（头部），false 用 RPUSH（尾部）。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "LPUSH" if left else "RPUSH")
        if not values:
            raise StatementNotAllowedError("values 不能为空")
        length = await _run(
            state, instance, inst,
            lambda c: c.lpush(key, *values) if left else c.rpush(key, *values),
        )
        return RedisListResult(
            instance=instance, key=key, values=values, length=int(length)
        )

    @mcp.tool
    async def redis_list_pop(
        instance: str, key: str, left: bool = True, count: int = 1,
        state: DbState = Depends(get_state),
    ) -> RedisListResult:
        """从 list 弹出元素（默认头部 LPOP）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            left: true 用 LPOP（头部），false 用 RPOP（尾部）。
            count: 弹出元素个数。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "LPOP" if left else "RPOP")
        popped = await _run(
            state, instance, inst,
            lambda c: c.lpop(key, count) if left else c.rpop(key, count),
        )
        if popped is None:
            values: list[str] = []
        elif isinstance(popped, list):
            values = [str(v) for v in popped]
        else:
            values = [str(popped)]
        return RedisListResult(instance=instance, key=key, values=values)

    # ==================== set ====================
    @mcp.tool
    async def redis_set_members(
        instance: str, key: str, state: DbState = Depends(get_state)
    ) -> RedisSetMembersResult:
        """列出 set 的所有成员（SMEMBERS）。

        Args:
            instance: 实例名称。
            key: 键名。
        """
        inst = _redis(state, instance)
        limits = state.config.limits
        members = await _run(state, instance, inst, lambda c: c.smembers(key))
        truncated = len(members) > limits.max_rows
        return RedisSetMembersResult(
            instance=instance, key=key,
            members=sorted(str(m) for m in members)[: limits.max_rows],
            count=len(members), truncated=truncated,
        )

    @mcp.tool
    async def redis_set_add(
        instance: str, key: str, members: list[str], state: DbState = Depends(get_state)
    ) -> RedisSetMembersResult:
        """向 set 添加成员。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            members: 要添加的成员列表。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "SADD")
        if not members:
            raise StatementNotAllowedError("members 不能为空")
        added = await _run(state, instance, inst, lambda c: c.sadd(key, *members))
        return RedisSetMembersResult(
            instance=instance, key=key, members=members, count=int(added)
        )

    @mcp.tool
    async def redis_set_remove(
        instance: str, key: str, members: list[str], state: DbState = Depends(get_state)
    ) -> RedisDeleteResult:
        """从 set 移除成员（SREM）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            members: 要移除的成员列表。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "SREM")
        if not members:
            raise StatementNotAllowedError("members 不能为空")
        removed = await _run(state, instance, inst, lambda c: c.srem(key, *members))
        return RedisDeleteResult(
            instance=instance, deleted=int(removed), requested=len(members)
        )

    # ==================== zset ====================
    @mcp.tool
    async def redis_zset_range(
        instance: str,
        key: str,
        start: int = 0,
        stop: int = -1,
        with_scores: bool = True,
        desc: bool = False,
        state: DbState = Depends(get_state),
    ) -> RedisZsetResult:
        """按排名读取 zset 成员（ZRANGE），可带分数、可倒序。

        Args:
            instance: 实例名称。
            key: 键名。
            start: 起始排名（0 为最高分）。
            stop: 结束排名（-1 为末尾，闭区间）。
            with_scores: 是否返回分数。
            desc: true 则按分数从高到低（ZREVRANGE）。
        """
        inst = _redis(state, instance)
        limits = state.config.limits
        raw = await _run(
            state, instance, inst,
            lambda c: _zrange(c, key, start, stop, with_scores, desc),
        )
        members = _normalize_zset(raw, with_scores)
        truncated = len(members) > limits.max_rows
        return RedisZsetResult(
            instance=instance, key=key,
            members=members[: limits.max_rows],
            count=len(members), truncated=truncated,
        )

    @mcp.tool
    async def redis_zset_add(
        instance: str,
        key: str,
        members: dict[str, float],
        state: DbState = Depends(get_state),
    ) -> RedisSetResult:
        """向 zset 添加成员及其分数（ZADD）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            members: 成员到分数的映射，如 {"alice": 95.5, "bob": 88}。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "ZADD")
        if not members:
            raise StatementNotAllowedError("members 不能为空")
        added = await _run(
            state, instance, inst, lambda c: c.zadd(key, members)
        )
        return RedisSetResult(instance=instance, key=key, value={"added": int(added)})

    @mcp.tool
    async def redis_zset_remove(
        instance: str, key: str, members: list[str], state: DbState = Depends(get_state)
    ) -> RedisDeleteResult:
        """从 zset 移除成员（ZREM）。

        Args:
            instance: 实例名称（权限必须为 write）。
            key: 键名。
            members: 要移除的成员列表。
        """
        inst = _redis(state, instance)
        _require_write(inst, instance, "ZREM")
        if not members:
            raise StatementNotAllowedError("members 不能为空")
        removed = await _run(state, instance, inst, lambda c: c.zrem(key, *members))
        return RedisDeleteResult(
            instance=instance, deleted=int(removed), requested=len(members)
        )


# ---------------------------------------------------------------- 内部辅助


async def _type_and_ttl(client, key: str) -> tuple[str, int | None]:
    """一次拿类型与 TTL；key 不存在时 TTL 为 None（避免展示 -2 的困惑）。"""
    value_type = await client.type(key)
    value_type = value_type.decode() if isinstance(value_type, bytes) else str(value_type)
    if value_type == "none":
        return value_type, None
    ttl = int(await client.ttl(key))
    return value_type, ttl


async def _zrange(client, key: str, start: int, stop: int, with_scores: bool, desc: bool):
    fn = client.zrevrange if desc else client.zrange
    return await fn(key, start, stop, withscores=with_scores)


def _normalize_zset(raw, with_scores: bool) -> list[dict[str, object]]:
    """把 ZRANGE 的返回（成员列表或 [成员, 分数] 对）归一成 [{member, score}]。"""
    if not with_scores:
        return [{"member": str(m)} for m in raw]
    if isinstance(raw, dict):
        return [{"member": str(m), "score": float(s)} for m, s in raw.items()]
    return [
        {"member": str(m), "score": float(s)} for m, s in raw
    ]
