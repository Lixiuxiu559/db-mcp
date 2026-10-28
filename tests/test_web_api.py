"""Web 管理端 API 测试：鉴权、CRUD、热更新、测试连接、密码不回显。"""

from __future__ import annotations

import asyncpg
import pytest
from fastmcp import Client
from httpx2 import ASGITransport, AsyncClient


@pytest.fixture
def app(gw):
    return gw.mcp.http_app()


async def request(app, method: str, path: str, token: str | None = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
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
    resp = await request(app, "GET", "/api/me", token="tok-alice")
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice", "admin": True, "default_admin": False}
    resp = await request(app, "GET", "/api/me", token="tok-bob")
    assert resp.json() == {"username": "bob", "admin": False, "default_admin": False}


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


async def test_create_requires_admin(app):
    resp = await request(app, "POST", "/api/instances", token="tok-bob", json=_NEW_PG)
    assert resp.status_code == 403


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


async def test_test_connection_requires_admin(app):
    resp = await request(app, "POST", "/api/instances/test", token="tok-bob", json=_NEW_PG)
    assert resp.status_code == 403


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


async def test_users_requires_admin(app):
    assert (await request(app, "GET", "/api/users", token="tok-bob")).status_code == 403
    assert (
        await request(app, "PUT", "/api/users", token="tok-bob", json={"users": []})
    ).status_code == 403


async def test_users_list_returns_token(app):
    resp = await request(app, "GET", "/api/users", token="tok-alice")
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert {u["username"] for u in users} == {"alice", "bob"}
    by_name = {u["username"]: u for u in users}
    # 返回 token 配置原文（${ENV} 引用本身），不展开真实值
    assert by_name["alice"]["token"] == "${TEST_ALICE_TOKEN}"
    assert "tok-alice" not in resp.text


async def test_users_update_changes_token(app):
    payload = {
        "users": [
            {"username": "alice", "token": "${TEST_ALICE_TOKEN}", "admin": True},
            {"username": "bob", "token": "bob-new-secret", "admin": False},
        ]
    }
    resp = await request(app, "PUT", "/api/users", token="tok-alice", json=payload)
    assert resp.status_code == 200

    assert (await request(app, "GET", "/api/me", token="tok-alice")).status_code == 200
    assert (await request(app, "GET", "/api/me", token="tok-bob")).status_code == 401
    assert (await request(app, "GET", "/api/me", token="bob-new-secret")).status_code == 200


async def test_users_update_validates(app):
    # token 为空
    resp = await request(
        app, "PUT", "/api/users", token="tok-alice",
        json={"users": [{"username": "alice", "token": "", "admin": True}]},
    )
    assert resp.status_code == 400
    assert "token 不能为空" in resp.json()["detail"]

    # 用户名重复
    resp = await request(
        app, "PUT", "/api/users", token="tok-alice",
        json={"users": [{"username": "a", "token": "t1"}, {"username": "a", "token": "t2"}]},
    )
    assert resp.status_code == 400
    assert "用户名重复: a" in resp.json()["detail"]

    # 空列表
    resp = await request(app, "PUT", "/api/users", token="tok-alice", json={"users": []})
    assert resp.status_code == 400
    assert "至少" in resp.json()["detail"]


async def test_config_raw_requires_admin(app):
    assert (await request(app, "GET", "/api/config-raw", token="tok-bob")).status_code == 403
    resp = await request(app, "GET", "/api/config-raw", token="tok-alice")
    assert resp.status_code == 200
    # 导出的是配置原文：${ENV} 引用保留，真实密码不出现
    assert "${TEST_PG_PASSWORD}" in resp.text
    assert "pg-pw" not in resp.text


async def test_default_admin_bootstrap(tmp_path, harness):
    """未配置任何成员时内置 admin/admin；配置正式成员后默认账号自动失效。"""
    from db_mcp.config import ConfigFile
    from db_mcp.server import create_mcp

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n  host: 127.0.0.1\n  port: 8807\ninstances: {}\n", encoding="utf-8"
    )
    mcp, _ = create_mcp(ConfigFile(config_path))
    app = mcp.http_app()

    resp = await request(app, "GET", "/api/me", token="admin")
    assert resp.status_code == 200
    assert resp.json() == {"username": "admin", "admin": True, "default_admin": True}

    resp = await request(app, "GET", "/api/users", token="admin")
    assert resp.json()["users"] == [{"username": "admin", "admin": True, "token": "admin"}]

    resp = await request(
        app, "PUT", "/api/users", token="admin",
        json={"users": [{"username": "ops", "token": "ops-tok", "admin": True}]},
    )
    assert resp.status_code == 200
    assert (await request(app, "GET", "/api/me", token="admin")).status_code == 401
    resp = await request(app, "GET", "/api/me", token="ops-tok")
    assert resp.json() == {"username": "ops", "admin": True, "default_admin": False}


async def test_assets_serves_built_index(app):
    """构建产物入库后才能提供管理页；未构建时应 404 且不抛异常。"""
    resp = await request(app, "GET", "/assets/../config.yaml")
    assert resp.status_code == 404  # 目录穿越被拒
