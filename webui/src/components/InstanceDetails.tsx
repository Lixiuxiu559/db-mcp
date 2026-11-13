import { useState } from "react";
import { ArrowLeft, ArrowRight, ChevronRight, Cable, Pencil, Database, Terminal, Network } from "lucide-react";
import type { AuditRecord, InstanceEntry } from "../types";
import { useResource } from "../lib/use-resource";
import { formatTime } from "../lib/utils";
import { Button } from "./ui/button";
import { Feedback, Status } from "./ui/feedback";

export interface ConnectionCheck { ok: boolean; message: string; }
const tabs = [{ id: "overview", label: "概览" }, { id: "config", label: "连接配置" }, { id: "audit", label: "审计记录" }] as const;
type Tab = (typeof tabs)[number]["id"];
export function InstanceDetails({ instance, check, busy, onBack, onTest, onEdit, onDelete }: {
  instance: InstanceEntry; check?: ConnectionCheck; busy: boolean;
  onBack: () => void; onTest: () => void; onEdit: () => void; onDelete: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const audit = useResource<{ records: AuditRecord[]; total: number }>(`/api/audit?kind=mcp&instance=${encodeURIComponent(instance.name)}&limit=20`, { records: [], total: 0 });
  const type = instance.type === "postgres" ? "PostgreSQL" : "Redis";
  const permission = instance.permissions === "write" ? "读写 · write" : "只读 · read";
  function back() {
    onBack();
    requestAnimationFrame(() => document.getElementById("instances-heading")?.focus());
  }
  const fields = [
    ["数据库类型", type], ["目标主机", instance.host], ["端口", instance.port],
    [instance.type === "postgres" ? "数据库名称" : "数据库编号", instance.database],
    ["连接账号", instance.user || instance.username || "默认账号"],
    ...(instance.type === "postgres" ? [["默认 Schema", instance.schema || "public"]] : []),
    ["工具权限", permission], ["连接密码", instance.has_password ? "已配置，仅写入不回显" : "未配置"],
  ];
  return <>
    <div className="detail-breadcrumb"><Button variant="quiet" onClick={back}><ArrowLeft size={14} />数据库实例</Button><ChevronRight size={12} aria-hidden="true" /><span>{instance.name}</span></div>
    <div className="instance-detail-heading">
      <div className="detail-title"><h1>{instance.name}</h1><Status value={instance.enabled ? "enabled" : "disabled"} /></div>
      <div className="detail-address"><span className="tag">{type}</span><span>{instance.host}:{instance.port}</span><span className="permission-label">{permission}</span></div>
      <div className="button-row"><Button disabled={busy} onClick={onTest}><Cable size={14} />{busy ? "检测中…" : "测试连接"}</Button><Button onClick={onEdit} disabled={busy}><Pencil size={14} />编辑</Button><Button variant="danger" onClick={onDelete} disabled={busy}>删除配置</Button></div>
    </div>
    <section className="panel reference-path-panel">
      <h2>连接路径</h2>
      <div className="reference-path"><div><Terminal size={14} aria-hidden="true" /><span>MCP 客户端</span></div><ArrowRight size={14} aria-hidden="true" /><div><Network size={14} aria-hidden="true" /><span>DB MCP 网关</span></div><ArrowRight size={14} aria-hidden="true" /><div><Database size={14} aria-hidden="true" /><span>{type} :{instance.port}</span></div></div>
      <div className="path-result"><Status value={check ? check.ok ? "ok" : "error" : "unchecked"} label={check ? check.ok ? "检测通过" : "连接失败" : "尚未检测"} /><p>{check?.message || "配置路径，不代表存在活跃连接。检测结果来自手动测试。"}</p></div>
    </section>
    <div className="detail-tabs" role="tablist" aria-label="数据库实例详情">{tabs.map((item, index) => <button key={item.id} id={`instance-tab-${item.id}`} role="tab" aria-selected={tab === item.id} aria-controls={`instance-panel-${item.id}`} tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)} onKeyDown={(event) => {
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault(); setTab(tabs[next].id);
      document.getElementById(`instance-tab-${tabs[next].id}`)?.focus();
    }}>{item.label}</button>)}</div>
    <div className="detail-tab-content" role="tabpanel" id={`instance-panel-${tab}`} aria-labelledby={`instance-tab-${tab}`} tabIndex={0}>
      {(tab === "overview" || tab === "config") && <section className="panel reference-info-panel"><h2>{tab === "overview" ? "基本信息" : "连接配置"}</h2><table className="reference-info-table"><tbody>{fields.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td className="wrap">{value}</td></tr>)}</tbody></table>{tab === "config" && <Button onClick={onEdit}><Pencil size={14} />编辑配置</Button>}</section>}
      {(tab === "overview" || tab === "audit") && <section className="panel reference-info-panel"><div className="section-heading"><h2>{tab === "overview" ? "最近调用" : "审计记录"}</h2><Button variant="quiet" onClick={audit.reload} disabled={audit.loading}>刷新</Button></div><Feedback message={audit.error} /><div className="table-scroll"><table className="data-table"><thead><tr><th scope="col">时间</th><th scope="col">Token</th><th scope="col">工具</th><th scope="col">结果</th></tr></thead><tbody>{audit.data.records.slice(0, tab === "overview" ? 3 : 20).map(record => <tr key={record.id}><td className="nowrap">{formatTime(record.ts)}</td><td>{record.token_name || record.token_id || record.user || "历史调用"}</td><td>{record.tool || "—"}</td><td><Status value={record.status} /></td></tr>)}</tbody></table></div>{!audit.data.records.length && <p className="empty-inline">{audit.loading ? "正在读取…" : audit.error ? "调用记录加载失败" : "暂无调用记录"}</p>}{tab === "audit" && <p className="page-note">展示最近 {audit.data.records.length} 条，共 {audit.data.total} 条记录。完整筛选和详情请前往 MCP 调用审计。</p>}</section>}
    </div>
  </>;
}
