"""配置模型与 YAML 读写。

设计要点（与 ssh-mcp/config.py 同源）：
- ${ENV_VAR} 插值在 pydantic 校验前做，缺变量直接报错（fail-fast），不支持静默空串
- 保存时写回"原始树"（未插值的 YAML dict），保证 ${ENV} 引用不会落盘成明文
- 网页编辑某个实例时若未填密码，保留原密码（含其 ${ENV} 引用）

与 ssh-mcp 的差异：实例是 name -> 配置 的映射（对齐参考 TS 版 db-mcp），
而非列表；权限用 permissions: read|write 表达，而非按凭据类型区分。
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOLLAR_SENTINEL = "\x00DBMCP_DOLLAR\x00"
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """配置文件错误。"""


def expand_env_vars(node: Any) -> Any:
    """递归展开字符串中的 ${ENV_VAR} 引用；$$ 转义为字面 $；缺变量报错。"""
    if isinstance(node, str):
        text = node.replace("$$", _DOLLAR_SENTINEL)

        def _repl(match: re.Match[str]) -> str:
            name = match.group(1)
            value = os.environ.get(name)
            if value is None:
                raise ConfigError(f"环境变量 {name} 未设置（配置中引用为 ${{{name}}}）")
            return value

        return _ENV_PATTERN.sub(_repl, text).replace(_DOLLAR_SENTINEL, "$")
    if isinstance(node, dict):
        return {key: expand_env_vars(value) for key, value in node.items()}
    if isinstance(node, list):
        return [expand_env_vars(item) for item in node]
    return node


# ---------------------------------------------------------------- 模型


class ListenerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8807


class UserEntry(BaseModel):
    username: str
    token: SecretStr
    admin: bool = False


class AuthConfig(BaseModel):
    # users 仅用于旧 MCP token 的一次性迁移，不再是网页人员账号。
    users: list[UserEntry] = Field(default_factory=list)
    database: Path = Path("data/service.db")
    session_hours: int = Field(default=8, ge=1, le=168)
    cookie_secure: bool = False
    cookie_path: str = "/"

    @model_validator(mode="after")
    def _ensure_users(self) -> AuthConfig:
        names = [u.username for u in self.users]
        if len(names) != len(set(names)):
            raise ValueError("auth.users 中存在重复的 username")
        if not self.cookie_path.startswith("/") or not self.cookie_path.endswith("/"):
            raise ValueError("auth.cookie_path 必须以 / 开始和结束")
        return self



class AuditConfig(BaseModel):
    path: Path = Path("audit/audit.db")
    retention_days: int = 30  # 只保留最近 N 天，超期记录自动清理
    # 审计记录里单条 SQL/命令的最大长度。SQL 明文是排查问题的关键，
    # 但要防止超大 INSERT 语句把审计库撑爆，故设上限并截断。
    max_statement_chars: int = 4000


class LimitsConfig(BaseModel):
    connect_timeout_seconds: int = 10
    query_timeout_seconds: int = 60
    max_query_timeout_seconds: int = 600
    max_rows: int = 1000
    max_result_chars: int = 50000
    max_pool_size: int = 5
    max_concurrency_per_instance: int = 8


_Permissions = Literal["read", "write"]


class PostgresInstance(BaseModel):
    type: Literal["postgres"] = "postgres"
    host: str
    port: int = 5432
    database: str
    user: str
    password: SecretStr = SecretStr("")
    permissions: _Permissions = "read"
    schema_: str = Field(default="public", alias="schema")
    enabled: bool = True

    model_config = {"populate_by_name": True}


class RedisInstance(BaseModel):
    type: Literal["redis"] = "redis"
    host: str
    port: int = 6379
    username: str = ""
    password: SecretStr = SecretStr("")
    database: int = 0
    permissions: _Permissions = "read"
    enabled: bool = True


InstanceConfig = Annotated[
    PostgresInstance | RedisInstance,
    Field(discriminator="type"),
]


class GatewayConfig(BaseModel):
    server: ListenerConfig = Field(default_factory=ListenerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    instances: dict[str, InstanceConfig] = Field(default_factory=dict)

    @field_validator("instances")
    @classmethod
    def _check_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            if not _NAME_PATTERN.match(name):
                raise ValueError(
                    f"实例名 {name!r} 非法：只能包含字母、数字、下划线和连字符"
                )
        return value

    def get_instance(self, name: str) -> InstanceConfig | None:
        return self.instances.get(name)

    def resolve_instance(self, name: str) -> InstanceConfig:
        """取实例并校验启用状态；不存在/已禁用给出可读错误。"""
        from .errors import InstanceDisabledError, InstanceNotFoundError

        entry = self.instances.get(name)
        if entry is None:
            raise InstanceNotFoundError(name, sorted(self.instances))
        if not entry.enabled:
            raise InstanceDisabledError(name)
        return entry

    def summary(self) -> str:
        """掩码密码的配置摘要，用于调试输出。"""
        lines = [
            f"监听 {self.server.host}:{self.server.port}",
            f"旧 MCP 凭证 {len(self.auth.users)} 个（仅用于首次迁移）",
            f"审计日志 {self.audit.path}",
        ]
        for name, inst in self.instances.items():
            lines.append(
                f"  {name} -> {inst.type} {inst.host}:{inst.port} "
                f"({inst.permissions}, {'启用' if inst.enabled else '禁用'})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------- 文件读写

_INSTANCE_KEYS = (
    "type",
    "host",
    "port",
    "database",
    "user",
    "username",
    "password",
    "permissions",
    "schema",
    "enabled",
)


def sanitize_instance_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """只保留合法字段，避免网页提交未知键污染配置文件。"""
    return {k: v for k, v in entry.items() if k in _INSTANCE_KEYS and v is not None}


class ConfigFile:
    """YAML 配置文件：读入原始树，插值后校验；保存时写回原始树。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise ConfigError(f"配置文件不存在: {self.path}")
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件不是合法的 YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件内容应为 YAML 映射: {self.path}")
        self.raw: dict[str, Any] = raw

    def load(self) -> GatewayConfig:
        try:
            config = GatewayConfig.model_validate(expand_env_vars(deepcopy(self.raw)))
            base = self.path.resolve().parent
            if not config.auth.database.is_absolute():
                config.auth.database = base / config.auth.database
            if not config.audit.path.is_absolute():
                config.audit.path = base / config.audit.path
            return config
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"配置校验失败: {exc}") from exc

    def save(self) -> None:
        """原子写回原始树，并收紧文件权限（配置里可能含明文密码）。"""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def upsert_instance(self, name: str, entry: dict[str, Any]) -> GatewayConfig:
        """新增或更新一个数据库实例；校验失败会回滚内存中的原始树。"""
        if not _NAME_PATTERN.match(name):
            raise ConfigError(
                f"实例名 {name!r} 非法：只能包含字母、数字、下划线和连字符"
            )
        entry = sanitize_instance_entry(entry)
        instances: dict[str, Any] = self.raw.setdefault("instances", {})
        snapshot = deepcopy(instances)
        try:
            existing = instances.get(name)
            if existing is not None and not entry.get("password"):
                # 编辑时未填密码：保留原密码（含 ${ENV} 引用）
                entry = {**entry, "password": existing.get("password", "")}
            instances[name] = entry
            config = self.load()
            self.save()
        except Exception:
            self.raw["instances"] = snapshot
            raise
        return config

    def delete_instance(self, name: str) -> GatewayConfig:
        instances: dict[str, Any] = self.raw.get("instances", {})
        if name not in instances:
            raise ConfigError(f"实例 {name!r} 不存在")
        snapshot = deepcopy(instances)
        try:
            del instances[name]
            config = self.load()
            self.save()
        except Exception:
            self.raw["instances"] = snapshot
            raise
        return config
