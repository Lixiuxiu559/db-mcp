"""Token 显式到期的存储、HTTP、活动会话、排队和重试契约。"""

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx2 import ASGITransport, AsyncClient
from mcp.shared.exceptions import MCPError

from db_mcp.auth import IdentityError, IdentityStore
from db_mcp.auth.verifier import StoreTokenVerifier
from db_mcp.errors import PermissionDeniedError
from db_mcp.server import create_mcp


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


@pytest.fixture
def clock(monkeypatch):
    value = [time.time()]
    monkeypatch.setattr(time, "time", lambda: value[0])
    return value


async def login(web):
    response = await web.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    web.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def test_expiry_http_lifecycle_normalization_restart_and_copy(gw, clock):
    clock[0] = math.floor(clock[0]) + 0.1
    deadline = clock[0] + 20.4
    normalized = stamp(deadline)
    offset = datetime.fromtimestamp(deadline, timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).isoformat(timespec="milliseconds")
    app = gw.mcp.http_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as web:
        await login(web)
        created = await web.post("/api/tokens", json={
            "name": "expiring", "workspace_id": gw.workspace_id, "expires_at": offset,
        })
        assert created.status_code == 201
        value, item = created.json()["token"], created.json()["item"]
        assert item["expires_at"] == normalized and item["expired"] is False
        path = f"/api/tokens/{item['id']}"
        long_lived = (await web.post("/api/tokens", json={
            "name": "long-lived", "workspace_id": gw.workspace_id,
        })).json()
        assert long_lived["item"]["expires_at"] is None
        assert long_lived["item"]["expired"] is False
        access = await StoreTokenVerifier(gw.state.identity).verify_token(value)
        assert access.expires_at == math.ceil(deadline)
        row = dict(gw.state.identity._connection().execute(
            "SELECT * FROM tokens WHERE id=?", (item["id"],)
        ).fetchone())
        assert gw.state.identity.verify_token(value)
        used = gw.state.identity.token_info(item["id"])["last_used_at"]
    key = gw.state.identity.token_key_path.read_bytes()
    await gw.state.shutdown()
    restarted, state = create_mcp(gw.cfg_file)
    try:
        restored = dict(state.identity._connection().execute(
            "SELECT * FROM tokens WHERE id=?", (item["id"],)
        ).fetchone())
        for field in ("id", "token_hash", "token_ciphertext", "workspace_id", "expires_at"):
            assert restored[field] == row[field]
        assert state.identity.token_key_path.read_bytes() == key
        clock[0] = deadline - 0.001
        assert state.identity.verify_token(value)
        used = state.identity.token_info(item["id"])["last_used_at"]
        for now in (deadline, deadline + 0.001):
            clock[0] = now
            assert state.identity.verify_token(value) is None
            assert not state.identity.active_binding(item["id"], gw.workspace_id)
            assert state.identity.token_info(item["id"])["expired"] is True
            assert state.identity.token_info(item["id"])["enabled"] is True
            assert state.identity.token_info(item["id"])["last_used_at"] == used
        async with AsyncClient(transport=ASGITransport(app=restarted.http_app()), base_url="http://t") as web:
            await login(web)
            listed = (await web.get("/api/tokens")).json()["tokens"]
            assert next(t for t in listed if t["id"] == item["id"])["expired"] is True
            assert (await web.post(path + "/reveal")).json() == {"token": value}
            # 到期不阻止管理元数据，重传相同的截止时间也不视为调整。
            assert (await web.patch(path, json={"name": "renamed"})).status_code == 200
            assert (await web.patch(path, json={"expires_at": normalized})).status_code == 200
            future = stamp(deadline + 50)
            changed = await web.patch(path, json={"expires_at": future})
            assert changed.status_code == 200 and changed.json()["expired"] is False
            assert state.identity.verify_token(value)
            cleared = await web.patch(path, json={"expires_at": None})
            assert cleared.status_code == 200 and cleared.json()["expires_at"] is None
            clock[0] = deadline + 100
            assert state.identity.verify_token(value)
            assert state.identity.verify_token(long_lived["token"])
            assert (await web.delete(path)).status_code == 200
            assert (await web.patch(path, json={"expires_at": None})).status_code == 400
            assert state.identity.verify_token(value) is None
    finally:
        await state.shutdown()


