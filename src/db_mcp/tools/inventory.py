"""实例清单与连通性检查工具。"""

from __future__ import annotations

import asyncio
import time

from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from ..config import PostgresInstance, RedisInstance
from ..dependencies import get_state
from ..models import InstanceInfo, InstanceListResult, PingResult
from ..state import DbState


def _describe(inst: PostgresInstance | RedisInstance) -> str:
    if isinstance(inst, PostgresInstance):
        return f"{inst.host}:{inst.port}/{inst.database} ({inst.user})"
    return f"{inst.host}:{inst.port}/db{inst.database}"


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def list_instances(
        state: DbState = Depends(get_state),
    ) -> InstanceListResult:
        """列出所有已配置的数据库实例及其类型、读写权限和启用状态。

        返回所有实例的摘要信息，帮助了解当前可用的数据库资源。后续所有工具都用
        instance 参数指定实例，开始任务时建议先调用本工具。

        Returns:
            实例列表，每项含 name / type / permissions / enabled / 连接信息。
        """
        instances = []
        for name, inst in state.config.instances.items():
            info = InstanceInfo(
                name=name,
                type=inst.type,  # type: ignore[arg-type]
                permissions=inst.permissions,
                enabled=inst.enabled,
                host=inst.host,
                port=inst.port,
                database=getattr(inst, "database", None) if isinstance(inst, PostgresInstance) else None,
                user=getattr(inst, "user", None) if isinstance(inst, PostgresInstance) else None,
                schema_name=inst.schema_ if isinstance(inst, PostgresInstance) else None,
            )
            instances.append(info)
        return InstanceListResult(instances=instances)

    @mcp.tool
    async def db_ping(
        instance: str,
        state: DbState = Depends(get_state),
    ) -> PingResult:
        """检测某个数据库实例的连通性，返回状态与版本/延迟。

        不加载数据、不改动任何东西，纯粹用来确认"这个实例现在能不能连上"。
        排查"连不上数据库"类问题时应先调用本工具。

        Args:
            instance: 实例名称（先用 list_instances 获取）。
        """
        inst = state.config.resolve_instance(instance)
        limits = state.config.limits

        async def op(conn):
            start = time.monotonic()
            if isinstance(inst, PostgresInstance):
                async with conn.acquire() as c:
                    version = await c.fetchval("SELECT version()")
            else:
                info = conn.info()
                if asyncio.iscoroutine(info):  # redis-py 5/6 的 info() 是协程
                    info = await info
                version = info.get("redis_version")
            return version, round((time.monotonic() - start) * 1000, 1)

        try:
            version, latency = await asyncio.wait_for(
                state.pool.run_with_retry(instance, inst, op),
                timeout=limits.connect_timeout_seconds + 5,
            )
        except Exception as exc:  # noqa: BLE001 - 连通性检查：任何失败都转成结构化结果
            return PingResult(
                instance=instance, status="error", error=f"{type(exc).__name__}: {exc}"
            )
        return PingResult(
            instance=instance, status="ok", version=str(version) if version else None,
            latency_ms=latency,
        )
