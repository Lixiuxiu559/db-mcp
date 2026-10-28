"""网页管理端 API：实例配置 CRUD、连接测试、审计查询、成员 token 管理。

- 鉴权与 MCP 共用同一套用户 token（Authorization: Bearer <token>）
- 管理页面仅 admin 可进入；写操作与成员管理要求 admin 标志
- **密码只写不读**：instances CRUD 与成员接口永不返回密码明文，
  保存时留空 = 保留原值（含 ${ENV} 引用）
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

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


def _audit_filters(query_params: Any) -> dict[str, Any]:
    """从查询串提取审计筛选条件：instance / status / q（关键词）/ days（时间范围）。"""
    filters: dict[str, Any] = {
        "instance": query_params.get("instance") or None,
        "status": query_params.get("status") or None,
        "q": (query_params.get("q") or "").strip() or None,
    }
    days = query_params.get("days")
    if days:
        try:
            n = max(1, int(days))
            filters["since"] = (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(
                timespec="milliseconds"
            )
        except ValueError:
            pass
    return filters


def _csv_cell(value: Any) -> Any:
    """防 CSV 公式注入：以 = + - @ 开头的单元格加前缀，避免在 Excel 中被当公式执行。"""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def register_web_routes(mcp: FastMCP, state: DbState) -> None:
    def authenticate(request: Request) -> tuple[Any, JSONResponse | None]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None, JSONResponse({"detail": "未提供 token"}, status_code=401)
        token = header[7:].strip()
        for user in state.config.auth.users:
            if hmac.compare_digest(user.token.get_secret_value(), token):
                return user, None
        return None, JSONResponse({"detail": "token 无效"}, status_code=401)

    def require_admin(request: Request) -> tuple[Any, JSONResponse | None]:
        user, error = authenticate(request)
        if error is not None:
            return None, error
        if not user.admin:
            return None, JSONResponse({"detail": "需要 admin 权限"}, status_code=403)
        return user, None

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> FileResponse | JSONResponse:
        if not _INDEX_HTML.is_file():
            # 前端产物未入库（未跑 npm run build）：给出可读提示而不是裸 500
            return JSONResponse(
                {"detail": "前端产物缺失，请在 webui/ 下执行 npm run build"}, status_code=503
            )
        return FileResponse(_INDEX_HTML)

    @mcp.custom_route("/assets/{path:path}", methods=["GET"])
    async def assets(request: Request) -> FileResponse | JSONResponse:
        rel = request.path_params["path"]
        file = (_STATIC_DIR / "assets" / rel).resolve()
        if not file.is_relative_to(_STATIC_DIR.resolve()) or not file.is_file():
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(file)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/me", methods=["GET"])
    async def me(request: Request) -> JSONResponse:
        user, error = authenticate(request)
        if error is not None:
            return error
        return JSONResponse(
            {
                "username": user.username,
                "admin": user.admin,
                "default_admin": state.config.auth.using_default_admin,
            }
        )

    @mcp.custom_route("/api/instances", methods=["GET"])
    async def list_instances(request: Request) -> JSONResponse:
        _, error = authenticate(request)
        if error is not None:
            return error
        return JSONResponse(
            {
                "instances": [
                    _instance_payload(name, inst)
                    for name, inst in state.config.instances.items()
                ]
            }
        )

    # 注意：先注册 /api/instances/test，避免被 {name} 路由吞掉
    @mcp.custom_route("/api/instances/test", methods=["POST"])
    async def test_instance(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        try:
            body = sanitize_instance_entry(await request.json())
            if "password" not in body:
                # 编辑模式留空密码：测试时沿用该实例已保存的密码
                existing = state.config.get_instance(str(body.get("name") or ""))
                if existing is not None:
                    body["password"] = existing.password.get_secret_value()
            inst = _validate_instance(body)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - 测试连接：把任何异常转成可读结果
            return JSONResponse({"ok": False, "error": f"配置无效: {exc}"}, status_code=400)

        conn = None
        try:
            conn = await asyncio.wait_for(
                state.pool.connect_one_shot(inst), timeout=_TEST_TIMEOUT
            )
            if isinstance(inst, PostgresInstance):
                async with conn.acquire() as c:
                    await c.fetchval("SELECT 1")
            else:
                await conn.ping()
            return JSONResponse({"ok": True, "message": "连接成功"})
        except DbMcpError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "error": f"连接超时（{_TEST_TIMEOUT}s）"})
        except Exception as exc:  # noqa: BLE001 - 测试连接：把任何异常转成可读结果
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if conn is not None:
                await state.pool.close_one_shot(conn)

    @mcp.custom_route("/api/instances", methods=["POST"])
    async def create_instance(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 请求体非 JSON
            return JSONResponse({"detail": "请求体不是合法 JSON"}, status_code=400)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"detail": "缺少实例名 name"}, status_code=400)
        try:
            config = await state.upsert_instance(name, body)
        except ConfigError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except OSError as exc:
            # 权限不足 / 磁盘满 / 挂载不可写等：给出可读原因而不是 500
            return JSONResponse({"detail": f"配置文件写入失败: {exc}"}, status_code=500)
        inst = config.get_instance(name)
        if inst is None:  # pragma: no cover - upsert 成功后必然存在
            return JSONResponse({"detail": "保存后未找到实例"}, status_code=500)
        return JSONResponse(_instance_payload(name, inst), status_code=201)

    @mcp.custom_route("/api/instances/{name}", methods=["PUT"])
    async def update_instance(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        name = request.path_params["name"]
        if state.config.get_instance(name) is None:
            return JSONResponse({"detail": f"实例 {name!r} 不存在"}, status_code=404)
        try:
            body = await request.json()
            config = await state.upsert_instance(name, body)
        except ConfigError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"detail": f"配置文件写入失败: {exc}"}, status_code=500)
        inst = config.get_instance(name)
        if inst is None:  # pragma: no cover
            return JSONResponse({"detail": "保存后未找到实例"}, status_code=500)
        return JSONResponse(_instance_payload(name, inst))

    @mcp.custom_route("/api/instances/{name}", methods=["DELETE"])
    async def delete_instance(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        name = request.path_params["name"]
        if state.config.get_instance(name) is None:
            return JSONResponse({"detail": f"实例 {name!r} 不存在"}, status_code=404)
        try:
            await state.delete_instance(name)
        except OSError as exc:
            return JSONResponse({"detail": f"配置文件写入失败: {exc}"}, status_code=500)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/audit", methods=["GET"])
    async def audit_query(request: Request) -> JSONResponse:
        _, error = authenticate(request)
        if error is not None:
            return error
        qp = request.query_params
        try:
            limit = min(max(1, int(qp.get("limit", "20"))), 200)
        except ValueError:
            limit = 20
        try:
            offset = max(0, int(qp.get("offset", "0")))
        except ValueError:
            offset = 0
        filters = _audit_filters(qp)
        records, total = state.audit.query(limit=limit, offset=offset, **filters)
        return JSONResponse(
            {"records": records, "total": total, "limit": limit, "offset": offset}
        )

    @mcp.custom_route("/api/audit/export", methods=["GET"])
    async def audit_export(request: Request) -> Response:
        _, error = authenticate(request)
        if error is not None:
            return error
        rows = state.audit.export_rows(**_audit_filters(request.query_params))
        columns = [
            "ts", "user", "tool", "instance", "kind", "status", "duration_ms",
            "statement", "statement_truncated", "row_count", "error", "request_id",
        ]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([_csv_cell(r.get(k)) for k in columns])
        # BOM 让 Excel 正确识别 UTF-8 中文
        body = "﻿" + buf.getvalue()
        filename = f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @mcp.custom_route("/api/users", methods=["GET"])
    async def list_users(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        # 返回 token 配置原文：${ENV} 引用显示引用本身，不展开真实值
        raw_users = state.cfg_file.raw.get("auth", {}).get("users", [])
        if raw_users:
            users = [
                {
                    "username": str(u.get("username") or ""),
                    "admin": bool(u.get("admin")),
                    "token": str(u.get("token") or ""),
                }
                for u in raw_users
            ]
        else:
            # 配置里还没写 auth（使用内置默认账号）：展示当前生效的成员，便于直接改名/换 token
            users = [
                {
                    "username": u.username,
                    "admin": u.admin,
                    "token": u.token.get_secret_value(),
                }
                for u in state.config.auth.users
            ]
        return JSONResponse({"users": users})

    @mcp.custom_route("/api/users", methods=["PUT"])
    async def update_users(request: Request) -> JSONResponse:
        _, error = require_admin(request)
        if error is not None:
            return error
        body = await request.json()
        users = body.get("users")
        if not isinstance(users, list):
            return JSONResponse({"detail": "users 应为列表"}, status_code=400)
        try:
            config = await asyncio.to_thread(state.cfg_file.update_users, users)
        except ConfigError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"detail": f"配置文件写入失败: {exc}"}, status_code=500)
        except Exception as exc:  # noqa: BLE001 - 用户管理：任何异常转成可读错误
            return JSONResponse({"detail": f"保存失败: {exc}"}, status_code=400)
        state.replace_config(config)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/config-raw", methods=["GET"])
    async def config_raw(request: Request) -> Response:
        """导出配置文件原文（凭据为 ${ENV} 引用原文，不含真实密码）。仅 admin。"""
        _, error = require_admin(request)
        if error is not None:
            return error
        import yaml

        text = yaml.safe_dump(state.cfg_file.raw, allow_unicode=True, sort_keys=False)
        return Response(
            text,
            media_type="application/x-yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="config.yaml"'},
        )


def _validate_instance(body: dict[str, Any]) -> InstanceConfig:
    """把网页提交的表单校验成实例配置对象（用于测试连接）。"""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[InstanceConfig] = TypeAdapter(InstanceConfig)
    return adapter.validate_python(expand_env_vars(body))
