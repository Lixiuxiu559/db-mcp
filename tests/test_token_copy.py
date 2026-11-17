"""MCP Token 复制的持久化、秘密隔离与认证边界。"""

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from httpx2 import ASGITransport, AsyncClient

from db_mcp.auth import IdentityError, IdentityStore
from db_mcp.server import create_mcp


@pytest.fixture
def copy_app(gw):
    identity = gw.state.identity
    identity.create_user("alice", "password-alice")
    identity.create_user("bob", "password-bob")
    return gw.mcp.http_app()


async def login(client, username="alice"):
    response = await client.post("/api/auth/login", json={
        "username": username, "password": f"password-{username}",
    })
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


async def test_http_token_copy_survives_restart_and_disabled_state(copy_app, gw):
    async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as client:
        headers = await login(client)
        created = await client.post("/api/tokens", headers=headers, json={
            "name": "persistent-client", "workspace_id": gw.workspace_id,
        })
        assert created.status_code == 201
        secret, item = created.json()["token"], created.json()["item"]
        assert item["can_copy"] is True
        path = f"/api/tokens/{item['id']}"
        copied = await client.post(path + "/reveal", headers=headers)
        assert copied.status_code == 200 and copied.json() == {"token": secret}
        assert copied.headers["cache-control"] == "no-store"
        assert (await client.patch(path, headers=headers, json={"enabled": False})).status_code == 200
    key = gw.state.identity.token_key_path.read_bytes()
    await gw.state.shutdown()
    gw.state.identity.token_key_path.chmod(0o644)

    restarted, state = create_mcp(gw.cfg_file)
    try:
        assert state.identity.token_key_path.read_bytes() == key
        assert state.identity.verify_token(secret) is None
        async with AsyncClient(
            transport=ASGITransport(app=restarted.http_app()), base_url="http://t"
        ) as client:
            # 所有启用人员同权，其他人员也能复制停用的独立 token。
            headers = await login(client, "bob")
            copied = await client.post(path + "/reveal", headers=headers)
            assert copied.status_code == 200 and copied.json() == {"token": secret}
            assert stat.S_IMODE(state.identity.token_key_path.stat().st_mode) == 0o600
            assert state.identity.token_key_path.read_bytes() == key
            assert (await client.patch(path, headers=headers, json={"enabled": True})).status_code == 200
            assert state.identity.verify_token(secret)["id"] == item["id"]
            assert (await client.delete(path, headers=headers)).status_code == 200
            assert (await client.post(path + "/reveal", headers=headers)).status_code == 400
        assert state.identity.verify_token(secret) is None
        assert state.identity.token_info(item["id"])["can_copy"] is False
        row = state.identity._connection().execute(
            "SELECT token_hash,token_ciphertext FROM tokens WHERE id=?", (item["id"],)
        ).fetchone()
        assert row["token_ciphertext"] is None
        assert row["token_hash"] == hashlib.sha256(secret.encode()).hexdigest()
        assert state.identity.pending_events() == []
    finally:
        await state.shutdown()


async def test_copy_requires_cookie_and_csrf_and_never_enters_metadata_or_audit(copy_app, gw):
    secret, item = gw.secret, gw.token
    path = f"/api/tokens/{item['id']}/reveal"
    async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as client:
        assert (await client.post(path)).status_code == 401
        assert (await client.post(path, headers={"Authorization": f"Bearer {secret}"})).status_code == 401
        headers = await login(client)
        assert (await client.post(path)).status_code == 403
        assert (await client.post(path, headers={"X-CSRF-Token": "wrong"})).status_code == 403
        assert (await client.post(path, headers=headers)).json() == {"token": secret}
        assert (await client.post("/api/tokens/missing/reveal", headers=headers)).status_code == 400
        listed = await client.get("/api/tokens")
        assert listed.json()["tokens"][0]["can_copy"] is True
        history = await client.get("/api/audit?kind=person&q=token.copy")
        exported = await client.get("/api/audit/export?kind=person&q=token.copy", headers=headers)

    ciphertext = gw.state.identity._connection().execute(
        "SELECT token_ciphertext FROM tokens WHERE id=?", (item["id"],)
    ).fetchone()[0]
    for serialized in (listed.text, history.text, exported.text, json.dumps(gw.state.identity.pending_events())):
        assert secret not in serialized and ciphertext not in serialized
        assert "token_ciphertext" not in serialized and "token_hash" not in serialized
        assert gw.state.identity.token_key_path.read_text() not in serialized
    rows = history.json()["records"]
    assert len(rows) == 6
    assert all(row["action"] == "token.copy" and row["object_type"] == "token" for row in rows)
    assert all(row["object_id"] == item["id"] for row in rows if row["object_id"] != "missing")
    assert {row["status"] for row in rows} == {"ok", "denied", "error"}
    copied = next(row for row in rows if row["status"] == "ok")
    assert copied["username"] == "alice" and copied["actor_user_id"]
    assert copied["summary"] == "复制 MCP Token"
    denied = [row for row in rows if row["status"] == "denied"]
    assert len(denied) == 4
    assert all(row["object_id"] == item["id"] and row["request_id"] and row["source_ip"]
               for row in denied)
    assert len({row["request_id"] for row in denied}) == 4
    assert sum(row["actor_user_id"] is None for row in denied) == 2
    assert all(row["username"] == "alice" for row in denied if row["actor_user_id"])


