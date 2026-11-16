import { useEffect, useRef } from "react";
import { Breadcrumb, Button, Card, Descriptions, Flex, Space, Tabs, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ArrowLeft, ArrowRight, Cable, Database, Network, Pencil, RefreshCw, Terminal } from "lucide-react";
import type { AuditRecord, InstanceEntry } from "../types";
import { useResource } from "../lib/use-resource";
import { useListPagination } from "../lib/pagination";
import { formatCompactTime, formatTime } from "../lib/utils";
import { AppTable } from "./AppTable";
import { Feedback, Status } from "./Feedback";
import { ListNotice } from "./ListStates";

export interface ConnectionCheck {
  ok: boolean;
  message: string;
}

export function InstanceDetails({
  instance, check, busy, onBack, onTest, onEdit, onDelete,
}: {
  instance: InstanceEntry;
  check?: ConnectionCheck;
  busy: boolean;
  onBack: () => void;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const title = useRef<HTMLHeadingElement>(null);
  const { pagination } = useListPagination();
  const { current, pageSize } = pagination;
  const query = `kind=mcp&instance=${encodeURIComponent(instance.name)}`;
  const recentAudit = useResource<{ records: AuditRecord[]; total: number }>(
    `/api/audit?${query}&limit=3`, { records: [], total: 0 },
  );
  const audit = useResource<{ records: AuditRecord[]; total: number }>(
    `/api/audit?${query}&limit=${pageSize}&offset=${(current - 1) * pageSize}`,
    { records: [], total: 0 },
  );
  useEffect(() => { title.current?.focus(); }, []);
  useEffect(() => {
    if (audit.loading || audit.error) return;
    const lastPage = Math.max(1, Math.ceil(audit.data.total / pageSize));
    if (current > lastPage) pagination.onChange(lastPage, pageSize);
  }, [audit.loading, audit.error, audit.data.total, current, pageSize, pagination.onChange]);

  const type = instance.type === "postgres" ? "PostgreSQL" : "Redis";
  const permission = instance.permissions === "write" ? "读写 · write" : "只读 · read";
  const fields = [
    { key: "type", label: "数据库类型", children: type },
    { key: "host", label: "目标主机", children: <span className="wrap-anywhere">{instance.host}</span> },
    { key: "port", label: "端口", children: instance.port },
    { key: "database", label: instance.type === "postgres" ? "数据库名称" : "数据库编号", children: <span className="wrap-anywhere">{instance.database}</span> },
    { key: "username", label: instance.type === "postgres" ? "连接账号" : "ACL 用户名", children: <span className="wrap-anywhere">{instance.user || instance.username || "默认账号"}</span> },
    ...(instance.type === "postgres" ? [{ key: "schema", label: "默认 Schema", children: <span className="wrap-anywhere">{instance.schema || "public"}</span> }] : []),
    { key: "permissions", label: "工具权限", children: permission },
    { key: "enabled", label: "实例状态", children: <Status value={instance.enabled ? "enabled" : "disabled"} /> },
    { key: "password", label: "连接密码", children: instance.has_password ? "已配置，仅写入不回显" : "未配置" },
  ];
  const columns: ColumnsType<AuditRecord> = [
    { key: "time", title: "时间", width: 190, render: (_, record) => <span className="table-time">{formatTime(record.ts)}</span> },
    { key: "token", title: "Token", render: (_, record) => record.token_name || record.token_id || record.user || "历史调用" },
    { key: "tool", title: "工具", render: (_, record) => record.tool || "—" },
    { key: "status", title: "状态", width: 110, render: (_, record) => <Status value={record.status} /> },
  ];

  const overview = (
    <Flex vertical gap={16}>
      <Card title="基本信息"><Descriptions column={2} items={fields} /></Card>
      <Card title="最近调用" loading={recentAudit.loading && !recentAudit.data.records.length}
        extra={<Button type="text" aria-label="刷新最近调用" loading={recentAudit.loading} icon={<RefreshCw size={15} aria-hidden="true" />} onClick={recentAudit.reload} />}>
        <Feedback message={recentAudit.error} />
        {recentAudit.error && <Button onClick={recentAudit.reload} style={{ marginBottom: 12 }}>重试</Button>}
        <Flex vertical gap={12}>
          {recentAudit.data.records.map((record) => (
            <Flex key={record.id} justify="space-between" align="center" gap={16}>
              <Flex vertical gap={2} style={{ minWidth: 0 }}>
                <Typography.Text className="wrap-anywhere">{record.tool || "—"} · {record.token_name || record.token_id || record.user || "历史调用"}</Typography.Text>
                <Typography.Text type="secondary"><time dateTime={record.ts}>{formatCompactTime(record.ts, true)}</time></Typography.Text>
              </Flex>
              <Status value={record.status} />
            </Flex>
          ))}
        </Flex>
        {!recentAudit.loading && !recentAudit.error && !recentAudit.data.records.length && <ListNotice compact title="暂无调用记录" description="通过 MCP 调用此实例后，这里会显示最近的工具调用与结果。" />}
      </Card>
    </Flex>
  );
  const config = (
    <Card title="连接配置">
      <Descriptions column={2} items={fields} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>
        {instance.permissions === "write" ? "此实例允许 MCP 工具读取和修改数据库内容。" : "此实例仅允许读取，写入请求会被拒绝。"}
        PostgreSQL 只读查询由数据库只读事务保护，高危 DDL 仍需调用方显式确认。
      </Typography.Paragraph>
      <Button onClick={onEdit} disabled={busy} icon={<Pencil size={15} aria-hidden="true" />}>编辑配置</Button>
    </Card>
  );
  const history = (
    <Card title="审计记录" extra={<Button type="text" aria-label="刷新实例审计" loading={audit.loading} icon={<RefreshCw size={15} aria-hidden="true" />} onClick={audit.reload} />}>
      <Feedback message={audit.error} />
      {audit.error && <Button onClick={audit.reload} style={{ marginBottom: 16 }}>重试</Button>}
      {!audit.loading && !audit.error && !audit.data.total ? (
        <ListNotice title="此实例暂无调用记录" description="首次 MCP 调用后，这里会显示调用时间、Token、工具和执行结果。" />
      ) : (
        <AppTable<AuditRecord> aria-label="数据库实例审计记录" dataSource={audit.data.records} columns={columns} rowKey="id"
          loading={audit.loading} tableLayout="fixed" scroll={{ x: 680 }}
          pagination={{ ...pagination, total: audit.data.total, disabled: audit.loading }} />
      )}
    </Card>
  );

  return (
    <>
      <Breadcrumb style={{ marginBottom: 16 }} items={[
        { title: <Button type="link" size="small" onClick={onBack} icon={<ArrowLeft size={14} aria-hidden="true" />}>数据库实例</Button> },
        { title: <span className="wrap-anywhere">{instance.name}</span> },
      ]} />
      <Flex justify="space-between" align="start" gap={16} wrap style={{ marginBottom: 20 }}>
        <Flex vertical gap={8} style={{ minWidth: 0, flex: 1 }}>
          <Space wrap>
            <Typography.Title ref={title} tabIndex={-1} level={1} className="wrap-anywhere" style={{ margin: 0 }}>{instance.name}</Typography.Title>
            <Status value={instance.enabled ? "enabled" : "disabled"} />
          </Space>
          <Space wrap>
            <Tag>{type}</Tag>
            <Typography.Text className="wrap-anywhere">{instance.host}:{instance.port}</Typography.Text>
            <Tag color={instance.permissions === "write" ? "warning" : "default"}>{permission}</Tag>
          </Space>
        </Flex>
        <Space>
          <Button disabled={busy} loading={busy} onClick={onTest} icon={<Cable size={15} aria-hidden="true" />}>测试连接</Button>
          <Button disabled={busy} onClick={onEdit} icon={<Pencil size={15} aria-hidden="true" />}>编辑</Button>
          <Button danger disabled={busy} onClick={onDelete}>删除配置</Button>
        </Space>
      </Flex>
      <Card size="small" style={{ marginBottom: 16 }}>
        <Flex align="center" justify="space-between" gap={16} wrap>
          <Space size={12} wrap>
            <Space size={6}><Terminal size={15} aria-hidden="true" /><Typography.Text>MCP 客户端</Typography.Text></Space>
            <ArrowRight size={14} aria-hidden="true" />
            <Space size={6}><Network size={15} aria-hidden="true" /><Typography.Text>DB MCP 网关</Typography.Text></Space>
            <ArrowRight size={14} aria-hidden="true" />
            <Space size={6}><Database size={15} aria-hidden="true" /><Typography.Text>{type} 目标实例</Typography.Text></Space>
          </Space>
          <Status value={check ? (check.ok ? "ok" : "error") : "unchecked"} label={check ? (check.ok ? "检测通过" : "连接失败") : "尚未检测"} />
        </Flex>
        <Typography.Paragraph type={check && !check.ok ? "danger" : "secondary"} className="wrap-anywhere" style={{ margin: "12px 0 0" }}>
          {check?.message || "配置路径不代表存在活跃连接，连接结果来自手动测试。"}
        </Typography.Paragraph>
      </Card>
      <Tabs defaultActiveKey="overview" items={[
        { key: "overview", label: "概览", children: overview },
        { key: "config", label: "连接配置", children: config },
        { key: "audit", label: "审计记录", children: history },
      ]} />
    </>
  );
}
