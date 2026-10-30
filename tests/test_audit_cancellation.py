"""取消落在真实审计工作线程等待点时，不能继续执行数据库操作。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from db_mcp.audit import AuditMiddleware


@pytest.mark.parametrize(
    ("phase", "expected_operations"), [("update", 0), ("write", 0), ("complete", 1)]
)
async def test_cancel_during_slow_audit_thread_stays_cancelled(
    gw, monkeypatch, phase, expected_operations
):
    original_to_thread = asyncio.to_thread
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    delayed = False

    async def to_thread(operation, *args, **kwargs):
        nonlocal delayed
        if operation.__name__ != phase or delayed:
            return await original_to_thread(operation, *args, **kwargs)
        delayed = True

        def slow_write():
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(timeout=5), "审计线程未释放"
                return operation(*args, **kwargs)
            finally:
                loop.call_soon_threadsafe(finished.set)

        return await original_to_thread(slow_write)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    connections, operations = [], []

    async def connection(*_args):
        connections.append("connected")
        return object()

    async def operate(_connection):
        operations.append("executed")
        return SimpleNamespace(is_error=False, structured_content={"row_count": 1})

    monkeypatch.setattr(gw.state.pool, "_get", connection)

    async def call_next(_):
        return await gw.state.pool.run_with_retry(
            "pg-read", gw.state.config.get_instance("pg-read"), operate
        )

    context = SimpleNamespace(
        message=SimpleNamespace(name="postgres_query", arguments={"instance": "pg-read"}),
        fastmcp_context=None,
    )
    task = asyncio.create_task(AuditMiddleware(gw.state.audit, gw.state).on_call_tool(context, call_next))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert len(connections) == len(operations) == expected_operations
        assert gw.state.audit.query()[0][0]["status"] == "cancelled"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=3)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    # 已被取消的 to_thread 并不会中止原线程；迟到的原写入也不能把终态改回 ok/running。
    assert gw.state.audit.query()[0][0]["status"] == "cancelled"
