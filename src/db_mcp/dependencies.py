"""FastMCP 依赖注入：把 DbState 注入工具函数。"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.dependencies import CurrentContext

from .state import DbState


def get_state(ctx: Context = CurrentContext()) -> DbState:
    return ctx.lifespan_context["state"]
