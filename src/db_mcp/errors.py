"""面向 LLM/用户的错误类型。

全部继承 ToolError：即使 mask_error_details=True，消息也会原文透传，
因此消息里应包含"下一步建议"，且绝不能包含密码等凭据。
"""

from fastmcp.exceptions import ToolError


class DbMcpError(ToolError):
    """db-mcp 工具错误基类。"""


class InstanceNotFoundError(DbMcpError):
    def __init__(self, name: str, available: list[str]):
        self.name = name
        self.available = available
        tips = "、".join(available) if available else "（尚未配置任何实例）"
        super().__init__(
            f"未找到实例 {name!r}。可用实例: {tips}。可先调用 list_instances 查看。"
        )


class InstanceDisabledError(DbMcpError):
    def __init__(self, name: str):
        super().__init__(f"实例 {name!r} 已禁用（配置中 enabled: false），请在管理端启用后重试。")


class PermissionDeniedError(DbMcpError):
    """实例权限不足：read 实例收到写操作。"""


class ReadOnlySQLRequiredError(PermissionDeniedError):
    """read 实例只允许只读查询。"""


class ConnectionFailedError(DbMcpError):
    """连接失败 / 认证失败。"""


class QueryTimeoutError(DbMcpError):
    """查询或命令执行超时。"""


class StatementNotAllowedError(DbMcpError):
    """语句被安全策略拒绝（多语句、非法类型等）。"""


class ConfirmationRequiredError(DbMcpError):
    """高危语句需要 confirm=true 二次确认。"""