@pytest.mark.parametrize("invalid", ["", 42, True, [], {}, "2030-01-01", "2030-01-01T00:00:00",
                                    "2030-99-01T00:00:00Z", "2030-01-01T00:00:00+24:00",
                                    "2030-01-01T00:00:00+00:60", "2030-01-01T00:00:00-01:99",
                                    "2000-01-01T00:00:00Z", "now"])
async def test_invalid_expiration_has_no_identity_or_success_outbox_effect(gw, clock, invalid):
    async with AsyncClient(transport=ASGITransport(app=gw.mcp.http_app()), base_url="http://t") as web:
        await login(web)
        await gw.state.flush_identity_events()
        before = gw.state.identity.list_tokens()
        key = gw.state.identity.token_key_path.read_bytes()
        response = await web.post("/api/tokens", json={
            "name": "invalid", "workspace_id": gw.workspace_id, "expires_at": invalid,
        })
        assert response.status_code == 400
        response = await web.patch(f"/api/tokens/{gw.token['id']}", json={
            "name": "must-not-change", "expires_at": invalid,
        })
        assert response.status_code == 400
        assert gw.state.identity.list_tokens() == before
        assert gw.state.identity.token_key_path.read_bytes() == key
        assert gw.state.identity.pending_events() == []
        rows, _ = gw.state.audit.query(kind="person", q="token.", limit=100)
        assert all(r["status"] == "error" for r in rows)
        assert all(r["action"] in {"token.create", "token.update"} for r in rows)


async def test_current_cutoff_rejected_and_failed_create_never_creates_key(tmp_path, clock):
    store = IdentityStore(tmp_path / "service.db")
    try:
        workspace = store.save_workspace("project")["id"]
        clock[0] = math.floor(clock[0])
        for invalid in (stamp(clock[0]), stamp(clock[0] - 0.001), ""):
            with pytest.raises(IdentityError):
                store.create_token("client", "creator", workspace, expires_at=invalid,
                                   event={"action": "token.create"})
        assert not store.token_key_path.exists()
        assert store.list_tokens() == [] and store.pending_events() == []
    finally:
        store.close()


