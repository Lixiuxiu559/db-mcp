"""工作空间管理、真实 MCP 协议请求和外部操作前授权的回归测试。"""

import asyncio
from contextlib import AsyncExitStack

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx2 import ASGITransport, AsyncClient

from db_mcp.errors import InstanceNotFoundError, PermissionDeniedError


@pytest.fixture
async def console(gw):
    app = gw.mcp.http_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as web:
        setup = await web.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        web.headers["X-CSRF-Token"] = setup.json()["csrf_token"]
        yield app, web


async def test_workspace_lifecycle_and_reference_validation(console, gw):
    _, web = console
    spaces = "/api/workspaces"
    tokens = "/api/tokens"
    workspace = (await web.post(spaces, json={"name": " 项目 B "})).json()["workspace"]
    wid = workspace["id"]
    path = f"{spaces}/{wid}"
    assert workspace["name"] == "项目 B"
    assert workspace in (await web.get(spaces)).json()["workspaces"]
    assert (await web.post(spaces, json={"name": "项目 B"})).status_code == 400
    assert (await web.patch(path, json={"name": "新名称"})).json()["workspace"]["id"] == wid
    for body in ({}, {"workspace_id": "unknown"}, {"workspace_id": [wid]}):
        before = gw.state.identity.list_tokens()
        assert (await web.post(tokens, json={"name": "ci", **body})).status_code == 400
        assert gw.state.identity.list_tokens() == before
        assert gw.state.identity.pending_events() == []
    assert gw.state.audit.query(kind="person", q="token.create", status="ok")[1] == 0
    data = (await web.post(tokens, json={"name": "ci", "workspace_id": wid})).json()
    token_url = f"{tokens}/{data['item']['id']}"
    assert data["item"]["workspace_id"] == wid
    for workspace_id in (wid, gw.workspace_id):
        response = await web.patch(token_url, json={"workspace_id": workspace_id, "name": "must-not-change"})
        assert response.status_code == 400
        assert gw.state.identity.token_info(data["item"]["id"]) == data["item"]
        assert gw.state.identity.pending_events() == []
    assert gw.state.audit.query(kind="person", q="token.update", status="ok")[1] == 0
    assert (await web.patch(token_url, json={"enabled": False})).status_code == 200
    assert (await web.delete(path)).status_code == 409
    assert (await web.delete(token_url)).status_code == 200

    resource = {"name": "staging", "type": "redis", "host": "example.invalid"}
    added = (await web.post("/api/instances", json=resource)).json()
    assert added["workspace_ids"] == []
    resource_url = "/api/instances/staging"
    assert (await web.put(resource_url, json={**resource, "workspace_ids": [wid]})).status_code == 200
    assert (await web.delete(path)).status_code == 409
    for invalid in (None, wid, ["unknown"], [42]):
        assert (await web.put(resource_url, json={**resource, "workspace_ids": invalid})).status_code == 400
    assert (await web.put(resource_url, json=resource)).json()["workspace_ids"] == [wid]
    assert (await web.put(resource_url, json={**resource, "workspace_ids": []})).status_code == 200
    assert (await web.delete(path)).status_code == 200
    assert (await web.post(spaces, json={"name": "新名称"})).json()["workspace"]["id"] != wid
    history = (await web.get("/api/audit?kind=person&limit=100")).json()["records"]
    assert any(r["action"] == "instance.update" and wid in (r["summary"] or "") for r in history)
    assert any(r["action"] == "token.create" and wid in (r["summary"] or "") for r in history)


