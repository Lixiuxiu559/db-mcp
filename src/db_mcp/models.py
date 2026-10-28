"""工具返回的数据模型（pydantic），FastMCP 会自动生成结构化输出 schema。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class InstanceInfo(BaseModel):
    name: str
    type: Literal["postgres", "redis"]
    permissions: Literal["read", "write"]
    enabled: bool
    host: str
    port: int
    description: str = ""
    # postgres 专有
    database: str | None = None
    user: str | None = None
    schema_name: str | None = None


class InstanceListResult(BaseModel):
    instances: list[InstanceInfo]


class PgQueryResult(BaseModel):
    instance: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    command: str = ""
    truncated: bool = False
    duration_ms: float = 0.0


class PgExecuteResult(BaseModel):
    instance: str
    command: str = ""
    row_count: int = 0
    rows: list[list[Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0


class TableInfo(BaseModel):
    schema_name: str
    name: str
    type: Literal["table", "view", "materialized_view", "foreign_table", "other"] = "table"
    row_estimate: int | None = None


class PgTableListResult(BaseModel):
    instance: str
    schema_name: str
    tables: list[TableInfo]


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    comment: str | None = None


class PgDescribeResult(BaseModel):
    instance: str
    schema_name: str
    table: str
    exists: bool
    columns: list[ColumnInfo] = Field(default_factory=list)


class PgIndexInfo(BaseModel):
    name: str
    columns: list[str] = Field(default_factory=list)
    unique: bool = False
    primary: bool = False
    definition: str | None = None


class PgIndexListResult(BaseModel):
    instance: str
    schema_name: str
    table: str
    indexes: list[PgIndexInfo]


class SchemaListResult(BaseModel):
    instance: str
    schemas: list[str]


class RedisValueResult(BaseModel):
    instance: str
    key: str
    type: str
    value: Any = None
    ttl: int | None = None
    exists: bool = True


class RedisSetResult(BaseModel):
    instance: str
    key: str
    value: Any = None
    ttl: int | None = None


class RedisDeleteResult(BaseModel):
    instance: str
    deleted: int
    requested: int


class RedisKeysResult(BaseModel):
    instance: str
    pattern: str
    keys: list[str] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False


class RedisScanResult(BaseModel):
    instance: str
    cursor: str
    keys: list[str] = Field(default_factory=list)
    count: int = 0
    finished: bool = False


class RedisExpireResult(BaseModel):
    instance: str
    key: str
    seconds: int
    success: bool
    ttl: int | None = None


class RedisListResult(BaseModel):
    instance: str
    key: str
    values: list[str] = Field(default_factory=list)
    length: int | None = None
    truncated: bool = False


class RedisHashResult(BaseModel):
    instance: str
    key: str
    fields: dict[str, str] = Field(default_factory=dict)
    count: int = 0
    truncated: bool = False


class RedisSetMembersResult(BaseModel):
    instance: str
    key: str
    members: list[str] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False


class RedisZsetResult(BaseModel):
    instance: str
    key: str
    members: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False


class RedisBoolResult(BaseModel):
    instance: str
    key: str
    exists: bool


class RedisTtlResult(BaseModel):
    instance: str
    key: str
    ttl: int  # -2 不存在, -1 永不过期, >=0 剩余秒数


class PingResult(BaseModel):
    instance: str
    status: Literal["ok", "error"]
    version: str | None = None
    latency_ms: float | None = None
    error: str | None = None
