"""人员 Cookie 会话管理 API；MCP Bearer 凭证只在协议端点使用。"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.responses import FileResponse, JSONResponse, Response

from ..audit import raise_if_cancelled, redact
from ..auth import IdentityError
from ..config import (
    ConfigError,
    InstanceConfig,
    PostgresInstance,
    expand_env_vars,
    sanitize_instance_entry,
)
from ..errors import DbMcpError
from ..state import DbState

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"
_STATIC_DIR = Path(__file__).parent / "static"
_TEST_TIMEOUT = 15
_COOKIE = "db_mcp_session"


def _instance_payload(name: str, inst: InstanceConfig) -> dict[str, Any]:
    """对外暴露的实例信息：绝不包含密码。"""
    payload: dict[str, Any] = {
        "name": name,
        "type": inst.type,
        "host": inst.host,
        "port": inst.port,
        "permissions": inst.permissions,
        "enabled": inst.enabled,
        "has_password": bool(inst.password.get_secret_value()),
    }
    if isinstance(inst, PostgresInstance):
        payload.update({"database": inst.database, "user": inst.user, "schema": inst.schema_})
    else:
        payload.update({"username": inst.username, "database": inst.database})
    return payload


def _audit_filters(qp: Any) -> dict[str, Any]:
    kind = qp.get("kind", "mcp")
    if kind not in {"mcp", "person"}:
        raise IdentityError("审计类型须为 mcp 或 person")
    filters = {key: qp.get(key) or None for key in ("instance", "status", "q", "token_id", "user_id")}
    filters["kind"] = kind
    if qp.get("since"):
        try:
            stamp = datetime.fromisoformat(qp["since"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            filters["since"] = stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except ValueError:
            raise IdentityError("since 须为包含时区的 ISO 时间") from None
    if qp.get("days"):
        try:
            days = min(3650, max(1, int(qp["days"])))
        except ValueError:
            raise IdentityError("days 须为整数") from None
        filters["since"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="milliseconds"
        )
    return filters


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def register_web_routes(mcp: FastMCP, state: DbState) -> None:
    def event(
        request,
        action,
        *,
        user=None,
        status="ok",
        error=None,
        object_type=None,
        object_id=None,
        summary=None,
    ):
        return {
            "event_id": str(uuid.uuid4()),
            "actor_user_id": user["id"] if user else None,
            "username": user["username"]
            if user
            else getattr(request.state, "attempted_username", None),
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "summary": summary,
            "status": status,
            "error": error,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "source_ip": request.client.host if request.client else None,
        }

    async def read_body(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise IdentityError("请求体必须是 JSON 对象") from None
        if not isinstance(value, dict):
            raise IdentityError("请求体必须是 JSON 对象")
        return value

    def route(path, methods, *, action=None, public=False, object_type=None):
        def decorate(handler):
            @wraps(handler)
            async def wrapped(request):
                request.state.request_id = str(uuid.uuid4())
                request.state.user = None
                request.state.identity_event = None
                request.state.outbox_recorded = False
                if public and request.method == "POST":
                    content_type = request.headers.get("content-type", "").split(";", 1)[0]
                    if content_type != "application/json":
                        return JSONResponse({"detail": "请使用 application/json"}, status_code=400)
                if not public:
                    session = await asyncio.to_thread(
                        state.identity.session, request.cookies.get(_COOKIE)
                    )
                    if session is None:
                        if action:
                            await state.audit.log_person(
                                event(
                                    request,
                                    action,
                                    status="denied",
                                    error="网页登录认证失败",
                                    object_type=object_type,
                                )
                            )
                        return JSONResponse({"detail": "请使用用户名和密码登录"}, status_code=401)
                    request.state.user, request.state.csrf_token = session
                    if request.method not in {"GET", "HEAD"}:
                        csrf = request.headers.get("x-csrf-token", "")
                        if not hmac.compare_digest(session[1], csrf):
                            if action:
                                await state.audit.log_person(
                                    event(
                                        request,
                                        action,
                                        user=session[0],
                                        status="denied",
                                        error="CSRF校验失败",
                                        object_type=object_type,
                                    )
                                )
                            return JSONResponse(
                                {"detail": "CSRF 校验失败，请刷新页面"}, status_code=403
                            )
                record = (
                    event(
                        request,
                        action,
                        user=request.state.user,
                        object_type=object_type,
                        object_id=next(iter(request.path_params.values()), None),
                    )
                    if action
                    else None
                )
                request.state.identity_event = record
                external_audit = bool(action and action.startswith(("instance.", "config.", "audit.")))
                if external_audit:
                    await state.audit.begin_person(record)
                try:
                    # 审计兜底可能完成落库；取消的请求不能继续触发外部副作用。
                    raise_if_cancelled()
                    response = await handler(request)
                except asyncio.CancelledError:
                    if external_audit:
                        record.update(status="cancelled", error="请求取消，外部操作结果未知")
                        await state.audit.finish_person(record)
                    raise
                except IdentityError as exc:
                    response = JSONResponse({"detail": str(exc)}, status_code=400)
                except ConfigError:
                    # Pydantic validation messages can contain submitted secrets.
                    response = JSONResponse(
                        {"detail": "配置校验失败，请检查字段、环境变量与凭据类型"}, status_code=400
                    )
                except (OSError, ValueError, TypeError, KeyError):
                    response = JSONResponse(
                        {"detail": "操作失败，请检查输入、文件权限与服务日志"}, status_code=400
                    )
                except Exception as exc:  # noqa: BLE001 - keep audit and sanitized failure response
                    logging.getLogger(__name__).error("管理操作失败: %s", type(exc).__name__)
                    response = JSONResponse(
                        {"detail": "服务暂时无法完成操作，请稍后重试"}, status_code=500
                    )
                if record:
                    if response.status_code >= 400:
                        record["status"] = "error"
                        record["error"] = "操作未完成（请求被拒绝或参数无效）"
                    if (
                        getattr(request.state, "attempted_username", None)
                        and record["username"] is None
                    ):
                        record["username"] = request.state.attempted_username
                    # Identity mutations have a transactional outbox. Others are written directly.
                    if external_audit:
                        await state.audit.finish_person(record)
                    elif not getattr(request.state, "outbox_recorded", False):
                        await state.audit.log_person(record)
                    await state.flush_identity_events()
                # 副作用已完成时保留其真实审计结果，但仍传播请求取消。
                raise_if_cancelled()
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Request-ID"] = request.state.request_id
                return response

            mcp.custom_route(path, methods=methods)(wrapped)
            return wrapped

        return decorate

    def set_session(response, value):
        auth = state.config.auth
        response.set_cookie(
            _COOKIE,
            value,
            max_age=auth.session_hours * 3600,
            httponly=True,
            secure=auth.cookie_secure,
            samesite="strict",
            path=auth.cookie_path,
        )

    @route("/", ["GET"], public=True)
    async def index(request):
        if not _INDEX_HTML.is_file():
            return JSONResponse({"detail": "前端产物缺失，请构建 webui"}, status_code=503)
        return FileResponse(_INDEX_HTML)

    @route("/assets/{path:path}", ["GET"], public=True)
    async def assets(request):
        file = (_STATIC_DIR / "assets" / request.path_params["path"]).resolve()
        if not file.is_relative_to((_STATIC_DIR / "assets").resolve()) or not file.is_file():
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(file)

    @route("/healthz", ["GET"], public=True)
    async def healthz(request):
        return JSONResponse({"ok": True})

    @route("/api/auth/status", ["GET"], public=True)
    async def auth_status(request):
        return JSONResponse(
            {"setup_required": await asyncio.to_thread(lambda: state.identity.setup_required)}
        )

    @route("/api/auth/setup", ["POST"], action="user.initialize", public=True, object_type="user")
    async def setup(request):
        value = await read_body(request)
        user = await asyncio.to_thread(
            state.identity.initialize,
            value.get("username", ""),
            value.get("password", ""),
            value.get("setup_key", ""),
            request.state.identity_event,
        )
        request.state.outbox_recorded = True
        request.state.user = user
        user, session, csrf = await asyncio.to_thread(
            state.identity.login, user["username"], value["password"]
        )
        response = JSONResponse({"user": user, "csrf_token": csrf}, status_code=201)
        set_session(response, session)
        return response

    @route("/api/auth/login", ["POST"], action="user.login", public=True, object_type="user")
    async def login(request):
        value = await read_body(request)
        username = value.get("username", "")
        username = username.strip() if isinstance(username, str) else username
        request.state.attempted_username = username[:128] if isinstance(username, str) else None
        if not isinstance(username, str) or not isinstance(value.get("password"), str):
            raise IdentityError("用户名或密码错误")
        try:
            user, session, csrf = await asyncio.to_thread(
                state.identity.login, username, value["password"], request.state.identity_event
            )
        except IdentityError:
            return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
        request.state.outbox_recorded = True
        response = JSONResponse({"user": user, "csrf_token": csrf})
        set_session(response, session)
        return response

    @route("/api/me", ["GET"])
    async def me(request):
        return JSONResponse({"user": request.state.user, "csrf_token": request.state.csrf_token})

    @route("/api/auth/logout", ["POST"], action="user.logout", object_type="session")
    async def logout(request):
        await asyncio.to_thread(
            state.identity.logout, request.cookies.get(_COOKIE, ""), request.state.identity_event
        )
        request.state.outbox_recorded = True
        response = JSONResponse({"ok": True})
        response.delete_cookie(_COOKIE, path=state.config.auth.cookie_path)
        return response

    @route("/api/users", ["GET"])
    async def list_users(request):
        return JSONResponse({"users": await asyncio.to_thread(state.identity.list_users)})

    @route("/api/users", ["POST"], action="user.create", object_type="user")
    async def create_user(request):
        value = await read_body(request)
        item = await asyncio.to_thread(
            state.identity.create_user,
            value.get("username", ""),
            value.get("password", ""),
            value.get("display_name", ""),
            request.state.identity_event,
        )
        request.state.outbox_recorded = True
        return JSONResponse(item, status_code=201)

    @route("/api/users/{id}", ["PATCH"], action="user.update", object_type="user")
    async def update_user(request):
        value = await read_body(request)
        changes = {k: v for k, v in value.items() if k in {"display_name", "password", "enabled"}}
        request.state.identity_event["summary"] = ", ".join(
            "密码已更换" if k == "password" else k for k in changes
        )
        item = await asyncio.to_thread(
            state.identity.update_user,
            request.path_params["id"],
            changes,
            request.state.identity_event,
        )
        request.state.outbox_recorded = True
        return JSONResponse(item)

    @route("/api/tokens", ["GET"])
    async def list_tokens(request):
        return JSONResponse({"tokens": await asyncio.to_thread(state.identity.list_tokens)})

    @route("/api/tokens", ["POST"], action="token.create", object_type="token")
    async def create_token(request):
        value = await read_body(request)
        token, item = await asyncio.to_thread(
            state.identity.create_token,
            value.get("name", ""),
            request.state.user["id"],
            value.get("description", ""),
            request.state.identity_event,
        )
        request.state.outbox_recorded = True
        return JSONResponse({"token": token, "item": item}, status_code=201)

    @route("/api/tokens/{id}", ["PATCH"], action="token.update", object_type="token")
    async def update_token(request):
        value = await read_body(request)
        changes = {k: v for k, v in value.items() if k in {"name", "description", "enabled"}}
        request.state.identity_event["summary"] = ", ".join(changes)
        item = await asyncio.to_thread(
            state.identity.update_token,
            request.path_params["id"],
            changes,
            request.state.identity_event,
        )
        request.state.outbox_recorded = True
        return JSONResponse(item)

    @route("/api/tokens/{id}", ["DELETE"], action="token.revoke", object_type="token")
    async def revoke_token(request):
        await asyncio.to_thread(
            state.identity.revoke_token, request.path_params["id"], request.state.identity_event
        )
        request.state.outbox_recorded = True
        return JSONResponse({"ok": True})


    @route("/api/instances", ["GET"])
    async def list_instances(request):
        return JSONResponse({"instances": [
            _instance_payload(name, inst) for name, inst in state.config.instances.items()
        ]})

    # 注册在动态名称路由之前；测试连接不修改连接池或持久化配置。
    @route("/api/instances/test", ["POST"], action="instance.test", object_type="instance")
    async def test_instance(request):
        value = await read_body(request)
        name = str(value.get("name") or "").strip()
        request.state.identity_event["object_id"] = name or None
        body = sanitize_instance_entry(value)
        existing = state.config.get_instance(name)
        inst = _validate_instance(body)
        if existing is not None and not body.get("password"):
            # 已存密码已完成环境变量解析，不可再展开其中的 $$/\${...}。
            inst = inst.model_copy(update={"password": existing.password})
        secrets = tuple(filter(None, (inst.password.get_secret_value(),)))
        conn = None
        try:
            conn = await asyncio.wait_for(state.pool.connect_one_shot(inst), timeout=_TEST_TIMEOUT)
            if isinstance(inst, PostgresInstance):
                async with conn.acquire() as c:
                    await asyncio.wait_for(c.fetchval("SELECT 1"), timeout=_TEST_TIMEOUT)
            else:
                await asyncio.wait_for(conn.ping(), timeout=_TEST_TIMEOUT)
        except asyncio.TimeoutError:
            request.state.identity_event.update(status="timeout", error="连接测试超时")
            return JSONResponse({"ok": False, "error": f"连接超时（{_TEST_TIMEOUT}s）"})
        except DbMcpError as exc:
            error = redact(str(exc), secrets)
            request.state.identity_event.update(status="error", error=error)
            return JSONResponse({"ok": False, "error": error})
        except Exception:  # noqa: BLE001 - 驱动异常统一脱敏并记录失败结果
            request.state.identity_event.update(status="error", error="连接失败，请检查地址与凭据")
            return JSONResponse({"ok": False, "error": "连接失败，请检查地址、凭据与网络"})
        finally:
            if conn is not None:
                await state.pool.close_one_shot(conn)
        return JSONResponse({"ok": True, "message": "连接成功"})

    @route("/api/instances", ["POST"], action="instance.create", object_type="instance")
    async def create_instance(request):
        body = await read_body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            raise IdentityError("缺少实例名 name")
        request.state.identity_event["object_id"] = name
        request.state.identity_event["summary"] = "创建数据库实例（凭据不记录）"
        config = await state.upsert_instance(name, body, create_only=True)
        return JSONResponse(_instance_payload(name, config.get_instance(name)), status_code=201)

    @route("/api/instances/{name}", ["PUT"], action="instance.update", object_type="instance")
    async def update_instance(request):
        name = request.path_params["name"]
        if state.config.get_instance(name) is None:
            return JSONResponse({"detail": "实例不存在"}, status_code=404)
        body = await read_body(request)
        request.state.identity_event["summary"] = "更新数据库实例" + (
            "，密码已更换" if body.get("password") else "，保留原凭据"
        )
        config = await state.upsert_instance(name, body)
        return JSONResponse(_instance_payload(name, config.get_instance(name)))

    @route("/api/instances/{name}", ["DELETE"], action="instance.delete", object_type="instance")
    async def delete_instance(request):
        name = request.path_params["name"]
        if state.config.get_instance(name) is None:
            return JSONResponse({"detail": "实例不存在"}, status_code=404)
        await state.delete_instance(name)
        return JSONResponse({"ok": True})

    @route("/api/config-raw", ["GET"], action="config.export", object_type="config")
    async def config_raw(request):
        # 只导出脱敏副本，旧 auth.users 中的 token 也不能重新暴露。
        from copy import deepcopy

        import yaml

        raw = deepcopy(state.cfg_file.raw)
        for instance in raw.get("instances", {}).values():
            if "password" in instance:
                instance["password"] = "[REDACTED]"
        raw.get("auth", {}).pop("users", None)
        return Response(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            media_type="application/x-yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="config-redacted.yaml"'},
        )

    @route("/api/audit", ["GET"])
    async def audit_query(request):
        qp = request.query_params
        try:
            limit = min(max(1, int(qp.get("limit", "20"))), 200)
            offset = max(0, int(qp.get("offset", "0")))
        except ValueError:
            raise IdentityError("分页参数必须是整数") from None
        records, total = await asyncio.to_thread(
            state.audit.query, limit=limit, offset=offset, **_audit_filters(qp)
        )
        return JSONResponse({"records": records, "total": total, "limit": limit, "offset": offset})

    @route("/api/audit/export", ["GET"], action="audit.export", object_type="audit")
    async def audit_export(request):
        filters = _audit_filters(request.query_params)
        rows = await asyncio.to_thread(state.audit.export_rows, **filters)
        fields = (
            [
                "event_id",
                "ts",
                "actor_user_id",
                "username",
                "action",
                "object_type",
                "object_id",
                "summary",
                "status",
                "error",
                "request_id",
                "source_ip",
            ]
            if filters["kind"] == "person"
            else [
                "id",
                "ts",
                "token_id",
                "token_name",
                "token_prefix",
                "tool",
                "kind",
                "user",
                "instance",
                "target_type",
                "target_host",
                "target_port",
                "target_username",
                "target_database",
                "statement",
                "statement_truncated",
                "row_count",
                "result_truncated",
                "status",
                "duration_ms",
                "error",
                "request_id",
                "source_ip",
            ]
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(fields)
        writer.writerows([_csv_cell(row.get(key)) for key in fields] for row in rows)
        return Response(
            "\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="audit-{filters["kind"]}.csv"'},
        )


def _validate_instance(body: dict[str, Any]) -> InstanceConfig:
    from pydantic import TypeAdapter

    adapter: TypeAdapter[InstanceConfig] = TypeAdapter(InstanceConfig)
    return adapter.validate_python(expand_env_vars(body))