def test_old_schema_adds_nullable_expiry_without_changing_credentials(tmp_path, clock):
    path = tmp_path / "service.db"
    secret = "legacy-secret-only-in-test"
    digest = hashlib.sha256(secret.encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE tokens (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, prefix TEXT, token_hash TEXT,
            enabled INTEGER, revoked INTEGER, created_by TEXT, created_at TEXT,
            last_used_at TEXT, workspace_id TEXT, token_ciphertext TEXT
        )""")
        conn.execute("INSERT INTO tokens VALUES('old','old','','prefix',?,1,0,NULL,'',NULL,'space',NULL)",
                     (digest,))
    store = IdentityStore(path)
    try:
        with store._connection() as conn:
            conn.execute("INSERT INTO workspaces VALUES('space','project')")
        assert store.token_info("old")["expires_at"] is None
        assert store.verify_token(secret)["id"] == "old"
        assert store.active_binding("old", "space")
        assert not store.token_key_path.exists()
        assert store._connection().execute("SELECT token_hash FROM tokens").fetchone()[0] == digest
    finally:
        store.close()


@pytest.mark.parametrize("corrupt", ["", "not-a-date", "2030-01-01T00:00:00", 42])
async def test_corrupted_persisted_expiry_is_fail_closed_but_auditable(gw, corrupt):
    used = gw.state.identity.token_info(gw.token["id"])["last_used_at"]
    with gw.state.identity._connection() as conn:
        conn.execute("UPDATE tokens SET expires_at=? WHERE id=?", (corrupt, gw.token["id"]))
    verifier = StoreTokenVerifier(gw.state.identity, gw.state.audit)
    assert await verifier.verify_token(gw.secret) is None
    assert not gw.state.identity.active_binding(gw.token["id"], gw.workspace_id)
    assert gw.state.identity.token_info(gw.token["id"])["expired"] is True
    assert gw.state.identity.token_info(gw.token["id"])["last_used_at"] == used
    row = gw.state.audit.query(status="denied")[0][0]
    assert row["token_id"] == gw.token["id"] and "到期" in row["error"]
    assert gw.secret not in json.dumps(row)


async def test_established_http_mcp_session_rechecks_precise_expiration(gw, harness, clock):
    clock[0] = math.floor(clock[0]) + 0.1
    deadline = clock[0] + 20.4
    gw.state.identity.update_token(gw.token["id"], {"expires_at": stamp(deadline)})
    app = gw.mcp.http_app()
    statuses = []

    async def capture(response):
        statuses.append(response.status_code)

    def http_client(**kwargs):
        hooks = kwargs.pop("event_hooks", {})
        hooks.setdefault("response", []).append(capture)
        return AsyncClient(transport=ASGITransport(app=app), event_hooks=hooks, **kwargs)

    transport = StreamableHttpTransport(
        "http://t/mcp", headers={"Authorization": f"Bearer {gw.secret}"},
        httpx_client_factory=http_client,
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(Client(transport))
        assert not (await client.call_tool("db_ping", {"instance": "pg-read"})).is_error
        used = gw.state.identity.token_info(gw.token["id"])["last_used_at"]
        attempts = len(harness.connect_calls)
        # 相同HTTP会话在精确截止（ceil秒之前）不能继续访问。
        clock[0] = deadline
        with pytest.raises(MCPError):
            await client.call_tool("db_ping", {"instance": "pg-read"})
        assert 401 in statuses
        assert len(harness.connect_calls) == attempts
        assert gw.state.identity.token_info(gw.token["id"])["last_used_at"] == used
        denied = gw.state.audit.query(status="denied")[0]
        assert any(r["token_id"] == gw.token["id"] and "到期" in r["error"] for r in denied)
        assert gw.secret not in json.dumps(denied)


@pytest.mark.parametrize("phase", ["waiting", "retry"])
async def test_expiration_during_pool_wait_or_retry_prevents_new_external_attempt(
    gw, harness, inprocess_auth, clock, phase
):
    pool = gw.state.pool
    instance = gw.state.config.get_instance("pg-read")
    deadline = math.floor(clock[0]) + 20.5
    gw.state.identity.update_token(gw.token["id"], {"expires_at": stamp(deadline)})
    calls = 0

    async def execute(_):
        nonlocal calls
        calls += 1
        clock[0] = deadline
        raise ConnectionResetError("retry from temporary test fixture")

    if phase == "waiting":
        blocked = asyncio.Semaphore(0)
        pool._sems["pg-read"] = blocked
        operation = asyncio.create_task(pool.run_with_retry("pg-read", instance, execute))
        await asyncio.sleep(0)
        clock[0] = deadline
        blocked.release()
        with pytest.raises(PermissionDeniedError, match="到期"):
            await operation
        assert calls == 0 and harness.connect_calls == []
    else:
        with pytest.raises(PermissionDeniedError, match="到期"):
            await pool.run_with_retry("pg-read", instance, execute)
        assert calls == 1 and len(harness.connect_calls) == 1


async def test_cached_access_token_after_expiration_preserves_denied_audit_identity(
    gw, harness, inprocess_auth, clock
):
    deadline = math.floor(clock[0]) + 20.5
    gw.state.identity.update_token(gw.token["id"], {"expires_at": stamp(deadline)})
    async with Client(gw.mcp) as client:
        clock[0] = deadline
        result = await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"},
                                        raise_on_error=False)
        assert result.is_error and "到期" in result.content[0].text
        assert harness.connect_calls == [] and harness.pg_statements == []
        rows = gw.state.audit.query(status="denied")[0]
        assert len(rows) == 1 and rows[0]["token_id"] == gw.token["id"]
        assert rows[0]["token_name"] == gw.token["name"]
        assert rows[0]["workspace_id"] is None and rows[0]["target_host"] is None
        assert gw.secret not in json.dumps(rows)


def test_normalized_millisecond_cutoff_must_still_be_future(tmp_path, clock):
    store = IdentityStore(tmp_path / "service.db")
    try:
        workspace = store.save_workspace("project")["id"]
        clock[0] = math.floor(clock[0]) + 0.0008
        raw = datetime.fromtimestamp(math.floor(clock[0]) + 0.0009, timezone.utc).isoformat(
            timespec="microseconds"
        )
        with pytest.raises(IdentityError, match="晚于当前"):
            store.create_token("client", "creator", workspace, expires_at=raw,
                               event={"action": "token.create"})
        assert store.list_tokens() == [] and store.pending_events() == []
        assert not store.token_key_path.exists()
        _, item = store.create_token("client", "creator", workspace, expires_at=None)
        assert item["expires_at"] is None and item["expired"] is False
    finally:
        store.close()


@pytest.mark.parametrize("wait_phase", ["connect", "acquire"])
async def test_expiration_during_connection_wait_prevents_sql(
    gw, harness, inprocess_auth, clock, monkeypatch, wait_phase
):
    deadline = math.floor(clock[0]) + 20.5
    gw.state.identity.update_token(gw.token["id"], {"expires_at": stamp(deadline)})
    waiting, release = asyncio.Event(), asyncio.Event()
    original = harness.create_pool

    async def connect(**kwargs):
        pool = await original(**kwargs)
        if wait_phase == "connect":
            waiting.set()
            await release.wait()
        else:
            acquire = pool.acquire

            class PendingAcquire:
                async def __aenter__(self):
                    waiting.set()
                    await release.wait()
                    self.actual = acquire()
                    return await self.actual.__aenter__()

                async def __aexit__(self, *args):
                    return await self.actual.__aexit__(*args)

            pool.acquire = PendingAcquire
        return pool

    monkeypatch.setattr("asyncpg.create_pool", connect)
    async with Client(gw.mcp) as client:
        result = asyncio.create_task(client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT 1"}, raise_on_error=False
        ))
        await asyncio.wait_for(waiting.wait(), timeout=5)
        clock[0] = deadline
        release.set()
        denied = await result
        assert denied.is_error and "到期" in denied.content[0].text
        assert len(harness.connect_calls) == 1 and harness.pg_statements == []
        rows = gw.state.audit.query(status="denied")[0]
        assert len(rows) == 1 and rows[0]["token_id"] == gw.token["id"]


@pytest.mark.parametrize("boundary", ["connect", "cached-operation", "acquired-sql"])
async def test_authorization_worker_result_crossing_cutoff_cannot_start_external_operation(
    gw, harness, inprocess_auth, clock, monkeypatch, boundary
):
    from threading import Event

    from db_mcp.db import pg

    state, pool = gw.state, gw.state.pool
    instance = state.config.get_instance("pg-read")
    deadline = math.floor(clock[0]) + 20.5
    state.identity.update_token(gw.token["id"], {"expires_at": stamp(deadline)})
    if boundary != "connect":
        # 预建连接后清空观测；本次调用不允许再有新连接或SQL。
        cached = await pool.get_pg_pool("pg-read", instance)
        harness.connect_calls.clear()
    ready, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = state.identity.active_token_info
    checks, operations = 0, 0
    pause_at = 3 if boundary == "cached-operation" else 1
    observed = []

    def hold_valid_result(*args):
        nonlocal checks
        checks += 1
        result = original(*args)
        if checks == pause_at:
            assert result is not None
            observed.append(result)
            # worker 已完成到期前校验；将返回结果扣住，事件循环先跨过截止。
            loop.call_soon_threadsafe(ready.set)
            assert release.wait(timeout=5)
        return result

    async def execute(connection):
        nonlocal operations
        operations += 1
        async with connection.acquire() as conn:
            return await conn.fetchval("SELECT 1")

    monkeypatch.setattr(state.identity, "active_token_info", hold_valid_result)
    if boundary == "connect":
        coroutine = pool.get_pg_pool("pg-read", instance)
    elif boundary == "cached-operation":
        coroutine = pool.run_with_retry("pg-read", instance, execute)
    else:
        coroutine = pg.run(
            cached, "SELECT 1", None, read_only=True, max_rows=10,
            authorize=lambda: state.authorize_attempt("pg-read", instance),
        )
    task = asyncio.create_task(coroutine)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert observed[0]["expires_at"] == stamp(deadline)
        clock[0] = deadline
        release.set()
        with pytest.raises(PermissionDeniedError, match="到期"):
            await task
        assert harness.connect_calls == [] and harness.pg_statements == []
        assert operations == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("field", ["id", "workspace_id"])
async def test_authorization_rejects_worker_metadata_for_a_different_binding(
    gw, harness, inprocess_auth, monkeypatch, field
):
    identity = gw.state.identity
    item = identity.active_token_info(gw.token["id"], gw.workspace_id)
    assert item == {"id": gw.token["id"], "workspace_id": gw.workspace_id, "expires_at": None}
    item[field] = "another-binding"
    monkeypatch.setattr(identity, "active_token_info", lambda *args: item)
    instance = gw.state.config.get_instance("pg-read")
    with pytest.raises(PermissionDeniedError):
        await gw.state.pool.get_pg_pool("pg-read", instance)
    assert harness.connect_calls == [] and harness.pg_statements == []
