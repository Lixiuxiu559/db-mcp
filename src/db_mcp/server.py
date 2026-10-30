"""FastMCP 服务组装：认证、生命周期、中间件、工具与网页路由。"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .audit import AuditMiddleware
from .auth import StoreTokenVerifier
from .config import ConfigFile
from .state import DbState
from .tools import inventory, postgres, redis
from .web.api import register_web_routes

INSTRUCTIONS = (
    "团队共享的数据库网关，可操作多台已配置的 PostgreSQL / Redis 实例。\n"
    "流程：先用 list_instances 获取实例名（instance）与读写权限，之后所有工具都用它指定实例：\n"
    "- 连通性排查: db_ping\n"
    "- PostgreSQL 只读查询: postgres_query（禁止写操作）\n"
    "- PostgreSQL 元数据: postgres_list_schemas / postgres_list_tables / "
    "postgres_describe_table / postgres_list_indexes\n"
    "- PostgreSQL 写入: postgres_execute（仅 write 实例；DROP/TRUNCATE/ALTER 需 confirm=true）\n"
    "- Redis 读取: redis_get / redis_mget / redis_type / redis_ttl / redis_exists / redis_scan / "
    "redis_hash_get / redis_list_range / redis_set_members / redis_zset_range\n"
    "- Redis 写入: redis_set / redis_del / redis_expire / redis_persist / redis_hash_set / "
    "redis_hash_del / redis_list_push / redis_list_pop / redis_set_add / redis_set_remove / "
    "redis_zset_add / redis_zset_remove（仅 write 实例）\n"
    "注意：权限为 read 的实例会拒绝一切写操作；每条语句都会记入审计日志。"
)


def create_mcp(cfg_file: ConfigFile) -> tuple[FastMCP, DbState]:
    state = DbState(cfg_file)

    @lifespan
    async def _app_lifespan(server: FastMCP):
        await state.startup()
        try:
            yield {"state": state}
        finally:
            await state.shutdown()

    mcp = FastMCP(
        name="db-mcp",
        instructions=INSTRUCTIONS,
        auth=StoreTokenVerifier(state.identity, state.audit),
        lifespan=_app_lifespan,
        mask_error_details=True,
    )
    mcp.add_middleware(AuditMiddleware(state.audit, state))
    inventory.register(mcp)
    postgres.register(mcp)
    redis.register(mcp)
    register_web_routes(mcp, state)
    return mcp, state
