"""运行时共享状态：配置 + 连接池 + 审计日志。

网页修改配置后调用 upsert_instance/delete_instance，替换内存配置并失效受影响的
连接池条目，工具下一次调用即生效，无需重启。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audit import AuditLogger
from .auth import IdentityError, IdentityStore
from .auth.expiry import is_expired
from .auth.scope import call_scope
from .auth.store import WorkspaceConflict
from .config import ConfigError, ConfigFile, GatewayConfig, InstanceConfig
from .db.pool import ConnectionPool
from .errors import PermissionDeniedError


async def _finish_locked(operation):
    """持锁期间即使请求被取消，也要等已提交的写入发布完毕再释放锁。"""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


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
        self.identity.ensure_default_account()
        try:
            self.validate_workspaces([
                wid for inst in self._config.instances.values() for wid in inst.workspace_ids
            ])
        except IdentityError as exc:
            self.identity.close()
            self.audit.close()
            raise ConfigError(str(exc)) from exc
        self._maintenance_task: asyncio.Task | None = None
        self.pool = ConnectionPool(self._get_config, self.authorize_attempt)
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

    async def authorize_attempt(self, name: str, expected: InstanceConfig) -> None:
        """排队、重连或取连接后，重新检查凭证、当前归属以及工具已校验的配置。"""
        scope = call_scope.get()
        item = await asyncio.to_thread(self.identity.active_token_info, *scope) if scope else None
        # worker 的有效结果可能在等待恢复时跨过截止；这里不能再 await。
        if (item is None or (item["id"], item["workspace_id"]) != scope
                or is_expired(item["expires_at"])):
            raise PermissionDeniedError("MCP Token 无效、已停用、已到期或缺少有效的工作空间")
        current = self.config.resolve_instance(name, scope[1])
        if current is not expected and current.connection_signature() != expected.connection_signature():
            raise PermissionDeniedError("实例配置已变更，请重新调用工具")

    def validate_workspaces(self, ids: list[str]) -> None:
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise IdentityError("所属工作空间须为 ID 列表")
        if ids and (set(ids) - {w["id"] for w in self.identity.list_workspaces()}):
            raise IdentityError("所属工作空间不存在")

    async def delete_workspace(self, workspace_id: str, event=None) -> None:
        async with self._lock:
            if self.config.visible_instances(workspace_id):
                raise WorkspaceConflict("请先解除该工作空间的实例归属")
            await _finish_locked(asyncio.to_thread(self.identity.delete_workspace, workspace_id, event))

    async def upsert_instance(
        self, name: str, entry: dict[str, Any], *, create_only: bool = False, event=None
    ) -> GatewayConfig:
        async with self._lock:
            if "workspace_ids" in entry:
                await asyncio.to_thread(self.validate_workspaces, entry["workspace_ids"])
            old = self._config.get_instance(name)
            if create_only and old is not None:
                raise ConfigError("实例名已存在，请使用编辑操作")
            config = await _finish_locked(
                self._publish_config(self._cfg_file.upsert_instance, name, entry)
            )
            new = config.get_instance(name)
            if event is not None and new is not None:
                before = ", ".join(old.workspace_ids) if old else ""
                after = ", ".join(new.workspace_ids)
                event["summary"] += f"，所属工作空间 {before or '无'} → {after or '无'}"
        return config

    async def delete_instance(self, name: str) -> GatewayConfig:
        async with self._lock:
            return await _finish_locked(self._publish_config(self._cfg_file.delete_instance, name))

    async def _publish_config(self, write, name, *args):
        old = self.config.get_instance(name)
        config = await asyncio.to_thread(write, name, *args)
        self._config = config
        new = config.get_instance(name)
        if old and (not new or old.connection_signature() != new.connection_signature()):
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
