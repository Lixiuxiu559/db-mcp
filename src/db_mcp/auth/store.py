"""单机身份存储：凭证哈希验证、令牌加密副本与串行化数据库事务。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .expiry import expiration_epoch, is_expired, normalize_expiration


class IdentityError(ValueError):
    """可以向调用者展示的输入或状态错误。"""


class WorkspaceConflict(IdentityError):
    """工作空间仍被使用。"""


def _workspace_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
        raise IdentityError("工作空间名称须为 1–128 个字符")
    return name.strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _password_hash(password: str, *, minimum_length: int = 6) -> str:
    if not isinstance(password, str) or not minimum_length <= len(password) <= 1024:
        raise IdentityError(f"密码长度须为 {minimum_length}–1024 个字符")
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt${salt}${key.hex()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, salt, expected = stored.split("$")
        if algorithm != "scrypt" or not isinstance(password, str) or len(password) > 1024:
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(key.hex(), expected)
    except (ValueError, TypeError):
        return False


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _expiration(value: object, *, previous: object = None) -> str | None:
    try:
        normalized = normalize_expiration(value)
        epoch = expiration_epoch(normalized)
    except (ValueError, OverflowError, OSError):
        raise IdentityError("到期时间须为包含时区的 RFC3339 时间或 null") from None
    if normalized != previous and epoch is not None and epoch <= time.time():
        raise IdentityError("到期时间必须晚于当前时间")
    return normalized


class IdentityStore:
    def __init__(self, path: Path, session_hours: int = 8):
        self.path = Path(path)
        created_directory = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if created_directory:
            os.chmod(self.path.parent, 0o700)
        self.session_hours = session_hours
        self.setup_key_path = self.path.parent / "setup.key"
        self.token_key_path = self.path.with_suffix(".token.key")
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        with self._lock:
            self._connection()
            if self.setup_required and not self.setup_key_path.exists():
                try:
                    fd = os.open(self.setup_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass  # 并发启动的另一实例已创建初始化密钥。
                else:
                    with os.fdopen(fd, "w") as handle:
                        handle.write(secrets.token_urlsafe(32))

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # SQLite copies the main file's permissions when creating WAL/SHM.
            # Secure it before the first connection, including existing installations.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self._secure_sidecars()
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    prefix TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1, revoked INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT, created_at TEXT NOT NULL, last_used_at TEXT,
                    workspace_id TEXT NOT NULL, token_ciphertext TEXT, expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS identity_events (
                    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, payload TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0
                );
            """)
            # 多进程同时升级旧库时，串行检查与扩展，避免重复 ADD COLUMN。
            conn.execute("BEGIN IMMEDIATE")
            if "workspace_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(tokens)")}:
                # 仅扩展表结构，已存在的凭证保持无归属，不能通过验证。
                conn.execute("ALTER TABLE tokens ADD COLUMN workspace_id TEXT")
            if "token_ciphertext" not in {
                row["name"] for row in conn.execute("PRAGMA table_info(tokens)")
            }:
                # 旧凭证仍由原哈希验证，不推断或重新生成其明文。
                conn.execute("ALTER TABLE tokens ADD COLUMN token_ciphertext TEXT")
            if "expires_at" not in {
                row["name"] for row in conn.execute("PRAGMA table_info(tokens)")
            }:
                conn.execute("ALTER TABLE tokens ADD COLUMN expires_at TEXT")
            conn.commit()
            os.chmod(self.path, 0o600)
            self._secure_sidecars()
            self._conn = conn
        return self._conn

    def _secure_sidecars(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    @property
    def setup_required(self) -> bool:
        with self._lock:
            return self._connection().execute("SELECT 1 FROM users LIMIT 1").fetchone() is None

    def _user(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "last_login_at": row["last_login_at"],
            "created_at": row["created_at"],
        }

    def _token(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = (
            "id", "name", "description", "prefix", "created_by", "created_at", "last_used_at",
            "workspace_id", "expires_at",
        )
        item = {key: row[key] for key in keys}
        item.update(
            enabled=bool(row["enabled"]),
            revoked=bool(row["revoked"]),
            can_copy=bool(row["token_ciphertext"]) and not row["revoked"],
            expired=is_expired(row["expires_at"]),
        )
        return item

    def _token_cipher(self, conn: sqlite3.Connection, *, create: bool = False) -> Fernet:
        """只在首次签发时建密钥；调用方持有 BEGIN IMMEDIATE，跨进程串行。"""
        try:
            fd = os.open(self.token_key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if not create or conn.execute(
                "SELECT 1 FROM tokens WHERE token_ciphertext IS NOT NULL LIMIT 1"
            ).fetchone():
                raise IdentityError("Token 复制密钥缺失，请从备份恢复；现有凭证认证不受影响") from None
            key = Fernet.generate_key()
            try:
                fd = os.open(self.token_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self._token_cipher(conn, create=create)
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
        else:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise IdentityError("Token 复制密钥必须是普通文件，请检查备份")
                # 恢复的密钥可能保留宽松权限；只收紧已打开的文件，不替换其内容。
                os.fchmod(handle.fileno(), 0o600)
                key = handle.read()
        try:
            cipher = Fernet(key)
        except (ValueError, TypeError):
            raise IdentityError("Token 复制密钥损坏，请从备份恢复；现有凭证认证不受影响") from None
        if create:
            existing = conn.execute(
                "SELECT token_ciphertext FROM tokens WHERE token_ciphertext IS NOT NULL LIMIT 1"
            ).fetchone()
            if existing:
                try:
                    cipher.decrypt(existing["token_ciphertext"])
                except (InvalidToken, ValueError, TypeError):
                    raise IdentityError("Token 复制密钥与已有加密副本不匹配，请检查备份") from None
        return cipher

    def _event(
        self,
        conn: sqlite3.Connection,
        record: dict[str, Any] | None,
        *,
        actor: dict[str, Any] | None = None,
        object_id: str | None = None,
    ) -> None:
        if record is not None:
            record.setdefault("ts", _now())
            if actor:
                record["actor_user_id"] = actor["id"]
                record["username"] = actor["username"]
            if object_id:
                record["object_id"] = object_id
            conn.execute(
                "INSERT INTO identity_events(ts,payload) VALUES(?,?)",
                (_now(), json.dumps(record, ensure_ascii=False)),
            )

    def ensure_default_account(self) -> None:
        """仅为空身份库创建默认账号；重启不重置或补建已有用户。"""
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self.setup_required:
                    self.setup_key_path.unlink(missing_ok=True)
                    return
                user_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO users VALUES(?,?,?,?,1,?,NULL)",
                    (user_id, "admin", "admin", _password_hash("admin", minimum_length=5), _now()),
                )
                self._event(conn, {
                    "event_id": uuid.uuid4().hex,
                    "action": "user.initialize",
                    "object_type": "user",
                    "status": "ok",
                    "summary": "首次启动创建默认登录账号 admin",
                }, object_id=user_id)
            self.setup_key_path.unlink(missing_ok=True)

    def initialize(
        self, username: str, password: str, setup_key: str, event: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self.setup_required:
                    raise IdentityError("初始化已经完成")
                expected = self.setup_key_path.read_text()
                if not isinstance(setup_key, str) or not hmac.compare_digest(expected, setup_key):
                    raise IdentityError("初始化凭证无效，请从服务运行目录获取 setup.key")
                user = self._create_user(conn, username, password, username)
                self._event(conn, event, actor=user, object_id=user["id"])
            self.setup_key_path.unlink(missing_ok=True)
            return user

    def _create_user(self, conn, username, password, display_name):
        if not isinstance(username, str) or not username.strip() or len(username) > 128:
            raise IdentityError("用户名须为 1–128 个字符")
        if not isinstance(display_name, str) or len(display_name) > 128:
            raise IdentityError("显示名不能超过 128 个字符")
        user_id = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,1,?,NULL)",
                (
                    user_id,
                    username.strip(),
                    display_name or username.strip(),
                    _password_hash(password),
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            raise IdentityError("用户名已存在") from None
        return self._user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str = "",
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            with conn:
                user = self._create_user(conn, username, password, display_name)
                self._event(conn, event, object_id=user["id"])
            return user

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._user(r)
                for r in self._connection().execute("SELECT * FROM users ORDER BY created_at")
            ]

    def update_user(
        self, user_id: str, changes: dict[str, Any], event: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if row is None:
                    raise IdentityError("用户不存在")
                if "enabled" in changes and not isinstance(changes["enabled"], bool):
                    raise IdentityError("enabled 必须是布尔值")
                enabled = changes.get("enabled", bool(row["enabled"]))
                count = conn.execute("SELECT count(*) FROM users WHERE enabled=1").fetchone()[0]
                if row["enabled"] and not enabled and count <= 1:
                    raise IdentityError("不能停用最后一个可登录用户")
                display = changes.get("display_name", row["display_name"])
                if not isinstance(display, str) or len(display) > 128:
                    raise IdentityError("显示名不能超过 128 个字符")
                password_hash = (
                    _password_hash(changes["password"])
                    if "password" in changes
                    else row["password_hash"]
                )
                conn.execute(
                    "UPDATE users SET display_name=?,enabled=?,password_hash=? WHERE id=?",
                    (display, int(enabled), password_hash, user_id),
                )
                if not enabled or "password" in changes:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                self._event(conn, event)
            return self._user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def delete_user(self, user_id: str, event: dict[str, Any] | None = None) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if row is None:
                    raise IdentityError("用户不存在")
                count = conn.execute("SELECT count(*) FROM users WHERE enabled=1").fetchone()[0]
                if row["enabled"] and count <= 1:
                    raise IdentityError("不能删除最后一个可登录用户")
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))
                if event is not None:
                    event["summary"] = f"删除成员 {row['username']}；已撤销网页登录会话"
                self._event(conn, event, object_id=user_id)

    def login(
        self, username: str, password: str, event: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str, str]:
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            # Dummy hash still performs the expensive password derivation for unknown users.
            stored = row["password_hash"] if row else "scrypt$" + "00" * 16 + "$" + "00" * 64
            valid = _password_matches(password, stored)
            if row is None or not valid or not row["enabled"]:
                raise IdentityError("用户名或密码错误")
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with conn:
                conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
                conn.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?)",
                    (_digest(session), row["id"], csrf, time.time() + self.session_hours * 3600),
                )
                conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), row["id"]))
                self._event(conn, event, actor=self._user(row), object_id=row["id"])
            user = self._user(
                conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            )
            return user, session, csrf

    def session(self, value: str | None) -> tuple[dict[str, Any], str] | None:
        if not value:
            return None
        with self._lock:
            row = (
                self._connection()
                .execute(
                    """SELECT u.*, s.csrf_token FROM sessions s
                JOIN users u ON u.id=s.user_id WHERE s.session_hash=? AND s.expires_at>? AND u.enabled=1""",
                    (_digest(value), time.time()),
                )
                .fetchone()
            )
            return (self._user(row), row["csrf_token"]) if row else None

    def logout(self, value: str, event: dict[str, Any] | None = None) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("DELETE FROM sessions WHERE session_hash=?", (_digest(value),))
                self._event(conn, event)

    def list_workspaces(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(row) for row in self._connection().execute(
                "SELECT id,name FROM workspaces ORDER BY name,id"
            )]

    def save_workspace(self, name: str, *, workspace_id: str | None = None, event=None):
        name = _workspace_name(name)
        with self._lock:
            conn = self._connection()
            with conn:
                try:
                    if workspace_id is None:
                        workspace_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO workspaces VALUES(?,?)", (workspace_id, name))
                    elif not conn.execute(
                        "UPDATE workspaces SET name=? WHERE id=?", (name, workspace_id)
                    ).rowcount:
                        raise IdentityError("工作空间不存在")
                except sqlite3.IntegrityError:
                    raise IdentityError("工作空间名称已存在") from None
                if event is not None:
                    event["summary"] = name
                self._event(conn, event, object_id=workspace_id)
        return {"id": workspace_id, "name": name}

    def delete_workspace(self, workspace_id: str, event=None) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM tokens WHERE workspace_id=? AND revoked=0 LIMIT 1", (workspace_id,)
                ).fetchone():
                    raise WorkspaceConflict("请先删除工作空间中的 Token")
                row = conn.execute("SELECT name FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
                if row is None:
                    raise IdentityError("工作空间不存在")
                conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
                if event is not None:
                    event["summary"] = row["name"]
                self._event(conn, event, object_id=workspace_id)

    def list_tokens(self, *, include_deleted: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._token(r)
                for r in self._connection().execute(
                    "SELECT * FROM tokens WHERE (? OR revoked=0) ORDER BY created_at",
                    (include_deleted,),
                )
            ]

    def create_token(
        self, name: str, created_by: str, workspace_id: str, description: str = "",
        event: dict[str, Any] | None = None,
        *, expires_at: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise IdentityError("Token 名称须为 1–128 个字符")
        if not isinstance(description, str) or len(description) > 1000:
            raise IdentityError("备注不能超过 1000 个字符")
        token_id, value = str(uuid.uuid4()), "dbmcp_" + secrets.token_urlsafe(32)
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                expires_at = _expiration(expires_at)
                if not isinstance(workspace_id, str) or not conn.execute(
                    "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
                ).fetchone():
                    raise IdentityError("请选择有效的工作空间")
                ciphertext = self._token_cipher(conn, create=True).encrypt(
                    f"{token_id}:{value}".encode()
                ).decode("ascii")
                conn.execute(
                    """INSERT INTO tokens(id,name,description,prefix,token_hash,created_by,created_at,
                       workspace_id,token_ciphertext,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        token_id,
                        name.strip(),
                        description,
                        "dbmcp_…" + value[-4:],
                        _digest(value),
                        created_by,
                        _now(),
                        workspace_id,
                        ciphertext,
                        expires_at,
                    ),
                )
                if event is not None:
                    event["summary"] = f"{name.strip()}，工作空间 {workspace_id}"
                self._event(conn, event, object_id=token_id)
            return value, self.token_info(token_id)

    def reveal_token(self, token_id: str, event: dict[str, Any] | None = None) -> str:
        """仅供网页专用复制接口；解密结果绑定记录 ID 和原验证哈希。"""
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
                if row is None or row["revoked"]:
                    raise IdentityError("Token 不存在或已删除")
                if not row["token_ciphertext"]:
                    raise IdentityError("该 Token 未保存可复制副本，请新建 Token")
                cipher = self._token_cipher(conn)
                try:
                    bound_id, value = cipher.decrypt(row["token_ciphertext"]).decode().split(":", 1)
                    valid = hmac.compare_digest(bound_id, token_id) and hmac.compare_digest(
                        _digest(value), row["token_hash"]
                    )
                except (InvalidToken, ValueError, TypeError):
                    valid = False
                if not valid:
                    raise IdentityError("Token 加密副本损坏或密钥不匹配，请检查备份")
                self._event(conn, event, object_id=token_id)
            return value

    def token_info(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT * FROM tokens WHERE id=?", (token_id,))
                .fetchone()
            )
            return self._token(row) if row else None

    def verify_token(self, value: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                """SELECT t.*,w.name AS workspace_name FROM tokens t JOIN workspaces w ON w.id=t.workspace_id
                   WHERE token_hash=? AND enabled=1 AND revoked=0""",
                (_digest(value),),
            ).fetchone()
            if row is None or is_expired(row["expires_at"]):
                return None
            with conn:
                conn.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (_now(), row["id"]))
            return {**self._token(row), "workspace_name": row["workspace_name"]}

    def active_binding(self, token_id: str, workspace_id: str) -> bool:
        return self.active_token_info(token_id, workspace_id) is not None

    def active_token_info(self, token_id: str, workspace_id: str) -> dict[str, Any] | None:
        """返回不含秘密的当前绑定与期限，供异步调用者恢复后再次核对截止。"""
        with self._lock:
            row = self._connection().execute(
                """SELECT t.id,t.workspace_id,t.expires_at
                   FROM tokens t JOIN workspaces w ON w.id=t.workspace_id
                   WHERE t.id=? AND w.id=? AND t.enabled=1 AND t.revoked=0""",
                (token_id, workspace_id),
            ).fetchone()
            if row is None or is_expired(row["expires_at"]):
                return None
            return dict(row)

    def token_for_audit(self, value: str) -> dict[str, Any] | None:
        """识别被拒绝凭证的稳定身份，不启用凭证或更新最近调用时间。"""
        with self._lock:
            row = (
                self._connection()
                .execute("""SELECT t.*,w.name AS workspace_name FROM tokens t
                    LEFT JOIN workspaces w ON w.id=t.workspace_id WHERE token_hash=?""", (_digest(value),))
                .fetchone()
            )
            return {**self._token(row), "workspace_name": row["workspace_name"]} if row else None

    def update_token(
        self, token_id: str, changes: dict[str, Any], event: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
                if row is None:
                    raise IdentityError("Token 不存在")
                if "workspace_id" in changes:
                    raise IdentityError("Token 所属工作空间不可更改")
                if row["revoked"]:
                    raise IdentityError("已删除或吊销的 Token 不能重新启用或修改")
                name, description = (
                    changes.get("name", row["name"]),
                    changes.get("description", row["description"]),
                )
                enabled = changes.get("enabled", bool(row["enabled"]))
                if not isinstance(name, str) or not name.strip() or len(name) > 128:
                    raise IdentityError("Token 名称须为 1–128 个字符")
                if not isinstance(description, str) or len(description) > 1000:
                    raise IdentityError("备注不能超过 1000 个字符")
                if not isinstance(enabled, bool):
                    raise IdentityError("enabled 必须是布尔值")
                expires_at = (
                    _expiration(changes["expires_at"], previous=row["expires_at"])
                    if "expires_at" in changes else row["expires_at"]
                )
                conn.execute(
                    "UPDATE tokens SET name=?,description=?,enabled=?,expires_at=? WHERE id=?",
                    (name.strip(), description, int(enabled), expires_at, token_id),
                )
                self._event(conn, event)
            return self.token_info(token_id)

    def revoke_token(self, token_id: str, event: dict[str, Any] | None = None) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM tokens WHERE id=?", (token_id,)).fetchone() is None:
                    raise IdentityError("Token 不存在")
                conn.execute(
                    "UPDATE tokens SET enabled=0,revoked=1,token_ciphertext=NULL WHERE id=?",
                    (token_id,),
                )
                self._event(conn, event, object_id=token_id)

    def delete_token(self, token_id: str, event: dict[str, Any] | None = None) -> None:
        """保留已删除凭证的审计身份。"""
        self.revoke_token(token_id, event)

    def pending_events(self) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            return [
                (row["id"], json.loads(row["payload"]))
                for row in self._connection().execute(
                    "SELECT id,payload FROM identity_events WHERE delivered=0 ORDER BY id LIMIT 1000"
                )
            ]

    def acknowledge_event(self, event_id: int) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("DELETE FROM identity_events WHERE id=?", (event_id,))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