def test_hash_only_token_is_not_migrated_or_recoverable(tmp_path):
    path = tmp_path / "service.db"
    value = "old-hash-only-secret"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE tokens (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, prefix TEXT, token_hash TEXT,
            enabled INTEGER, revoked INTEGER, created_by TEXT, created_at TEXT,
            last_used_at TEXT, workspace_id TEXT
        )""")
        conn.execute("INSERT INTO tokens VALUES('old','old','','old',?,1,0,NULL,'',NULL,'space')",
                     (hashlib.sha256(value.encode()).hexdigest(),))
    store = IdentityStore(path)
    try:
        with store._connection() as conn:
            conn.execute("INSERT INTO workspaces VALUES('space','旧项目')")
        assert store.verify_token(value)["id"] == "old"
        assert store.token_info("old")["can_copy"] is False
        assert store.list_tokens()[0]["can_copy"] is False
        assert not store.token_key_path.exists()
        with pytest.raises(IdentityError, match="未保存可复制副本"):
            store.reveal_token("old")
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["copy", "issue"])
def test_restored_key_permissions_are_repaired_without_changing_credentials(tmp_path, operation):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    secret, item = store.create_token("client", "creator", workspace)
    key_path = store.token_key_path
    original_key = key_path.read_bytes()
    ciphertext = store._connection().execute(
        "SELECT token_ciphertext FROM tokens WHERE id=?", (item["id"],)
    ).fetchone()[0]
    store.close()
    key_path.chmod(0o644)

    store = IdentityStore(store.path)
    try:
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o644
        if operation == "copy":
            assert store.reveal_token(item["id"]) == secret
        else:
            new_secret, new_item = store.create_token("new", "creator", workspace)
            assert store.verify_token(new_secret)["id"] == new_item["id"]
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert key_path.read_bytes() == original_key
        assert store._connection().execute(
            "SELECT token_ciphertext FROM tokens WHERE id=?", (item["id"],)
        ).fetchone()[0] == ciphertext
        assert store.reveal_token(item["id"]) == secret
        assert store.verify_token(secret)["id"] == item["id"]
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["copy", "issue"])
def test_key_permission_failure_does_not_return_secret_or_issue_token(tmp_path, monkeypatch, operation):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    secret, item = store.create_token("client", "creator", workspace)
    key_path = store.token_key_path
    original_key = key_path.read_bytes()
    key_path.chmod(0o644)
    key_inode = key_path.stat().st_ino
    original_fchmod = os.fchmod

    def deny_key_permissions(fd, mode):
        if os.fstat(fd).st_ino == key_inode:
            raise PermissionError("key permission change denied")
        return original_fchmod(fd, mode)

    try:
        with monkeypatch.context() as patched:
            patched.setattr(os, "fchmod", deny_key_permissions)
            with pytest.raises(PermissionError, match="key permission change denied"):
                if operation == "copy":
                    store.reveal_token(item["id"], {"action": "token.copy"})
                else:
                    store.create_token("blocked", "creator", workspace, event={"action": "token.create"})
        assert key_path.read_bytes() == original_key
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o644
        assert len(store.list_tokens()) == 1
        assert store.pending_events() == []
        assert store.verify_token(secret)["id"] == item["id"]
        assert store.reveal_token(item["id"]) == secret
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    finally:
        store.close()


@pytest.mark.parametrize("key_kind", ["symlink", "directory"])
def test_key_permission_repair_does_not_modify_other_files(tmp_path, key_kind):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    secret, item = store.create_token("client", "creator", workspace)
    key_path = store.token_key_path
    original_key = key_path.read_bytes()
    backup = tmp_path / "original.key"
    key_path.rename(backup)
    backup.chmod(0o644)
    if key_kind == "symlink":
        key_path.symlink_to(backup)
    else:
        key_path.mkdir(mode=0o755)

    try:
        with pytest.raises((OSError, IdentityError)):
            store.reveal_token(item["id"])
        assert backup.read_bytes() == original_key
        assert stat.S_IMODE(backup.stat().st_mode) == 0o644
        if key_kind == "directory":
            assert stat.S_IMODE(key_path.stat().st_mode) == 0o755
        else:
            assert key_path.is_symlink()
        assert store.verify_token(secret)["id"] == item["id"]
        assert store.pending_events() == []
    finally:
        store.close()


@pytest.mark.parametrize("replacement", [None, b"broken-key", Fernet.generate_key()])
def test_missing_or_damaged_key_never_replaces_ciphertext_or_breaks_auth(tmp_path, replacement):
    store = IdentityStore(tmp_path / "data" / "service.db")
    workspace = store.save_workspace("test")["id"]
    secret, item = store.create_token("client", "creator", workspace)
    original_key = store.token_key_path.read_bytes()
    ciphertext = store._connection().execute("SELECT token_ciphertext FROM tokens").fetchone()[0]
    path, key_path = store.path, store.token_key_path
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    store.close()
    if replacement is None:
        key_path.unlink()
    else:
        key_path.write_bytes(replacement)

    store = IdentityStore(path)
    try:
        assert store.verify_token(secret)["id"] == item["id"]
        assert store.token_info(item["id"])["can_copy"] is True
        with pytest.raises(IdentityError, match="密钥"):
            store.reveal_token(item["id"])
        with pytest.raises(IdentityError, match="密钥"):
            store.create_token("blocked", "creator", workspace)
        assert len(store.list_tokens()) == 1
        assert (key_path.read_bytes() if key_path.exists() else None) == replacement
        assert store._connection().execute(
            "SELECT token_ciphertext FROM tokens WHERE id=?", (item["id"],)
        ).fetchone()[0] == ciphertext
        key_path.write_bytes(original_key)
        assert store.reveal_token(item["id"]) == secret
    finally:
        store.close()


def test_ciphertext_is_bound_to_both_token_id_and_verification_hash(tmp_path):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    first_secret, first = store.create_token("first", "creator", workspace)
    second_secret, second = store.create_token("second", "creator", workspace)
    try:
        cipher = Fernet(store.token_key_path.read_bytes())
        for payload in (f"{second['id']}:{second_secret}", f"{first['id']}:{second_secret}"):
            with store._connection() as conn:
                conn.execute("UPDATE tokens SET token_ciphertext=? WHERE id=?",
                             (cipher.encrypt(payload.encode()).decode(), first["id"]))
            with pytest.raises(IdentityError, match="加密副本损坏"):
                store.reveal_token(first["id"])
            assert store.verify_token(first_secret)["id"] == first["id"]
    finally:
        store.close()


def test_concurrent_first_token_creation_shares_one_persistent_key(tmp_path):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    connections = [IdentityStore(store.path), IdentityStore(store.path)]
    ready = Barrier(2)

    def create(index):
        ready.wait(timeout=5)
        return connections[index].create_token(f"client-{index}", "creator", workspace)

    try:
        assert not store.token_key_path.exists()
        with ThreadPoolExecutor(max_workers=2) as workers:
            tokens = list(workers.map(create, range(2)))
        for secret, item in tokens:
            assert store.reveal_token(item["id"]) == secret
        assert stat.S_IMODE(store.token_key_path.stat().st_mode) == 0o600
    finally:
        for connection in connections:
            connection.close()
        store.close()


def test_copy_does_not_return_plaintext_if_transactional_audit_fails(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "service.db")
    workspace = store.save_workspace("test")["id"]
    secret, item = store.create_token("client", "creator", workspace)

    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError("outbox failed")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(store, "_event", fail_event)
            with pytest.raises(sqlite3.OperationalError, match="outbox"):
                store.reveal_token(item["id"], {"action": "token.copy"})
        assert store.pending_events() == []
        assert store.verify_token(secret)["id"] == item["id"]
    finally:
        store.close()


async def test_http_copy_outbox_insert_failure_rolls_back_and_retries_without_secret(
    copy_app, gw, monkeypatch, caplog
):
    store = gw.state.identity
    path = f"/api/tokens/{gw.token['id']}/reveal"
    original = store._event

    def fail_after_insert(conn, record, **kwargs):
        original(conn, record, **kwargs)
        raise sqlite3.OperationalError("copy commit failure")

    async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as web:
        headers = await login(web)
        await gw.state.flush_identity_events()
        with monkeypatch.context() as patched:
            patched.setattr(store, "_event", fail_after_insert)
            response = await web.post(path, headers=headers)
        assert response.status_code == 500
        assert gw.secret not in response.text and gw.secret not in caplog.text
        assert store.pending_events() == []
        rows = gw.state.audit.query(kind="person", q="token.copy")[0]
        assert len(rows) == 1 and rows[0]["status"] == "error"
        assert rows[0]["actor_user_id"] and rows[0]["object_id"] == gw.token["id"]
        assert store.verify_token(gw.secret)["id"] == gw.token["id"]
        response = await web.post(path, headers=headers)
        assert response.status_code == 200 and response.json() == {"token": gw.secret}
    rows = gw.state.audit.query(kind="person", q="token.copy")[0]
    assert sorted(row["status"] for row in rows) == ["error", "ok"]
    assert store.pending_events() == []


async def test_copy_delivery_failure_survives_restart_and_ack_retry_is_idempotent(
    copy_app, gw, monkeypatch
):
    store = gw.state.identity
    path = f"/api/tokens/{gw.token['id']}/reveal"
    async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as web:
        headers = await login(web)
        await gw.state.flush_identity_events()
        original = gw.state.audit.log_person

        async def fail_copy(record):
            return False if record.get("action") == "token.copy" else await original(record)

        with monkeypatch.context() as patched:
            patched.setattr(gw.state.audit, "log_person", fail_copy)
            response = await web.post(path, headers=headers)
            assert response.status_code == 200 and response.json() == {"token": gw.secret}
            pending = store.pending_events()
            assert len(pending) == 1 and pending[0][1]["action"] == "token.copy"
            event_id = pending[0][1]["event_id"]
            assert gw.state.audit.query(kind="person", q="token.copy")[1] == 0
            await gw.state.shutdown()

    _, state = create_mcp(gw.cfg_file)
    try:
        assert state.identity.pending_events()[0][1]["event_id"] == event_id

        def fail_ack(_):
            raise sqlite3.OperationalError("temporary acknowledgement failure")

        with monkeypatch.context() as patched:
            patched.setattr(state.identity, "acknowledge_event", fail_ack)
            await state.flush_identity_events()
        assert len(state.identity.pending_events()) == 1
        assert state.audit.query(kind="person", q="token.copy")[1] == 1
        await state.flush_identity_events()
        await state.flush_identity_events()
        assert state.identity.pending_events() == []
        rows, total = state.audit.query(kind="person", q="token.copy")
        assert total == 1 and rows[0]["event_id"] == event_id and rows[0]["status"] == "ok"
        assert rows[0]["username"] == "alice" and rows[0]["object_id"] == gw.token["id"]
    finally:
        await state.shutdown()


async def test_http_copy_decryption_runs_in_worker_thread(copy_app, gw, monkeypatch):
    import threading

    main_thread = threading.get_ident()
    threads = []
    original = gw.state.identity._token_cipher

    def capture_cipher(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as web:
        headers = await login(web)
        monkeypatch.setattr(gw.state.identity, "_token_cipher", capture_cipher)
        response = await web.post(f"/api/tokens/{gw.token['id']}/reveal", headers=headers)
        assert response.status_code == 200 and response.json() == {"token": gw.secret}
    assert threads and all(thread != main_thread for thread in threads)


@pytest.mark.parametrize("damage", ["missing-key", "bad-key", "mismatched-key", "bad-cipher"])
async def test_http_copy_key_and_cipher_failures_are_sanitized_and_audited(
    copy_app, gw, monkeypatch, caplog, damage
):
    store = gw.state.identity
    original_key = store.token_key_path.read_bytes()
    original_cipher = store._connection().execute(
        "SELECT token_ciphertext FROM tokens WHERE id=?", (gw.token["id"],)
    ).fetchone()[0]
    if damage == "missing-key":
        store.token_key_path.unlink()
    elif damage == "bad-key":
        store.token_key_path.write_bytes(b"only-test-corrupted-key-secret")
    elif damage == "mismatched-key":
        store.token_key_path.write_bytes(Fernet.generate_key())
    else:
        with store._connection() as conn:
            conn.execute("UPDATE tokens SET token_ciphertext=? WHERE id=?",
                         ("only-test-broken-cipher-secret", gw.token["id"]))
    try:
        async with AsyncClient(transport=ASGITransport(app=copy_app), base_url="http://t") as web:
            headers = await login(web)
            response = await web.post(f"/api/tokens/{gw.token['id']}/reveal", headers=headers)
            assert response.status_code == 400
            rows = gw.state.audit.query(kind="person", q="token.copy")[0]
            assert len(rows) == 1 and rows[0]["status"] == "error"
            assert rows[0]["object_id"] == gw.token["id"] and rows[0]["username"] == "alice"
            serialized = response.text + json.dumps(rows) + caplog.text + json.dumps(store.pending_events())
            for secret in (gw.secret, original_key.decode(), original_cipher,
                           "only-test-corrupted-key-secret", "only-test-broken-cipher-secret"):
                assert secret not in serialized
            assert store.verify_token(gw.secret)["id"] == gw.token["id"]
            assert store.pending_events() == []
    finally:
        store.token_key_path.write_bytes(original_key)
        with store._connection() as conn:
            conn.execute("UPDATE tokens SET token_ciphertext=? WHERE id=?", (original_cipher, gw.token["id"]))
