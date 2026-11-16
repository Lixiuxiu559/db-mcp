import { useState } from "react";
import { Button, Card, Flex, Input, Select, Space, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Cable, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { api, json } from "../api";
import type { InstanceEntry } from "../types";
import { useResource } from "../lib/use-resource";
import { useListPagination } from "../lib/pagination";
import { errorText } from "../lib/utils";
import { AppTable } from "../components/AppTable";
import { AppModal } from "../components/AppModal";
import { Feedback, Status } from "../components/Feedback";
import { ListNotice, ResourceStatus } from "../components/ListStates";
import { PageHeading } from "../components/PageHeading";
import { InstanceDetails, type ConnectionCheck } from "../components/InstanceDetails";
import { InstanceEditor } from "../components/InstanceEditor";

export default function Instances() {
  const { data, setData, loading, error, reload } = useResource<{
    instances: InstanceEntry[];
  }>("/api/instances", { instances: [] });
  const { pagination, resetPage } = useListPagination();
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [editor, setEditor] = useState<InstanceEntry | null | undefined>(undefined);
  const [deleting, setDeleting] = useState<InstanceEntry | null>(null);
  const [details, setDetails] = useState<InstanceEntry | null>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [checks, setChecks] = useState<Record<string, ConnectionCheck>>({});
  const filtered = data.instances.filter((instance) =>
    (!type || instance.type === type) &&
    `${instance.name} ${instance.type} ${instance.host} ${instance.database} ${instance.user || instance.username || ""}`
      .toLowerCase().includes(query.trim().toLowerCase()),
  );
  const hasFilters = !!(query || type);

  function clearFilters() {
    setQuery("");
    setType("");
    resetPage();
  }

  async function test(instance: InstanceEntry) {
    if (busy) return;
    setBusy(`test:${instance.name}`);
    setActionError("");
    setMessage("");
    try {
      const result = await api<{ ok: boolean; message?: string; error?: string }>(
        "/api/instances/test", json("POST", instance),
      );
      const check = {
        ok: result.ok,
        message: result.message || result.error || (result.ok ? "连接成功" : "连接失败"),
      };
      setChecks((old) => ({ ...old, [instance.name]: check }));
      if (check.ok) setMessage(`${instance.name}：${check.message}`);
      else setActionError(`${instance.name}：${check.message}`);
    } catch (error) {
      setActionError(errorText(error));
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!deleting || busy) return;
    setBusy("delete");
    setActionError("");
    try {
      await api(`/api/instances/${encodeURIComponent(deleting.name)}`, json("DELETE"));
      const name = deleting.name;
      setData((old) => ({ instances: old.instances.filter((instance) => instance.name !== name) }));
      setDeleting(null);
      setDetails(null);
      setChecks((old) => {
        const next = { ...old };
        delete next[name];
        return next;
      });
      const lastPage = Math.max(1, Math.ceil((filtered.length - 1) / pagination.pageSize));
      if (pagination.current > lastPage) pagination.onChange(lastPage, pagination.pageSize);
      reload();
      setMessage("实例配置已删除，历史审计保留。");
    } catch (error) {
      setActionError(errorText(error));
    } finally {
      setBusy("");
    }
  }

  function openDetails(instance: InstanceEntry) {
    setDetails(instance);
    setActionError("");
    setMessage("");
  }

  function backToList() {
    const name = details?.name;
    setDetails(null);
    requestAnimationFrame(() => {
      if (name) document.getElementById(`instance-link-${name}`)?.focus();
    });
  }

  function saved() {
    const name = editor?.name;
    if (name) setChecks((old) => {
      const next = { ...old };
      delete next[name];
      return next;
    });
    if (!data.instances.length) clearFilters();
    setEditor(undefined);
    reload();
    setMessage("实例配置已保存。");
  }

  const columns: ColumnsType<InstanceEntry> = [
    {
      key: "name", title: "数据库实例", width: 180,
      sorter: (a, b) => a.name.localeCompare(b.name),
      render: (_, instance) => (
        <Flex vertical gap={2} align="center">
          <Button id={`instance-link-${instance.name}`} type="link"
            style={{ padding: 0, height: "auto", whiteSpace: "normal", maxWidth: "100%", overflowWrap: "anywhere" }}
            aria-label={`查看 ${instance.name} 详情`} onClick={() => openDetails(instance)}>
            {instance.name}
          </Button>
          <Typography.Text type="secondary">{instance.type === "postgres" ? "PostgreSQL" : "Redis"}</Typography.Text>
        </Flex>
      ),
    },
    {
      key: "host", title: "目标端点", width: 220,
      render: (_, instance) => <span className="mono wrap-anywhere">{instance.host}:{instance.port}</span>,
    },
    {
      key: "database", title: "数据库 / 账号", width: 180,
      render: (_, instance) => (
        <Flex vertical gap={2} align="center">
          <Typography.Text>{instance.type === "redis" ? `DB ${instance.database}` : instance.database}</Typography.Text>
          <Typography.Text type="secondary">{instance.user || instance.username || "默认账号"}</Typography.Text>
        </Flex>
      ),
    },
    {
      key: "permissions", title: "工具权限", width: 130,
      render: (_, instance) => <Tag color={instance.permissions === "write" ? "warning" : "default"}>{instance.permissions === "write" ? "读写 · write" : "只读 · read"}</Tag>,
    },
    {
      key: "enabled", title: "状态", width: 100,
      render: (_, instance) => <Status value={instance.enabled ? "enabled" : "disabled"} />,
    },
    {
      key: "connection", title: "手动连接检测", width: 135,
      render: (_, instance) => {
        const check = checks[instance.name];
        return <Tooltip title={check?.message}><span><Status value={check ? (check.ok ? "ok" : "error") : "unchecked"} label={check ? (check.ok ? "检测通过" : "连接失败") : "尚未检测"} /></span></Tooltip>;
      },
    },
    {
      key: "actions", title: "操作", width: 180,
      render: (_, instance) => (
        <Flex gap={4} justify="center" align="center">
          <Button type="text" disabled={!!busy} loading={busy === `test:${instance.name}`}
            aria-label={`测试 ${instance.name} 连接`} icon={<Cable size={15} aria-hidden="true" />} onClick={() => void test(instance)}>测试</Button>
          <Tooltip title="编辑配置"><Button type="text" disabled={!!busy} aria-label={`编辑 ${instance.name}`} icon={<Pencil size={15} aria-hidden="true" />} onClick={() => setEditor(instance)} /></Tooltip>
          <Tooltip title="删除配置"><Button danger type="text" disabled={!!busy} aria-label={`删除 ${instance.name}`} icon={<Trash2 size={15} aria-hidden="true" />} onClick={() => { setDeleting(instance); setActionError(""); }} /></Tooltip>
        </Flex>
      ),
    },
  ];
  const selected = details ? data.instances.find((instance) => instance.name === details.name) || details : null;

  return (
    <>
      <Feedback message={deleting ? "" : actionError} onDismiss={() => setActionError("")} />
      <Feedback message={message} success onDismiss={() => setMessage("")} />
      {selected ? (
        <InstanceDetails key={selected.name} instance={selected} check={checks[selected.name]} busy={!!busy}
          onBack={backToList} onTest={() => void test(selected)} onEdit={() => setEditor(selected)}
          onDelete={() => { setDeleting(selected); setActionError(""); }} />
      ) : (
        <>
          <PageHeading title="数据库实例" />
          {!data.instances.length && (loading || error) ? (
            <ResourceStatus loading={loading} error={error} onRetry={reload} />
          ) : (
            <Card style={{ minWidth: 0 }}>
              <Flex gap={12} align="center" wrap style={{ marginBottom: 16 }}>
                <Input allowClear aria-label="搜索数据库实例" placeholder="搜索名称、主机、数据库或账号…"
                  prefix={<Search size={15} aria-hidden="true" />} value={query}
                  onChange={(event) => { setQuery(event.target.value); resetPage(); }} style={{ flex: 1, minWidth: 220 }} />
                <Select aria-label="筛选数据库类型" value={type} style={{ width: 150 }}
                  options={[{ value: "", label: "全部类型" }, { value: "postgres", label: "PostgreSQL" }, { value: "redis", label: "Redis" }]}
                  onChange={(value) => { setType(value); resetPage(); }} />
                {hasFilters && <Button type="text" onClick={clearFilters}>重置筛选</Button>}
                <Flex gap={12} align="center" style={{ marginLeft: "auto" }}>
                  <Typography.Text type="secondary" className="nowrap">共 {filtered.length} 个实例</Typography.Text>
                  <Button type="text" aria-label="刷新实例列表" loading={loading} icon={<RefreshCw size={15} aria-hidden="true" />} onClick={reload} />
                  <Button type="primary" icon={<Plus size={15} aria-hidden="true" />} onClick={() => setEditor(null)}>添加实例</Button>
                </Flex>
              </Flex>
              <Feedback message={error} />
              {error && <Button onClick={reload} style={{ marginBottom: 16 }}>重试</Button>}
              {!filtered.length && !loading && !error ? (
                <ListNotice title={hasFilters ? "没有匹配的数据库实例" : "添加第一个数据库实例"}
                  description={hasFilters ? "试试其他名称、主机或数据库类型。" : "添加 PostgreSQL 或 Redis 连接，并设置 MCP 工具的只读或读写权限。"}
                  onClear={hasFilters ? clearFilters : undefined}
                  action={!hasFilters ? <Button type="primary" icon={<Plus size={15} aria-hidden="true" />} onClick={() => setEditor(null)}>添加实例</Button> : undefined} />
              ) : (
                <AppTable<InstanceEntry> aria-label="数据库实例列表" dataSource={filtered} columns={columns} rowKey="name"
                  loading={loading} pagination={{ ...pagination, total: filtered.length }} tableLayout="fixed" scroll={{ x: 1125 }} />
              )}
              <Typography.Paragraph type="secondary" style={{ margin: "16px 0 0" }}>
                检测结果来自手动连接测试。连接密码仅由网关持有，不向 MCP 客户端返回。
              </Typography.Paragraph>
            </Card>
          )}
        </>
      )}
      {editor !== undefined && <InstanceEditor editing={editor} onClose={() => setEditor(undefined)} onSaved={saved} />}
      <AppModal opened={!!deleting} busy={!!busy} onClose={() => { if (!busy) setDeleting(null); }} title="删除实例配置"
        footer={<Space><Button onClick={() => setDeleting(null)} disabled={!!busy}>取消</Button><Button danger type="primary" loading={busy === "delete"} disabled={!!busy} onClick={() => void remove()}>删除配置</Button></Space>}>
        <Typography.Paragraph>
          确认删除 <Typography.Text strong className="wrap-anywhere">{deleting?.name}</Typography.Text>？后续 MCP 调用将无法再使用此实例，历史审计仍按保留期留存。
        </Typography.Paragraph>
        <Feedback message={actionError} />
      </AppModal>
    </>
  );
}
