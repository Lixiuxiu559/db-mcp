"""配置模块测试：插值、校验、读写往返、热更新语义。"""

from __future__ import annotations

import os
import stat as st

import pytest

from db_mcp.config import ConfigError, expand_env_vars


def test_expand_env_resolves(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert expand_env_vars("${FOO}/x") == "bar/x"
    assert expand_env_vars({"a": ["${FOO}", 1]}) == {"a": ["bar", 1]}


def test_expand_env_missing_fails_fast(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        expand_env_vars("${NOPE}")


def test_expand_env_dollar_escape(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert expand_env_vars("$$${FOO}") == "$bar"


def test_load_interpolates_password(make_mcp):
    gw = make_mcp()
    inst = gw.state.config.get_instance("pg-read")
    assert inst.password.get_secret_value() == "pg-pw"


def test_instance_types_parsed(make_mcp):
    gw = make_mcp()
    assert gw.state.config.get_instance("pg-read").type == "postgres"
    assert gw.state.config.get_instance("redis-read").type == "redis"
    # schema 字段用别名映射到 schema_，且能读回原名
    assert gw.state.config.get_instance("pg-read").schema_ == "public"


def test_invalid_instance_name_rejected(make_mcp):
    with pytest.raises(ConfigError, match="实例名"):
        make_mcp("\n  bad name!:\n    type: redis\n    host: h\n    permissions: read\n")


def test_resolve_instance_raises_readable_errors(make_mcp):
    gw = make_mcp()
    from db_mcp.errors import InstanceDisabledError, InstanceNotFoundError

    with pytest.raises(InstanceNotFoundError, match="可用实例"):
        gw.state.config.resolve_instance("nope")
    with pytest.raises(InstanceDisabledError, match="已禁用"):
        gw.state.config.resolve_instance("pg-disabled")


async def test_upsert_new_instance_persists(make_mcp):
    gw = make_mcp()
    entry = {
        "type": "postgres",
        "host": "10.0.0.99",
        "database": "newdb",
        "user": "u",
        "password": "secret",
        "permissions": "read",
    }
    config = await gw.state.upsert_instance("pg-new", entry)
    assert config.get_instance("pg-new") is not None
    assert gw.state.config.get_instance("pg-new").host == "10.0.0.99"
    assert "pg-new" in gw.config_path.read_text()


async def test_upsert_keeps_password_when_omitted(make_mcp):
    gw = make_mcp()
    await gw.state.upsert_instance(
        "pg-read", {"type": "postgres", "host": "10.0.0.77", "database": "appdb", "user": "reader"}
    )
    assert gw.state.config.get_instance("pg-read").host == "10.0.0.77"
    # 原密码（含 ${ENV} 引用）被保留
    assert gw.cfg_file.raw["instances"]["pg-read"]["password"] == "${TEST_PG_PASSWORD}"


async def test_upsert_invalid_rolls_back(make_mcp):
    gw = make_mcp()
    with pytest.raises(ConfigError):
        await gw.state.upsert_instance("pg-bad", {"type": "unknown", "host": "h"})
    assert "pg-bad" not in gw.cfg_file.raw["instances"]


async def test_delete_instance(make_mcp):
    gw = make_mcp()
    await gw.state.delete_instance("redis-read")
    assert gw.state.config.get_instance("redis-read") is None
    assert "redis-read" not in gw.cfg_file.raw["instances"]


async def test_delete_missing_instance_errors(make_mcp):
    gw = make_mcp()
    with pytest.raises(ConfigError, match="不存在"):
        await gw.state.delete_instance("nope")


async def test_save_preserves_env_refs_and_chmod(make_mcp):
    gw = make_mcp()
    await gw.state.upsert_instance(
        "pg-read", {"type": "postgres", "host": "10.0.0.78", "database": "appdb", "user": "reader"}
    )
    mode = st.S_IMODE(os.stat(gw.config_path).st_mode)
    assert mode & 0o077 == 0  # 组/其他无权限
    text = gw.config_path.read_text()
    assert "${TEST_PG_PASSWORD}" in text  # 引用原样保留，未落盘明文
    assert "pg-pw" not in text


async def test_permissions_default_read(make_mcp):
    """未显式写 permissions 的实例默认为 read（安全默认）。"""
    gw = make_mcp(
        "\n  redis-default:\n    type: redis\n    host: h\n"
    )
    assert gw.state.config.get_instance("redis-default").permissions == "read"
