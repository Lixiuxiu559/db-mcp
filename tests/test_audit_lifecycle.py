"""审计身份、执行目标、生命周期与迁移的回归测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastmcp import Client

from db_mcp.audit import AuditLogger, AuditMiddleware, record_attempt


def context(tool="postgres_query", **arguments):
    return SimpleNamespace(
        message=SimpleNamespace(name=tool, arguments=arguments), fastmcp_context=None
    )


async def test_target_and_token_snapshots(gw, monkeypatch):
    value, token = gw.state.identity.create_token("ci-runner", "creator-id")
    monkeypatch.setattr(
        "db_mcp.audit.get_access_token",
        lambda: SimpleNamespace(client_id=token["id"], token=value),
    )
    monkeypatch.setattr(
        "db_mcp.audit.get_http_request",
        lambda: SimpleNamespace(client=SimpleNamespace(host="127.0.0.42")),
    )
    async with Client(gw.mcp) as client:
        await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"})
    record = gw.state.audit.query()[0][0]
    assert record["token_id"] == token["id"]
    assert record["token_name"] == "ci-runner"
    assert record["source_ip"] == "127.0.0.42"
    assert record["target_type"] == "postgres"
    assert record["target_host"] == "10.0.0.30"
    assert record["target_port"] == 5432
    assert record["target_database"] == "appdb"
    assert record["target_username"] == "reader"
    assert record["row_count"] == 1
    gw.state.identity.update_token(token["id"], {"name": "renamed"})
    entry = gw.state.config.get_instance("pg-read").model_dump(by_alias=True)
    entry.update(host="10.0.0.99", password="")
    await gw.state.upsert_instance("pg-read", entry)
    record = gw.state.audit.query()[0][0]
    assert record["token_name"] == "ci-runner"
    assert record["target_host"] == "10.0.0.30"


async def test_actual_pool_target_overrides_start_snapshot(gw, monkeypatch):
    """开始事件落库期间发生配置热更新时，实际执行目标必须覆盖旧快照。"""
    original_begin = gw.state.audit.begin_call

    async def begin(record):
        call_id = await original_begin(record)
        entry = gw.state.config.get_instance("pg-read").model_dump(by_alias=True)
        entry.update(host="10.0.0.99", password="")
        await gw.state.upsert_instance("pg-read", entry)
        return call_id

    monkeypatch.setattr(gw.state.audit, "begin_call", begin)
    async with Client(gw.mcp) as client:
        await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"})
    record = gw.state.audit.query()[0][0]
    assert record["target_host"] == "10.0.0.99"


async def test_redis_target_and_no_write_value_capture(gw):
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "redis_set", {"instance": "redis-write", "key": "user:1", "value": "private-body"}
        )
    record = gw.state.audit.query()[0][0]
    assert record["target_type"] == "redis"
    assert record["target_database"] == "1"
    assert record["statement"] == "SET user:1"
    assert "private-body" not in str(record)


async def test_sensitive_sql_and_driver_error_redaction(gw, monkeypatch):
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT 'pg-pw'"}
        )
    record = gw.state.audit.query()[0][0]
    assert "pg-pw" not in record["statement"]

    middleware = AuditMiddleware(gw.state.audit, gw.state)

    async def fail(_):
        raise ValueError("connection password=pg-pw token=secret-token")

    with pytest.raises(ValueError):
        await middleware.on_call_tool(context(instance="pg-read"), fail)
    record = gw.state.audit.query()[0][0]
    assert "pg-pw" not in record["error"]
    assert "secret-token" not in record["error"]


async def test_cancellation_finishes_call_and_attempt(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    middleware = AuditMiddleware(audit)
    executing = asyncio.Event()

    async def blocked(_):
        await record_attempt(1, "running")
        executing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await record_attempt(1, "cancelled", "操作结果未知")
            raise

    task = asyncio.create_task(middleware.on_call_tool(context(sql="SELECT 1"), blocked))
    await executing.wait()
    # 远端执行前已经存在 running 记录。
    assert audit.query()[0][0]["status"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = audit.query()[0][0]
    assert record["status"] == "cancelled"
    assert record["ended_at"] is not None
    with sqlite3.connect(audit.path) as conn:
        attempt = conn.execute("SELECT call_id,status FROM mcp_attempts").fetchone()
    assert attempt == (record["id"], "cancelled")
    audit.close()


async def test_timeout_and_policy_denial(gw):
    middleware = AuditMiddleware(gw.state.audit, gw.state)

    async def timeout(_):
        raise TimeoutError("query timed out")

    with pytest.raises(TimeoutError):
        await middleware.on_call_tool(context(instance="pg-read"), timeout)
    assert gw.state.audit.query()[0][0]["status"] == "timeout"
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_execute", {"instance": "pg-read", "sql": "UPDATE t SET x=1"},
            raise_on_error=False,
        )
    record = gw.state.audit.query()[0][0]
    assert record["status"] == "denied"
    assert record["statement"] == "UPDATE t SET x=1"


async def test_reconnect_attempts_share_call_id(gw, harness, monkeypatch):
    original = harness.pg_rows
    failed = False

    def rows(sql):
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionResetError("reset")
        return original(sql)

    monkeypatch.setattr(harness, "pg_rows", rows)
    async with Client(gw.mcp) as client:
        result = await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"})
    assert not result.is_error
    record = gw.state.audit.query()[0][0]
    with sqlite3.connect(gw.state.audit.path) as conn:
        attempts = conn.execute(
            "SELECT call_id,attempt,status FROM mcp_attempts ORDER BY attempt"
        ).fetchall()
    assert attempts == [(record["id"], 1, "error"), (record["id"], 2, "ok")]


async def test_restart_marks_unfinished_records_unknown(tmp_path):
    path = tmp_path / "audit.db"
    audit = AuditLogger(path)
    await audit.begin_call({"id": "call", "tool": "postgres_execute"})
    await audit.attempt("call", 1, "running", None)
    await audit.begin_person({"event_id": "person", "action": "instance.update"})
    audit.close()
    audit = AuditLogger(path)
    assert audit.query()[0][0]["status"] == "unknown"
    assert audit.query(kind="person")[0][0]["status"] == "unknown"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM mcp_attempts").fetchone()[0] == "unknown"
    audit.close()


async def test_person_event_idempotency_and_secret_summary(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    started = {
        "event_id": "edit-1", "actor_user_id": "user-id", "username": "alice",
        "action": "instance.update", "object_type": "instance", "object_id": "pg-main",
        "summary": {"password": "never-store-me", "host": "127.0.0.1"},
    }
    assert await audit.begin_person(started)
    original_ts = audit.query(kind="person")[0][0]["ts"]
    assert await audit.finish_person({**started, "status": "ok"})
    # outbox 重投不能覆盖 begin/finish 的最终结果。
    assert await audit.log_person({**started, "status": "running"})
    rows, count = audit.query(kind="person", user_id="user-id")
    assert count == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["ts"] == original_ts
    assert "never-store-me" not in rows[0]["summary"]
    audit.close()


async def test_retention_includes_person_attempts_and_legacy(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="milliseconds")
    await audit.log({"id": "old", "ts": old, "status": "ok"})
    await audit.attempt("old", 1, "ok", None)
    await audit.log_person({"event_id": "old-person", "ts": old, "status": "ok"})
    # 查询即时应用保留窗口，无需等待定时清理。
    assert audit.query()[1] == 0
    assert audit.query(kind="person")[1] == 0
    audit.prune()
    with sqlite3.connect(audit.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM person_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mcp_attempts").fetchone()[0] == 0
    audit.close()


async def test_retention_prunes_expired_orphan_attempts_without_deleting_recent(tmp_path):
    """调用开始落库失败但尝试恢复写入时，孤立尝试也受独立保留期约束。"""
    audit = AuditLogger(tmp_path / "audit.db")
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="milliseconds")
    await audit.attempt("orphan-expired", 1, "error", "original call marker was not persisted")
    await audit.attempt("orphan-recent", 1, "ok", None)
    await audit.log({"id": "active-call", "status": "ok"})
    await audit.attempt("active-call", 1, "ok", None)
    with sqlite3.connect(audit.path) as conn:
        conn.execute("UPDATE mcp_attempts SET ts=? WHERE call_id='orphan-expired'", [old])
    audit.prune()
    with sqlite3.connect(audit.path) as conn:
        remaining = conn.execute("SELECT call_id FROM mcp_attempts ORDER BY call_id").fetchall()
    assert remaining == [("active-call",), ("orphan-recent",)]
    assert audit.query()[0][0]["id"] == "active-call"
    audit.close()


def test_legacy_migration_is_idempotent_without_invented_token(tmp_path):
    path = tmp_path / "audit.db"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE audit_records(id INTEGER PRIMARY KEY,ts TEXT,user TEXT,tool TEXT,"
            "instance TEXT,kind TEXT,statement TEXT,status TEXT)"
        )
        conn.execute(
            "INSERT INTO audit_records VALUES(1,?,'alice','postgres_query','pg-main','sql','SELECT 1','ok')",
            [now],
        )
    for _ in range(2):
        audit = AuditLogger(path)
        records, count = audit.query()
        assert count == 1
        record = records[0]
        assert record["id"] == "legacy-1"
        assert record["legacy_id"] == 1
        assert record["statement"] == "SELECT 1"
        assert record["user"] == "alice"
        assert record["token_id"] is None
        assert record["target_host"] is None
        assert "历史记录" in record["token_name"]
        audit.close()


async def test_audit_files_private_and_reopenable(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    await audit.log({"tool": "postgres_query", "statement": "SELECT 1", "status": "ok"})
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / ("audit.db" + suffix)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    audit.close()
    await audit.log({"tool": "redis_get", "status": "ok"})
    assert audit.query()[1] == 2
    audit.close()


async def test_audit_failure_preserves_business_result(tmp_path, monkeypatch):
    audit = AuditLogger(tmp_path / "audit.db")

    def fail(*_):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(audit, "_write", fail)
    value = SimpleNamespace(is_error=False, structured_content={"row_count": 2})

    async def execute(_):
        return value

    assert await AuditMiddleware(audit).on_call_tool(context(sql="SELECT 1"), execute) is value
    audit.close()


async def test_delayed_duplicate_start_cannot_overwrite_finished_outcome(tmp_path):
    """取消兜底与线程写入竞争时，迟到的开始事件不能覆盖终态。"""
    audit = AuditLogger(tmp_path / "audit.db")
    call_id = await audit.begin_call({"id": "stable-call", "tool": "postgres_execute"})
    await audit.finish_call(call_id, {"status": "cancelled"})
    await audit.begin_call({"id": call_id, "tool": "postgres_execute"})
    assert audit.query()[0][0]["status"] == "cancelled"
    await audit.attempt(call_id, 1, "error", "disconnected")
    await audit.attempt(call_id, 1, "running", None)
    with sqlite3.connect(audit.path) as conn:
        assert conn.execute("SELECT status FROM mcp_attempts").fetchone()[0] == "error"
    audit.close()


async def test_password_changed_during_start_does_not_leak_in_driver_error(gw, monkeypatch):
    original_begin = gw.state.audit.begin_call

    async def begin(record):
        call_id = await original_begin(record)
        entry = gw.state.config.get_instance("pg-read").model_dump(by_alias=True)
        entry["password"] = "new-hot-reload-password"
        await gw.state.upsert_instance("pg-read", entry)
        return call_id

    async def connect(*_args, **_kwargs):
        raise ValueError("driver error with new-hot-reload-password")

    monkeypatch.setattr(gw.state.audit, "begin_call", begin)
    monkeypatch.setattr(gw.state.pool, "_get", connect)
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT 1"}, raise_on_error=False
        )
    record = gw.state.audit.query()[0][0]
    assert record["status"] == "error"
    assert "new-hot-reload-password" not in record["error"]
    with sqlite3.connect(gw.state.audit.path) as conn:
        error = conn.execute("SELECT error FROM mcp_attempts").fetchone()[0]
    assert "new-hot-reload-password" not in error
