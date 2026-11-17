"""账号核心边界、会话持久化、Cookie与失败归因的行为回归。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from threading import Barrier

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError

from db_mcp.auth import IdentityError, IdentityStore
from db_mcp.config import AuthConfig, ConfigFile
from db_mcp.server import create_mcp


@pytest.mark.parametrize("operation", ["create", "reset", "self"])
@pytest.mark.parametrize("length", [5, 6, 1024, 1025])
async def test_password_boundaries_via_http_preserve_failed_state(gw, operation, length):
    identity = gw.state.identity
    target = identity.create_user("target", "original-password")
    _, old_session, _ = identity.login("target", "original-password")
    app = gw.mcp.http_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as web:
        actor, password = ("target", "original-password") if operation == "self" else ("admin", "admin")
        login = await web.post("/api/auth/login", json={"username": actor, "password": password})
        assert login.status_code == 200
        web.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        await gw.state.flush_identity_events()
        before = list(identity._connection().execute("SELECT * FROM sessions"))
        before_users = identity.list_users()
        if operation == "create":
            response = await web.post("/api/users", json={"username": "created", "password": "a" * length})
        else:
            response = await web.patch(f"/api/users/{target['id']}", json={"password": "a" * length})
        if length in (5, 1025):
            assert response.status_code == 400
            assert identity.list_users() == before_users
            assert list(identity._connection().execute("SELECT * FROM sessions")) == before
            assert identity.session(old_session)
            assert identity.pending_events() == []
            rows = gw.state.audit.query(kind="person", q="user.create" if operation == "create" else "user.update")[0]
            assert len(rows) == 1 and rows[0]["status"] == "error"
        else:
            assert response.status_code == (201 if operation == "create" else 200)
            username = "created" if operation == "create" else "target"
            accepted = await web.post("/api/auth/login", json={"username": username, "password": "a" * length})
            assert accepted.status_code == 200
            if operation != "create":
                assert identity.session(old_session) is None
                assert (await web.post("/api/auth/login", json={
                    "username": "target", "password": "original-password",
                })).status_code == 401


@pytest.mark.parametrize("operation", ["password", "disable", "delete"])
def test_all_target_sessions_revoked_without_harming_other_person_or_tokens(tmp_path, operation):
    store = IdentityStore(tmp_path / "service.db")
    try:
        first = store.create_user("first", "first-password")
        second = store.create_user("second", "second-password")
        _, first_session, _ = store.login("first", "first-password")
        sessions = [store.login("second", "second-password")[1] for _ in range(2)]
        workspace = store.save_workspace("project")["id"]
        secret, item = store.create_token("machine", second["id"], workspace)
        if operation == "password":
            store.update_user(second["id"], {"password": "replacement-password"})
        elif operation == "disable":
            store.update_user(second["id"], {"enabled": False})
            store.update_user(second["id"], {"enabled": True})
        else:
            store.delete_user(second["id"])
            assert store.create_user("second", "replacement-password")["id"] != second["id"]
        assert all(store.session(session) is None for session in sessions)
        assert store.session(first_session)[0]["id"] == first["id"]
        assert store.verify_token(secret)["id"] == item["id"]
        assert store.token_info(item["id"])["created_by"] == second["id"]
    finally:
        store.close()


def test_simultaneous_disable_uses_independent_connections_and_keeps_one_user(tmp_path):
    store = IdentityStore(tmp_path / "service.db")
    users = [store.create_user(name, "password") for name in ("first", "second")]
    connections = [IdentityStore(store.path), IdentityStore(store.path)]
    barrier = Barrier(2)

    def disable(index):
        barrier.wait(timeout=5)
        try:
            connections[index].update_user(users[index]["id"], {"enabled": False})
            return True
        except IdentityError as error:
            assert "最后一个" in str(error)
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(disable, range(2))) == [False, True]
        assert sum(row["enabled"] for row in store.list_users()) == 1
    finally:
        for connection in connections:
            connection.close()
        store.close()


async def test_existing_web_session_survives_full_restart_and_expires_at_exact_boundary(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("instances: {}\n")
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    mcp, state = create_mcp(ConfigFile(path))
    async with AsyncClient(transport=ASGITransport(app=mcp.http_app()), base_url="http://t") as web:
        response = await web.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        user_id, csrf = response.json()["user"]["id"], response.json()["csrf_token"]
        cookie = web.cookies.get("db_mcp_session")
        expiry = state.identity._connection().execute("SELECT expires_at FROM sessions").fetchone()[0]
        assert expiry == now[0] + 8 * 3600
    await state.shutdown()
    restarted, state = create_mcp(ConfigFile(path))
    try:
        assert state.identity._connection().execute("SELECT expires_at FROM sessions").fetchone()[0] == expiry
        async with AsyncClient(transport=ASGITransport(app=restarted.http_app()), base_url="http://t",
                               cookies={"db_mcp_session": cookie}, headers={"X-CSRF-Token": csrf}) as web:
            now[0] = expiry - 0.001
            assert (await web.get("/api/me")).json()["user"]["id"] == user_id
            assert (await web.patch(f"/api/users/{user_id}", json={"display_name": "before"})).status_code == 200
            for cutoff in (expiry, expiry + 0.001):
                now[0] = cutoff
                assert (await web.get("/api/me")).status_code == 401
                assert (await web.patch(f"/api/users/{user_id}", json={"display_name": "forbidden"})).status_code == 401
            assert state.identity.list_users()[0]["display_name"] == "before"
    finally:
        await state.shutdown()


async def test_https_prefix_proxy_cookie_and_logout_contract(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("auth:\n  cookie_secure: true\n  cookie_path: /example/\n  session_hours: 1\ninstances: {}\n")
    mcp, state = create_mcp(ConfigFile(path))
    app = mcp.http_app()

    async def proxy(scope, receive, send):
        if scope["type"] == "http":
            assert scope["path"].startswith("/example/")
            path = scope["path"][len("/example"):]
            scope = {**scope, "path": path, "raw_path": path.encode()}
        await app(scope, receive, send)

    try:
        async with AsyncClient(transport=ASGITransport(app=proxy), base_url="https://t") as web:
            response = await web.post("/example/api/auth/login", json={"username": "admin", "password": "admin"})
            assert response.status_code == 200
            cookies = SimpleCookie(response.headers["set-cookie"])
            cookie = cookies["db_mcp_session"]
            assert cookie["httponly"] and cookie["secure"]
            assert cookie["samesite"] == "strict" and cookie["path"] == "/example/"
            assert cookie["max-age"] == "3600"
            session = web.cookies.get("db_mcp_session")
            assert (await web.get("/example/api/me")).status_code == 200
            response = await web.post("/example/api/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]})
            assert response.status_code == 200
            deleted = SimpleCookie(response.headers["set-cookie"])["db_mcp_session"]
            assert deleted["max-age"] == "0" and deleted["path"] == "/example/"
            assert deleted["httponly"] and deleted["secure"] and deleted["samesite"] == "strict"
            assert state.identity.session(session) is None
            assert (await web.get("/example/api/me")).status_code == 401
    finally:
        await state.shutdown()


@pytest.mark.parametrize("hours", [1, 168])
def test_auth_session_hours_accepts_endpoints(hours):
    assert AuthConfig(session_hours=hours).session_hours == hours


@pytest.mark.parametrize("hours", [0, 169])
def test_auth_session_hours_rejects_out_of_range(hours):
    with pytest.raises(ValidationError):
        AuthConfig(session_hours=hours)


@pytest.mark.parametrize("field,maximum", [("username", 128), ("display_name", 128),
                                          ("name", 128), ("description", 1000)])
def test_unicode_metadata_create_update_boundaries_and_types(tmp_path, field, maximum):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("project")["id"]
    target = store.create_user("existing", "password")
    _, token = store.create_token("existing", target["id"], workspace)
    counter = 0

    def create(value):
        nonlocal counter
        counter += 1
        if field in {"username", "display_name"}:
            return store.create_user(value if field == "username" else f"user-{counter}", "password",
                                     value if field == "display_name" else "display", event={"action": "user.create"})
        return store.create_token(value if field == "name" else f"token-{counter}", target["id"], workspace,
                                  value if field == "description" else "", event={"action": "token.create"})[1]

    def update(value):
        if field == "display_name":
            return store.update_user(target["id"], {field: value}, event={"action": "user.update"})
        return store.update_token(token["id"], {field: value}, event={"action": "token.update"})

    try:
        for count in (maximum - 1, maximum):
            value = "😀" * count
            created = create(value)
            assert created[field] == value
            if field == "username":
                assert store.login(value, "password")[0]["username"] == value
            else:
                assert update(value)[field] == value
        for invalid in ("😀" * (maximum + 1), None, 1, True, [], {}):
            before = (store.list_users(), store.list_tokens(), store.pending_events())
            with pytest.raises(IdentityError):
                create(invalid)
            if field != "username":
                with pytest.raises(IdentityError):
                    update(invalid)
            assert (store.list_users(), store.list_tokens(), store.pending_events()) == before
        if field in {"username", "name"}:
            before = (store.list_users(), store.list_tokens(), store.pending_events())
            for blank in ("", "   "):
                with pytest.raises(IdentityError):
                    create(blank)
                if field == "name":
                    with pytest.raises(IdentityError):
                        update(blank)
            assert (store.list_users(), store.list_tokens(), store.pending_events()) == before
            assert create(" trimmed ")[field] == "trimmed"
            if field == "username":
                with pytest.raises(IdentityError, match="已存在"):
                    create("trimmed")
        else:
            for blank in ("", "   "):
                assert update(blank)[field] == blank
    finally:
        store.close()


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_identity_enabled_only_accepts_booleans_without_side_effects(tmp_path, invalid):
    store = IdentityStore(tmp_path / "service.db")
    try:
        store.create_user("other", "password")
        user = store.create_user("target", "password")
        workspace = store.save_workspace("project")["id"]
        _, token = store.create_token("client", user["id"], workspace)
        before = (store.list_users(), store.list_tokens(), store.pending_events())
        with pytest.raises(IdentityError):
            store.update_user(user["id"], {"enabled": invalid}, event={"action": "user.update"})
        with pytest.raises(IdentityError):
            store.update_token(token["id"], {"enabled": invalid}, event={"action": "token.update"})
        assert (store.list_users(), store.list_tokens(), store.pending_events()) == before
        for enabled in (False, True):
            assert store.update_user(user["id"], {"enabled": enabled})["enabled"] is enabled
            assert store.update_token(token["id"], {"enabled": enabled})["enabled"] is enabled
    finally:
        store.close()


async def test_failed_login_attribution_keeps_sessions_and_recent_login_unchanged(gw):
    store = gw.state.identity
    user = store.create_user("enabled", "only-test-secret")
    disabled = store.create_user("disabled", "disabled-secret")
    store.login("enabled", "only-test-secret")
    store.update_user(disabled["id"], {"enabled": False})
    users = store.list_users()
    sessions = list(store._connection().execute("SELECT * FROM sessions"))
    await gw.state.flush_identity_events()
    async with AsyncClient(transport=ASGITransport(app=gw.mcp.http_app(), client=("192.0.2.25", 1234)), base_url="http://t") as web:
        for username, password in (("unknown", "unknown-secret"), ("enabled", "wrong-secret"), ("disabled", "disabled-secret")):
            response = await web.post("/api/auth/login", json={"username": username, "password": password})
            assert response.status_code == 401 and "set-cookie" not in response.headers
            assert store.list_users() == users
            assert list(store._connection().execute("SELECT * FROM sessions")) == sessions
            row = gw.state.audit.query(kind="person", q=username)[0][0]
            assert row["actor_user_id"] is None and row["username"] == username
            assert row["source_ip"] == "192.0.2.25" and row["request_id"] == response.headers["x-request-id"]
            assert row["status"] == "error"
            assert password not in response.text and password not in json.dumps(row)
        for body in ({"username": [], "password": "never-log-password"},
                     {"username": "enabled", "password": ["never-log-password"]}):
            response = await web.post("/api/auth/login", json=body)
            assert response.status_code == 400
            assert store.list_users() == users
            assert list(store._connection().execute("SELECT * FROM sessions")) == sessions
    assert user["id"] in {u["id"] for u in users}
    assert store.pending_events() == []
    assert "never-log-password" not in json.dumps(gw.state.audit.query(kind="person", limit=100)[0])


def test_successful_login_and_token_use_advance_only_their_recent_timestamps(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "service.db")
    try:
        user = store.create_user("person", "password")
        workspace = store.save_workspace("project")["id"]
        secret, token = store.create_token("client", user["id"], workspace)
        assert user["last_login_at"] is None and token["last_used_at"] is None
        for stamp in ("2030-01-01T00:00:01.000+00:00", "2030-01-01T00:00:02.000+00:00"):
            monkeypatch.setattr("db_mcp.auth.store._now", lambda stamp=stamp: stamp)
            assert store.login("person", "password")[0]["last_login_at"] == stamp
            assert store.list_users()[0]["last_login_at"] == stamp
            assert store.verify_token(secret)
            assert store.token_info(token["id"])["last_used_at"] == stamp
        monkeypatch.setattr("db_mcp.auth.store._now", lambda: "2030-01-01T00:00:03.000+00:00")
        assert store.reveal_token(token["id"]) == secret
        assert store.token_for_audit(secret)["last_used_at"] == stamp
        assert store.list_tokens()[0]["last_used_at"] == stamp
        store.update_token(token["id"], {"enabled": False})
        assert store.verify_token(secret) is None
        assert store.token_info(token["id"])["last_used_at"] == stamp
        with pytest.raises(IdentityError):
            store.login("person", "wrong-password")
        assert store.list_users()[0]["last_login_at"] == stamp
    finally:
        store.close()
