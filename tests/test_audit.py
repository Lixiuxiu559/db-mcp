"""审计测试：SQL 明文落库、截断、失败也记录、不泄露密码、分页与保留期。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastmcp import Client
from httpx2 import ASGITransport, AsyncClient


def read_records(audit_path):
    conn = sqlite3.connect(audit_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_records ORDER BY id").fetchall()]
    finally:
        conn.close()


async def _web_get(app, path: str, token: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})


async def test_audit_records_sql_plaintext(gw, tmp_path):
    """本服务与 ssh-mcp 的差异：SQL 明文入库（排查数据库问题必须知道执行了什么）。"""
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT * FROM users WHERE id = 7"}
        )

    records = read_records(tmp_path / "audit" / "audit.db")
    assert len(records) == 1
    rec = records[0]
    assert rec["tool"] == "postgres_query"
    assert rec["instance"] == "pg-read"
    assert rec["kind"] == "sql"
    assert rec["statement"] == "SELECT * FROM users WHERE id = 7"
    assert rec["statement_truncated"] == 0
    assert rec["status"] == "ok"
    assert rec["user"] == "anonymous"  # in-memory 无 HTTP 认证
    assert rec["duration_ms"] >= 0


async def test_audit_records_redis_command(gw, tmp_path):
    async with Client(gw.mcp) as client:
        await client.call_tool("redis_get", {"instance": "redis-read", "key": "user:1"})

    rec = read_records(tmp_path / "audit" / "audit.db")[0]
    assert rec["kind"] == "redis"
    assert rec["statement"] == "GET user:1"
    assert rec["instance"] == "redis-read"


async def test_audit_records_failures(gw, tmp_path):
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "nope", "sql": "SELECT 1"}, raise_on_error=False
        )

    rec = read_records(tmp_path / "audit" / "audit.db")[0]
    assert rec["status"] == "error"
    assert "InstanceNotFoundError" in rec["error"]
    # 失败时语句仍然记录，便于定位"谁尝试执行了什么"
    assert rec["statement"] == "SELECT 1"


async def test_audit_truncates_long_statement(gw, tmp_path):
    """max_statement_chars=200：超长语句被截断并打标。"""
    long_sql = "SELECT '" + "x" * 500 + "'"
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": long_sql}
        )

    rec = read_records(tmp_path / "audit" / "audit.db")[0]
    assert rec["statement_truncated"] == 1
    assert len(rec["statement"]) == 200


async def test_audit_never_stores_password(gw, tmp_path):
    """密码绝不能出现在审计库里（语句里出现密码字面量则按原样记录，但配置密码不会）。"""
    async with Client(gw.mcp) as client:
        await client.call_tool("list_instances", {})
        await client.call_tool("db_ping", {"instance": "pg-read"})

    db_bytes = (tmp_path / "audit" / "audit.db").read_bytes()
    assert b"pg-pw" not in db_bytes
    assert b"rd-pw" not in db_bytes


async def test_audit_visible_via_web(gw, tmp_path):
    app = gw.mcp.http_app()
    async with Client(gw.mcp) as client:
        await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 42"})

    resp = await _web_get(app, "/api/audit?limit=10", "tok-alice")
    body = resp.json()
    assert body["total"] == 1
    assert body["records"][0]["statement"] == "SELECT 42"


async def test_audit_pagination(gw):
    app = gw.mcp.http_app()
    async with Client(gw.mcp) as client:
        for i in range(7):
            await client.call_tool(
                "postgres_query", {"instance": "pg-read", "sql": f"SELECT {i}"}
            )

    page1 = (await _web_get(app, "/api/audit?limit=3&offset=0", "tok-alice")).json()
    page2 = (await _web_get(app, "/api/audit?limit=3&offset=3", "tok-alice")).json()
    page3 = (await _web_get(app, "/api/audit?limit=3&offset=6", "tok-alice")).json()

    assert page1["total"] == page2["total"] == page3["total"] == 7
    # 最新在前
    assert [r["statement"] for r in page1["records"]] == ["SELECT 6", "SELECT 5", "SELECT 4"]
    assert [r["statement"] for r in page2["records"]] == ["SELECT 3", "SELECT 2", "SELECT 1"]
    assert [r["statement"] for r in page3["records"]] == ["SELECT 0"]


async def test_audit_filter_by_instance_and_status(gw):
    app = gw.mcp.http_app()
    async with Client(gw.mcp) as client:
        await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"})
        await client.call_tool(
            "postgres_query", {"instance": "nope", "sql": "SELECT 2"}, raise_on_error=False
        )

    body = (await _web_get(app, "/api/audit?instance=pg-read", "tok-alice")).json()
    assert body["total"] == 1
    assert body["records"][0]["statement"] == "SELECT 1"

    body = (await _web_get(app, "/api/audit?status=error", "tok-alice")).json()
    assert body["total"] == 1
    assert body["records"][0]["status"] == "error"


async def test_audit_keyword_filter(gw):
    app = gw.mcp.http_app()
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT * FROM find_me"}
        )
        await client.call_tool("postgres_query", {"instance": "pg-read", "sql": "SELECT 1"})

    body = (await _web_get(app, "/api/audit?q=find_me", "tok-alice")).json()
    assert body["total"] == 1
    assert "find_me" in body["records"][0]["statement"]

    # 关键词也命中工具名
    assert (await _web_get(app, "/api/audit?q=postgres_query", "tok-alice")).json()["total"] == 2


async def test_audit_csv_export(gw):
    app = gw.mcp.http_app()
    async with Client(gw.mcp) as client:
        await client.call_tool(
            "postgres_query", {"instance": "pg-read", "sql": "SELECT '=SUM(A1:A9)'"}
        )

    resp = await _web_get(app, "/api/audit/export", "tok-alice")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    header = resp.text.splitlines()[0]
    assert "ts,user,tool,instance,kind,status" in header
    # 公式注入防护：以 = 开头的内容被加了前缀
    assert "'=SUM(A1:A9)" in resp.text


async def test_audit_retention_prunes_old_records(gw):
    """超过保留期的记录在写入时被清理。"""
    audit = gw.state.audit
    old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="milliseconds")
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="milliseconds")

    await audit.log({"ts": old_ts, "user": "u", "tool": "postgres_query", "status": "ok"})
    audit._prune(force=True)
    await audit.log({"ts": recent_ts, "user": "u", "tool": "postgres_query", "status": "ok"})
    audit._prune(force=True)

    records, total = audit.query(limit=50)
    assert total == 1
    assert records[0]["ts"] == recent_ts
