"""仅在已验证的 MCP 工具调用期间设置工作空间。"""

from contextvars import ContextVar

from ..errors import PermissionDeniedError

# (token_id, workspace_id) 均由服务端验证后的持久化记录产生。
call_scope: ContextVar[tuple[str, str] | None] = ContextVar("db_mcp_scope", default=None)


def current_workspace() -> str:
    scope = call_scope.get()
    if not scope:
        raise PermissionDeniedError("MCP Token 缺少有效的工作空间")
    return scope[1]
