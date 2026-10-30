import { useMemo, useState } from "react";
import {
  Plus,
  Search,
  RefreshCw,
  Pencil,
  Trash2,
  Cable,
  Database,
  ArrowUpRight,
} from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import { api, json } from "../api";
import type { InstanceEntry } from "../types";
import { useResource } from "../lib/use-resource";
import { errorText } from "../lib/utils";
import { Button } from "../components/ui/button";
import { DataTable } from "../components/ui/data-table";
import { Feedback, Status } from "../components/ui/feedback";
import { Modal } from "../components/ui/dialog";
import { InstanceEditor } from "../components/InstanceEditor";

export default function Instances() {
  const { data, loading, error, reload } = useResource<{
    instances: InstanceEntry[];
  }>("/api/instances", { instances: [] });
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [editor, setEditor] = useState<InstanceEntry | null | undefined>(
    undefined,
  );
  const [deleting, setDeleting] = useState<InstanceEntry | null>(null);
  const [details, setDetails] = useState<InstanceEntry | null>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [checks, setChecks] = useState<
    Record<string, { ok: boolean; message: string }>
  >({});
  const filtered = data.instances.filter(
    (instance) =>
      (!type || instance.type === type) &&
      `${instance.name} ${instance.type} ${instance.host} ${instance.database} ${instance.user || instance.username || ""}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  async function test(instance: InstanceEntry) {
    setBusy(instance.name);
    setActionError("");
    setMessage("");
    try {
      const result = await api<{
        ok: boolean;
        message?: string;
        error?: string;
      }>("/api/instances/test", json("POST", instance));
      const check = {
        ok: result.ok,
        message:
          result.message ||
          result.error ||
          (result.ok ? "连接成功" : "连接失败"),
      };
      setChecks((old) => ({ ...old, [instance.name]: check }));
      if (!check.ok) setActionError(`${instance.name}：${check.message}`);
      else setMessage(`${instance.name}：${check.message}`);
    } catch (error) {
      setActionError(errorText(error));
    } finally {
      setBusy("");
    }
  }
  async function remove() {
    if (!deleting) return;
    setBusy("delete");
    setActionError("");
    try {
      await api(
        `/api/instances/${encodeURIComponent(deleting.name)}`,
        json("DELETE"),
      );
      setDeleting(null);
      reload();
      setMessage("实例配置已删除，历史审计保留。");
    } catch (error) {
      setActionError(errorText(error));
    } finally {
      setBusy("");
    }
  }
  const columns = useMemo<ColumnDef<InstanceEntry, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: "数据库实例",
        cell: ({ row }) => (
          <button
            className="name-link"
            onClick={() => setDetails(row.original)}
          >
            <span className="node-icon">
              <Database size={16} />
            </span>
            <span>
              <strong className="mono">{row.original.name}</strong>
              <small>
                {row.original.type === "postgres" ? "PostgreSQL" : "Redis"}
              </small>
            </span>
          </button>
        ),
      },
      {
        accessorKey: "host",
        header: "目标端点",
        cell: ({ row }) => (
          <span className="mono nowrap">
            {row.original.host}
            <span className="muted">:{row.original.port}</span>
          </span>
        ),
      },
      {
        accessorKey: "database",
        header: "数据库 / 账号",
        cell: ({ row }) => (
          <div className="identity-cell">
            <strong className="mono">{row.original.database}</strong>
            <small className="mono">
              {row.original.user || row.original.username || "默认账号"}
            </small>
          </div>
        ),
      },
      {
        accessorKey: "permissions",
        header: "访问权限",
        cell: ({ getValue }) => (
          <span
            className={`permission-label ${getValue() === "write" ? "permission-write" : ""}`}
          >
            {getValue() === "write" ? "读写 · write" : "只读 · read"}
          </span>
        ),
      },
      {
        accessorKey: "enabled",
        header: "状态",
        cell: ({ getValue }) => (
          <Status value={getValue() ? "enabled" : "disabled"} />
        ),
      },
      {
        id: "connection",
        header: "连接检测",
        cell: ({ row }) =>
          checks[row.original.name] ? (
            <span title={checks[row.original.name].message}>
              <Status
                value={checks[row.original.name].ok ? "ok" : "error"}
                label={checks[row.original.name].ok ? "检测通过" : "连接失败"}
              />
            </span>
          ) : (
            <span className="muted small">尚未检测</span>
          ),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <div className="row-actions">
            <Button
              variant="quiet"
              disabled={!!busy}
              onClick={() => void test(row.original)}
              title="测试连接"
            >
              <Cable size={14} />
              <span>{busy === row.original.name ? "检测中" : "测试"}</span>
            </Button>
            <Button
              variant="quiet"
              className="icon-button"
              disabled={!!busy}
              aria-label={`编辑 ${row.original.name}`}
              onClick={() => setEditor(row.original)}
            >
              <Pencil size={15} />
            </Button>
            <Button
              variant="quiet"
              className="icon-button danger-text"
              disabled={!!busy}
              aria-label={`删除 ${row.original.name}`}
              onClick={() => {
                setDeleting(row.original);
                setActionError("");
              }}
            >
              <Trash2 size={15} />
            </Button>
          </div>
        ),
      },
    ],
    [checks, busy],
  );
  const enabled = data.instances.filter((instance) => instance.enabled).length;
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">DATABASE CONNECTIONS</span>
          <h1>数据库实例</h1>
          <p>集中管理 PostgreSQL 与 Redis 连接，按实例约束工具读写范围。</p>
        </div>
        <Button variant="primary" onClick={() => setEditor(null)}>
          <Plus size={16} />
          添加实例
        </Button>
      </div>
      <div className="metric-band">
        <div>
          <span>已配置实例</span>
          <strong>
            {data.instances.length}
            <small>个数据库目标</small>
          </strong>
        </div>
        <div>
          <span>启用状态</span>
          <strong>
            {enabled}
            <small>个可供调用</small>
          </strong>
        </div>
        <div>
          <span>只读边界</span>
          <strong>
            {
              data.instances.filter(
                (instance) => instance.permissions === "read",
              ).length
            }
            <small>个只读实例</small>
          </strong>
        </div>
      </div>
      <Feedback message={actionError} onDismiss={() => setActionError("")} />
      <Feedback message={message} success onDismiss={() => setMessage("")} />
      <section className="panel">
        <div className="panel-toolbar">
          <div className="search-input">
            <Search size={16} />
            <input
              aria-label="搜索数据库实例"
              placeholder="名称、主机、数据库或账号…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select
            className="input filter-select"
            aria-label="数据库类型"
            value={type}
            onChange={(e) => setType(e.target.value)}
          >
            <option value="">全部类型</option>
            <option value="postgres">PostgreSQL</option>
            <option value="redis">Redis</option>
          </select>
          <div className="toolbar-end">
            <span className="muted small">{filtered.length} 个实例</span>
            <Button onClick={reload} disabled={loading} variant="quiet">
              <RefreshCw size={15} />
              刷新
            </Button>
          </div>
        </div>
        <DataTable
          data={filtered}
          columns={columns}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={query || type ? "没有匹配的数据库实例" : "还没有数据库实例"}
          emptyAction={
            query || type ? (
              <Button
                onClick={() => {
                  setQuery("");
                  setType("");
                }}
              >
                清除筛选
              </Button>
            ) : (
              <Button onClick={() => setEditor(null)}>
                <Plus size={15} />
                添加实例
              </Button>
            )
          }
        />
        <div className="panel-footnote">
          检测结果来自手动连接测试。连接密码仅由网关持有，不向 MCP 客户端返回。
        </div>
      </section>
      {editor !== undefined && (
        <InstanceEditor
          editing={editor}
          onClose={() => setEditor(undefined)}
          onSaved={() => {
            const name = editor?.name;
            if (name)
              setChecks((old) => {
                const next = { ...old };
                delete next[name];
                return next;
              });
            setEditor(undefined);
            reload();
            setMessage("实例配置已保存。");
          }}
        />
      )}
      <Modal
        open={!!deleting}
        onOpenChange={(open) => {
          if (!open && !busy) setDeleting(null);
        }}
        title="删除实例配置"
        description="移除网关中的连接配置，历史审计仍按保留期留存。"
      >
        <p>
          确认删除 <strong className="mono wrap">{deleting?.name}</strong>？后续
          MCP 调用将无法再使用此实例。
        </p>
        <Feedback message={actionError} />
        <div className="dialog-actions right">
          <Button onClick={() => setDeleting(null)} disabled={!!busy}>
            取消
          </Button>
          <Button
            variant="danger"
            onClick={() => void remove()}
            disabled={!!busy}
          >
            {busy ? "正在删除…" : "删除配置"}
          </Button>
        </div>
      </Modal>
      <Modal
        open={!!details}
        onOpenChange={(open) => {
          if (!open) setDetails(null);
        }}
        title={details?.name || "实例详情"}
        description="当前配置与手动检测结果。路径表示配置，不代表存在活跃会话。"
        wide
      >
        {details && (
          <>
            <div className="connection-path">
              <div>
                <span className="node-icon">
                  <ArrowUpRight size={18} />
                </span>
                <strong>数据客户端</strong>
                <small>MCP 调用方</small>
              </div>
              <span className="path-segment">
                <span>MCP</span>
              </span>
              <div>
                <span className="node-icon">
                  <Cable size={18} />
                </span>
                <strong>db-mcp</strong>
                <small>数据库网关</small>
              </div>
              <span className="path-segment">
                <span>
                  {details.type === "postgres" ? "PG" : "Redis"} :{details.port}
                </span>
              </span>
              <div>
                <span className="node-icon">
                  <Database size={18} />
                </span>
                <strong>{details.name}</strong>
                <small>{details.host}</small>
              </div>
            </div>
            <dl className="detail-grid">
              <dt>数据库类型</dt>
              <dd>{details.type === "postgres" ? "PostgreSQL" : "Redis"}</dd>
              <dt>目标端点</dt>
              <dd className="mono wrap">
                {details.host}:{details.port}
              </dd>
              <dt>数据库</dt>
              <dd className="mono wrap">{details.database}</dd>
              <dt>连接账号</dt>
              <dd className="mono wrap">
                {details.user || details.username || "默认账号"}
              </dd>
              {details.type === "postgres" && (
                <>
                  <dt>默认 Schema</dt>
                  <dd className="mono wrap">{details.schema || "public"}</dd>
                </>
              )}
              <dt>访问权限</dt>
              <dd>
                {details.permissions === "write"
                  ? "读写 · write"
                  : "只读 · read"}
              </dd>
              <dt>状态</dt>
              <dd>
                <Status value={details.enabled ? "enabled" : "disabled"} />
              </dd>
              <dt>连接密码</dt>
              <dd>
                {details.has_password ? "已配置，仅写入不回显" : "未配置"}
              </dd>
              <dt>最近手动检测</dt>
              <dd className="wrap">
                {checks[details.name]?.message || "尚未检测"}
              </dd>
            </dl>
            <div className="dialog-actions right">
              <Button
                onClick={() => {
                  setDetails(null);
                  setEditor(details);
                }}
              >
                <Pencil size={15} />
                编辑配置
              </Button>
            </div>
          </>
        )}
      </Modal>
    </>
  );
}