async def test_live_scoped_sessions_and_unresolved_target_audit(console, gw, harness):
    app, web = console
    project_a = gw.workspace_id
    project_b = (await web.post("/api/workspaces", json={"name": "Project B"})).json()["workspace"]["id"]
    token_b = (await web.post("/api/tokens", json={"name": "B-token", "workspace_id": project_b})).json()
    resource = {"name": "shared", "host": "shared.invalid", "type": "redis", "permissions": "write"}
    await web.post("/api/instances", json={**resource, "workspace_ids": [project_a, project_b]})
    await web.post("/api/instances", json={**resource, "name": "b-only", "workspace_ids": [project_b]})
    await web.post("/api/instances", json={**resource, "name": "unassigned"})
    assert len((await web.get("/api/instances")).json()["instances"]) == 8

    def transport(secret):
        return StreamableHttpTransport(
            "http://t/mcp", headers={"Authorization": f"Bearer {secret}", "X-Workspace-Id": project_a},
            httpx_client_factory=lambda **kwargs: AsyncClient(transport=ASGITransport(app=app), **kwargs),
        )

    async def names(client):
        listing = await client.call_tool("list_instances", {})
        return {i.name for i in listing.data.instances}

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        a = await stack.enter_async_context(Client(transport(gw.secret)))
        b = await stack.enter_async_context(Client(transport(token_b["token"])))
        names_a, names_b = await asyncio.gather(names(a), names(b))
        assert names_b == {"shared", "b-only"}
        assert "unassigned" not in names_a and "b-only" not in names_a and "pg-read" in names_a
        for tool, arguments in (
            ("db_ping", {}), ("postgres_query", {"sql": "SELECT 1"}),
            ("postgres_list_schemas", {}), ("postgres_execute", {"sql": "DROP TABLE t", "confirm": True}),
            ("redis_get", {"key": "x"}), ("redis_set", {"key": "x", "value": "y"}),
        ):
            denied = await b.call_tool(tool, {"instance": "pg-read", **arguments}, raise_on_error=False)
            assert denied.is_error and "pg-write" not in denied.content[0].text
            assert "未找到实例" in denied.content[0].text
        spoofed = await b.call_tool("list_instances", {"workspace_id": project_a}, raise_on_error=False)
        assert spoofed.is_error
        assert harness.connect_calls == []
        assert not (await b.call_tool("redis_set", {"instance": "shared", "key": "x", "value": "y"})).is_error
        await web.patch(f"/api/workspaces/{project_b}", json={"name": "Renamed"})
        await web.put("/api/instances/shared", json={**resource, "workspace_ids": [project_a]})
        assert await names(b) == {"b-only"}
        denied = await b.call_tool("db_ping", {"instance": "shared"}, raise_on_error=False)
        assert denied.is_error
        assert not (await a.call_tool("db_ping", {"instance": "shared"})).is_error
        history = (await web.get(f"/api/audit?token_id={token_b['item']['id']}&limit=100")).json()["records"]
        assert any(r["status"] == "ok" and r["workspace_name"] == "Project B" and
                   r["target_host"] == "shared.invalid" for r in history)
        assert all(r["workspace_id"] == project_b for r in history)
        assert all(r["target_host"] is None for r in history if r["status"] == "denied")
        assert any(r["tool"] == "db_ping" and r["status"] == "denied" and
                   r["workspace_name"] == "Renamed" for r in history)


async def test_uncredentialed_inprocess_tool_call_denied(gw):
    async with Client(gw.mcp) as client:
        result = await client.call_tool("list_instances", {}, raise_on_error=False)
    assert result.is_error and "pg-read" not in result.content[0].text


async def test_waiting_and_retrying_driver_calls_recheck_authorization(gw, harness, inprocess_auth):
    pool, instance = gw.state.pool, gw.state.config.get_instance("pg-read")
    parked = asyncio.Semaphore(0)
    pool._sems["pg-read"] = parked
    executions = 0

    async def op(_):
        nonlocal executions
        executions += 1
        gw.state.identity.update_token(gw.token["id"], {"enabled": False})
        raise ConnectionResetError("dropped")

    blocked = asyncio.create_task(pool.run_with_retry("pg-read", instance, op))
    await asyncio.sleep(0)
    data = {**gw.cfg_file.raw["instances"]["pg-read"], "workspace_ids": []}
    await gw.state.upsert_instance("pg-read", data)
    parked.release()
    with pytest.raises(InstanceNotFoundError):
        await blocked
    assert harness.connect_calls == [] and executions == 0
    await gw.state.upsert_instance("pg-read", {**data, "workspace_ids": [gw.workspace_id]})
    with pytest.raises(PermissionDeniedError):
        await pool.run_with_retry("pg-read", instance, op)
    assert executions == 1 and len(harness.connect_calls) == 1
