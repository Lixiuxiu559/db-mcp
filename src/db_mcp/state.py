"""运行时共享状态：配置 + 连接池 + 审计日志。

网页修改配置后调用 upsert_instance/delete_instance，替换内存配置并失效受影响的
连接池条目，工具下一次调用即生效，无需重启。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audit import AuditLogger
from .auth import IdentityStore
from .config import ConfigError, ConfigFile, GatewayConfig, InstanceConfig
from .db.pool import ConnectionPool


def _instance_signature(inst: InstanceConfig) -> Any:
    """校验后的配置快照（已插值），用于判断连接参数是否变化。"""
    return inst.model_dump()


class DbState:
    def __init__(self, cfg_file: ConfigFile):
        self._cfg_file = cfg_file
        self._config = cfg_file.load()
        self.audit = AuditLogger(
            self._config.audit.path,
            retention_days=self._config.audit.retention_days,
            max_statement_chars=self._config.audit.max_statement_chars,
        )
        self.identity = IdentityStore(self._config.auth.database, self._config.auth.session_hours)
        self.identity.migrate_legacy(self._config.auth.users)
        self._maintenance_task: asyncio.Task | None = None
        self.pool = ConnectionPool(self._get_config)
        self._lock = asyncio.Lock()

    def _get_config(self) -> GatewayConfig:
        return self._config

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def cfg_file(self) -> ConfigFile:
        return self._cfg_file

    def replace_config(self, config: GatewayConfig) -> None:
        """配置文件被整体替换后刷新内存态。"""
        self._config = config

    async def upsert_instance(
        self, name: str, entry: dict[str, Any], *, create_only: bool = False
    ) -> GatewayConfig:
        async with self._lock:
            old = self._config.get_instance(name)
            if create_only and old is not None:
                raise ConfigError("实例名已存在，请使用编辑操作")
            config = await asyncio.to_thread(self._cfg_file.upsert_instance, name, entry)
            self._config = config
            new = config.get_instance(name)
            if old is not None and new is not None and _instance_signature(old) != _instance_signature(new):
                await self.pool.invalidate(name)
        return config

    async def delete_instance(self, name: str) -> GatewayConfig:
        async with self._lock:
            config = await asyncio.to_thread(self._cfg_file.delete_instance, name)
            self._config = config
            await self.pool.invalidate(name)
        return config

    async def flush_identity_events(self) -> None:
        try:
            pending = await asyncio.to_thread(self.identity.pending_events)
            for event_id, record in pending:
                result = await self.audit.log_person(record)
                if result is False:
                    break
                await asyncio.to_thread(self.identity.acknowledge_event, event_id)
        except Exception:
            logging.getLogger(__name__).exception("人员审计投递失败，保留待重试事件")

    async def _maintenance(self) -> None:
        while True:
            try:
                await self.flush_identity_events()
                await asyncio.to_thread(self.audit.prune)
            except Exception:
                logging.getLogger(__name__).exception("身份审计维护失败，将于下一周期重试")
            await asyncio.sleep(60)

    async def startup(self) -> None:
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(self._maintenance())

    async def shutdown(self) -> None:
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        await self.flush_identity_events()
        await self.pool.close_all()
        self.audit.close()
        self.identity.close()
