"""Web 管理端 API 测试：鉴权、CRUD、热更新、测试连接、密码不回显。"""

from __future__ import annotations

import asyncpg
import pytest
from fastmcp import Client
from httpx2 import ASGITransport, AsyncClient
from mcp.shared.exceptions import MCPError


@pytest.fixture
def app(gw):
    identity = gw.state.identity
    identity.initialize("alice", "password-alice", identity.setup_key_path.read_text().strip())
    identity.create_user("bob", "password-bob")
    return gw.mcp.http_app()


async def request(app, method: str, path: str, token: str | None = None, **kwargs):
    headers = kwargs.pop("headers", {})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        if token in {"tok-alice", "tok-bob"}:
            username = "alice" if token == "tok-alice" else "bob"
            login = await client.post("/api/auth/login", json={
                "username": username, "password": f"password-{username}",
            })
            assert login.status_code == 200
            headers.setdefault("X-CSRF-Token", login.json()["csrf_token"])
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await client.request(method, path, headers=headers, **kwargs)


async def test_index_serves_html(app):
    resp = await request(app, "GET", "/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_healthz_no_auth(app):
    resp = await request(app, "GET", "/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_me_auth(app):
    assert (await request(app, "GET", "/api/me")).status_code == 401
    assert (await request(app, "GET", "/api/me", token="wrong")).status_code == 401
    for username in ("alice", "bob"):
        resp = await request(app, "GET", "/api/me", token=f"tok-{username}")
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == username
        assert "admin" not in resp.json()["user"]


async def test_list_instances_no_secrets(app):
    resp = await request(app, "GET", "/api/instances", token="tok-bob")
    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()["instances"]}
    assert names == {"pg-read", "pg-write", "redis-read", "redis-write", "pg-disabled"}
    text = resp.text
    # 密码绝不回显（配置里的明文密码与 ${ENV} 引用都不该出现）
    assert "pg-pw" not in text
    assert "rd-pw" not in text
    assert "TEST_PG_PASSWORD" not in text
    by_name = {i["name"]: i for i in resp.json()["instances"]}
    # 只暴露"是否设置了密码"
    assert by_name["pg-read"]["has_password"] is True
    assert by_name["pg-read"]["permissions"] == "read"


_NEW_PG = {
    "name": "pg-new",
    "type": "postgres",
    "host": "10.0.0.99",
    "port": 5432,
    "database": "newdb",
    "user": "u",
    "password": "new-secret",
    "permissions": "read",
}

_NEW_REDIS = {
    "name": "redis-new",
    "type": "redis",
    "host": "10.0.0.41",
    "port": 6379,
    "password": "rd-secret",
    "database": 2,
    "permissions": "write",
}


async def test_all_users_can_create(app):
    resp = await request(app, "POST", "/api/instances", token="tok-bob", json=_NEW_PG)
    assert resp.status_code == 201


async def test_create_instance(app):
    resp = await request(app, "POST", "/api/instances", token="tok-alice", json=_NEW_PG)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "pg-new"
    assert body["type"] == "postgres"
    assert body["has_password"] is True
    assert "new-secret" not in resp.text

    resp = await request(app, "GET", "/api/instances", token="tok-bob")
    assert "pg-new" in {i["name"] for i in resp.json()["instances"]}


async def test_create_redis_instance(app):
    resp = await request(app, "POST", "/api/instances", token="tok-alice", json=_NEW_REDIS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "redis"
    assert body["permissions"] == "write"
    assert body["username"] == ""


async def test_create_missing_name(app):
    resp = await request(app, "POST", "/api/instances", token="tok-alice", json=_NEW_PG | {"name": ""})
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"]


async def test_create_invalid_returns_400(app):
    bad = {**_NEW_PG, "name": "bad name!"}
    resp = await request(app, "POST", "/api/instances", token="tok-alice", json=bad)
    assert resp.status_code == 400


async def test_update_instance_keeps_password(app, gw):
    await request(app, "POST", "/api/instances", token="tok-alice", json=_NEW_PG)
    # 不传 password：应保留原密码
    resp = await request(
        app, "PUT", "/api/instances/pg-new", token="tok-alice",
        json={"name": "pg-new", "type": "postgres", "host": "10.0.0.100",
              "database": "newdb", "user": "u", "permissions": "read"},
    )
    assert resp.status_code == 200
    assert resp.json()["host"] == "10.0.0.100"
    # 落盘的是原密码字面量（该实例是新建的，密码就是 new-secret）
    assert gw.cfg_file.raw["instances"]["pg-new"]["password"] == "new-secret"


async def test_update_missing_404(app):
    resp = await request(app, "PUT", "/api/instances/nope", token="tok-alice", json={})
    assert resp.status_code == 404


async def test_delete_instance(app):
    resp = await request(app, "DELETE", "/api/instances/redis-read", token="tok-alice")
    assert resp.status_code == 200
    resp = await request(app, "DELETE", "/api/instances/redis-read", token="tok-alice")
    assert resp.status_code == 404


async def test_unknown_route_returns_404(app):
    """子路径约定：后端按根路径自居，未知 /api 路径应 404 而不是落到管理页。"""
    resp = await request(app, "GET", "/api/does-not-exist", token="tok-alice")
    assert resp.status_code == 404


async def test_config_hot_reload_visible_to_tools(app, gw):
    async with Client(gw.mcp) as client:
        before = await client.call_tool("list_instances", {})
        assert len(before.data.instances) == 5

        resp = await request(app, "POST", "/api/instances", token="tok-alice", json=_NEW_PG)
        assert resp.status_code == 201

        after = await client.call_tool("list_instances", {})
        assert "pg-new" in {i.name for i in after.data.instances}


async def test_all_users_can_test_connection(app):
    resp = await request(app, "POST", "/api/instances/test", token="tok-bob", json=_NEW_PG)
    assert resp.status_code == 200


async def test_test_connection_ok(app, gw):
    resp = await request(app, "POST", "/api/instances/test", token="tok-alice", json=_NEW_PG)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # 测试连接走的是一次性新连接，不进连接池
    assert gw.state.pool._pg == {} and gw.state.pool._redis == {}


async def test_test_connection_redis_ok(app):
    resp = await request(
        app, "POST", "/api/instances/test", token="tok-alice", json=_NEW_REDIS
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_test_connection_failure_reported(app, gw, harness):
    harness.pg_fail_connect = asyncpg.PostgresError("connection refused")
    resp = await request(app, "POST", "/api/instances/test", token="tok-alice", json=_NEW_PG)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "refused" in body["error"]


async def test_test_connection_missing_required_field(app):
    resp = await request(
        app, "POST", "/api/instances/test", token="tok-alice",
        json={"name": "x", "type": "postgres", "host": "h"},
    )
    assert resp.status_code == 400  # 缺 database/user


async def test_assets_serves_built_index(app):
    """构建产物入库后才能提供管理页；未构建时应 404 且不抛异常。"""
    resp = await request(app, "GET", "/assets/../config.yaml")
    assert resp.status_code == 404  # 目录穿越被拒


async def test_cookie_login_rejects_mcp_token_and_csrf_required(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/me", headers={"Authorization": "Bearer tok-alice"})
        assert response.status_code == 401
        login = await client.post("/api/auth/login", json={"username": "bob", "password": "password-bob"})
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        response = await client.post("/api/instances", json=_NEW_PG)
        assert response.status_code == 403
        response = await client.post("/api/instances", json=_NEW_PG, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 201


async def test_users_and_tokens_managed_separately(app, gw):
    people = await request(app, "GET", "/api/users", token="tok-bob")
    assert people.status_code == 200
    assert {u["username"] for u in people.json()["users"]} == {"alice", "bob"}
    assert "tok-alice" not in people.text
    created = await request(app, "POST", "/api/users", token="tok-bob", json={"username": "charlie", "password": "password-charlie"})
    assert created.status_code == 201
    token = await request(app, "POST", "/api/tokens", token="tok-bob", json={"name": "ci-agent", "description": "自动化"})
    assert token.status_code == 201
    secret = token.json()["token"]
    token_id = token.json()["item"]["id"]
    listing = await request(app, "GET", "/api/tokens", token="tok-bob")
    assert secret not in listing.text
    assert gw.state.identity.verify_token(secret)["id"] == token_id
    assert (await request(app, "GET", "/api/me", token=secret)).status_code == 401
    update = await request(app, "PATCH", f"/api/tokens/{token_id}", token="tok-alice", json={"enabled": False})
    assert update.status_code == 200
    assert gw.state.identity.verify_token(secret) is None
    await request(app, "PATCH", f"/api/tokens/{token_id}", token="tok-alice", json={"enabled": True})
    assert gw.state.identity.verify_token(secret)
    await request(app, "DELETE", f"/api/tokens/{token_id}", token="tok-bob")
    assert gw.state.identity.verify_token(secret) is None


async def test_session_revoked_when_password_changes(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        login = await client.post("/api/auth/login", json={"username": "bob", "password": "password-bob"})
        user_id = login.json()["user"]["id"]
        response = await client.patch(f"/api/users/{user_id}", json={"password": "new-password-bob"}, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 200
        assert (await client.get("/api/me")).status_code == 401
        assert (await client.post("/api/auth/login", json={"username": "bob", "password": "password-bob"})).status_code == 401
        assert (await client.post("/api/auth/login", json={"username": "bob", "password": "new-password-bob"})).status_code == 200


async def test_setup_requires_local_code_no_default_account(tmp_path, harness):
    from db_mcp.config import ConfigFile
    from db_mcp.server import create_mcp

    path = tmp_path / "empty.yaml"
    path.write_text("instances: {}\n")
    mcp, state = create_mcp(ConfigFile(path))
    app = mcp.http_app()
    try:
        assert (await request(app, "GET", "/api/auth/status")).json()["setup_required"]
        assert state.identity.verify_token("admin") is None
        bad = await request(app, "POST", "/api/auth/setup", json={"username": "owner", "password": "owner-password", "setup_key": "wrong"})
        assert bad.status_code == 400
        setup = await request(app, "POST", "/api/auth/setup", json={"username": "owner", "password": "owner-password", "setup_key": state.identity.setup_key_path.read_text().strip()})
        assert setup.status_code == 201
        assert not (await request(app, "GET", "/api/auth/status")).json()["setup_required"]
        assert "password" not in setup.json()["user"]
    finally:
        await state.shutdown()


async def test_test_connection_keeps_saved_password(app, harness):
    result = await request(app, "POST", "/api/instances/test", token="tok-bob", json={"name": "pg-read", "type": "postgres", "host": "10.0.0.30", "database": "appdb", "user": "reader", "password": ""})
    assert result.json()["ok"] is True
    assert harness.connect_calls[-1]["password"] == "pg-pw"


async def test_audit_and_config_exports_are_sanitized(app):
    response = await request(app, "GET", "/api/config-raw", token="tok-bob")
    assert response.status_code == 200
    for secret in ("pg-pw", "rd-pw", "pw-write", "TEST_ALICE_TOKEN", "TEST_PG_PASSWORD"):
        assert secret not in response.text
    response = await request(app, "GET", "/api/audit?kind=person", token="tok-bob")
    assert response.status_code == 200
    assert any(r["action"] == "config.export" for r in response.json()["records"])
    response = await request(app, "GET", "/api/audit/export?kind=person", token="tok-bob")
    assert response.status_code == 200
    assert "actor_user_id" in response.text


async def test_invalid_json_and_validation_do_not_expose_secrets(app):
    response = await request(app, "POST", "/api/instances/test", token="tok-bob", json={"type": "postgres", "host": "h", "port": "private-secret"})
    assert response.status_code == 400
    assert "private-secret" not in response.text
    response = await request(app, "POST", "/api/users", token="tok-alice", json=[])
    assert response.status_code == 400


async def test_existing_mcp_session_rechecks_revoked_token(app, gw):
    from fastmcp.client.transports import StreamableHttpTransport

    token = await request(app, "POST", "/api/tokens", token="tok-alice", json={"name": "mcp-session"})
    secret = token.json()["token"]
    token_id = token.json()["item"]["id"]
    def factory(**kwargs):
        return AsyncClient(transport=ASGITransport(app=app), **kwargs)
    transport = StreamableHttpTransport("http://t/mcp", headers={"Authorization": f"Bearer {secret}"}, httpx_client_factory=factory)
    async with app.router.lifespan_context(app), Client(transport) as client:
        assert len((await client.call_tool("list_instances", {})).data.instances) == 5
        await request(app, "DELETE", f"/api/tokens/{token_id}", token="tok-bob")
        with pytest.raises(MCPError, match="Server returned an error response"):
            await client.call_tool("list_instances", {})



async def test_test_connection_does_not_expand_saved_password_twice(app, gw, harness, monkeypatch):
    monkeypatch.setenv("TEST_PG_PASSWORD", "literal$$password-${NOT_AN_ENV_REFERENCE}")
    gw.state.replace_config(gw.cfg_file.load())
    password = gw.state.config.get_instance("pg-read").password.get_secret_value()
    response = await request(app, "POST", "/api/instances/test", token="tok-bob", json={
        "name": "pg-read", "type": "postgres", "host": "10.0.0.30",
        "database": "appdb", "user": "reader", "password": "",
    })
    assert response.json()["ok"] is True
    assert harness.connect_calls[-1]["password"] == password
    assert password not in response.text


async def test_concurrent_create_cannot_overwrite_instance(app, gw, monkeypatch):
    import asyncio

    original = gw.state.upsert_instance
    arrived = 0
    ready = asyncio.Event()
    async def synchronized(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(gw.state, "upsert_instance", synchronized)
    responses = await asyncio.gather(*(
        request(app, "POST", "/api/instances", token="tok-bob", json=_NEW_PG | {"host": host})
        for host in ("10.0.0.99", "10.0.0.100")
    ))
    assert sorted(response.status_code for response in responses) == [201, 400]
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert gw.state.config.get_instance("pg-new").host == winner["host"]


async def test_cancel_during_person_start_audit_does_not_create_instance(app, gw, monkeypatch):
    import asyncio

    original = gw.state.audit.begin_person
    entered = asyncio.Event()
    async def delayed_start(record):
        result = await original(record)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 模拟审计线程取消兜底完成后返回，路由仍必须传播请求取消。
            return result
    monkeypatch.setattr(gw.state.audit, "begin_person", delayed_start)
    task = asyncio.create_task(request(app, "POST", "/api/instances", token="tok-bob", json=_NEW_PG))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gw.state.config.get_instance("pg-new") is None
    records = gw.state.audit.query(kind="person")[0]
    assert any(r["action"] == "instance.create" and r["status"] == "cancelled" for r in records)


async def test_cancel_during_person_finish_audit_propagates_after_completed_change(app, gw, monkeypatch):
    import asyncio

    original = gw.state.audit.finish_person
    entered = asyncio.Event()
    async def delayed_finish(record):
        result = await original(record)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return result
    monkeypatch.setattr(gw.state.audit, "finish_person", delayed_finish)
    task = asyncio.create_task(request(app, "POST", "/api/instances", token="tok-bob", json=_NEW_PG))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gw.state.config.get_instance("pg-new") is not None
    records = gw.state.audit.query(kind="person")[0]
    assert any(r["action"] == "instance.create" and r["status"] == "ok" for r in records)
