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


def test_runtime_paths_are_relative_to_config_directory(tmp_path, monkeypatch):
    from db_mcp.config import ConfigFile

    config_dir = tmp_path / "configured"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text("instances: {}\n")
    monkeypatch.chdir(tmp_path)
    cfg = ConfigFile(path).load()
    assert cfg.auth.database == config_dir / "data" / "service.db"
    assert cfg.audit.path == config_dir / "audit" / "audit.db"
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8807
    assert cfg.auth.users == []


def test_auth_settings_validation():
    from pydantic import ValidationError

    from db_mcp.config import AuthConfig

    assert AuthConfig(cookie_path="/db-mcp/").cookie_path == "/db-mcp/"
    with pytest.raises(ValidationError, match="cookie_path"):
        AuthConfig(cookie_path="/db-mcp")
    with pytest.raises(ValidationError, match="session_hours"):
        AuthConfig(session_hours=0)


def test_cli_initializes_private_empty_config_without_overwrite(tmp_path, monkeypatch, capsys):
    from db_mcp.cli import main
    from db_mcp.config import ConfigFile

    path = tmp_path / "runtime" / "config.yaml"
    monkeypatch.setattr("sys.argv", ["db-mcp", "--init", "--config", str(path)])
    main()
    assert st.S_IMODE(path.stat().st_mode) == 0o600
    config = ConfigFile(path).load()
    assert config.instances == {} and config.auth.users == []
    assert config.server.port == 8807
    original = path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert path.read_bytes() == original
    assert "未覆盖" in capsys.readouterr().err


async def test_failed_config_save_preserves_memory_and_disk(make_mcp, monkeypatch):
    from copy import deepcopy

    gw = make_mcp()
    original = deepcopy(gw.cfg_file.raw)
    original_bytes = gw.config_path.read_bytes()

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(gw.cfg_file, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        await gw.state.upsert_instance("redis-read", {"type": "redis", "host": "unsaved"})
    assert gw.cfg_file.raw == original
    assert gw.state.config.get_instance("redis-read").host == "10.0.0.40"
    assert gw.config_path.read_bytes() == original_bytes
    with pytest.raises(OSError, match="disk full"):
        await gw.state.delete_instance("redis-read")
    assert gw.cfg_file.raw == original
    assert gw.state.config.get_instance("redis-read") is not None
    assert gw.config_path.read_bytes() == original_bytes


async def test_changing_working_directory_preserves_identity_and_legacy_token(
    tmp_path, monkeypatch
):
    from db_mcp.config import ConfigFile
    from db_mcp.server import create_mcp

    configured = tmp_path / "persistent"
    configured.mkdir()
    path = configured / "config.yaml"
    path.write_text(
        "auth:\n  users:\n    - username: old-client\n      token: stable-legacy-token\ninstances: {}\n"
    )
    _, first = create_mcp(ConfigFile(path))
    token = first.identity.verify_token("stable-legacy-token")
    assert token is not None
    await first.shutdown()
    other = tmp_path / "another-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    _, second = create_mcp(ConfigFile(path))
    assert second.identity.path == configured / "data" / "service.db"
    assert second.identity.verify_token("stable-legacy-token")["id"] == token["id"]
    second.identity.revoke_token(token["id"])
    await second.shutdown()
    _, third = create_mcp(ConfigFile(path))
    assert third.identity.verify_token("stable-legacy-token") is None
    assert len(third.identity.list_tokens()) == 1
    await third.shutdown()
