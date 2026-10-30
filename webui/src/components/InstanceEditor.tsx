import { useRef, useState, type FormEvent } from "react";
import { Cable } from "lucide-react";
import type { InstanceEntry } from "../types";
import { api, json } from "../api";
import { Modal } from "./ui/dialog";
import { Field, Input, PasswordInput } from "./ui/field";
import { Switch } from "./ui/switch";
import { Button } from "./ui/button";
import { Feedback } from "./ui/feedback";
import { errorText } from "../lib/utils";

export function InstanceEditor({
  editing,
  onClose,
  onSaved,
}: {
  editing: InstanceEntry | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [name, setName] = useState(editing?.name || "");
  const [type, setType] = useState<InstanceEntry["type"]>(
    editing?.type || "postgres",
  );
  const [host, setHost] = useState(editing?.host || "");
  const [port, setPort] = useState(editing?.port || 5432);
  const [database, setDatabase] = useState(String(editing?.database ?? ""));
  const [username, setUsername] = useState(
    editing?.user || editing?.username || "",
  );
  const [schema, setSchema] = useState(editing?.schema || "public");
  const [password, setPassword] = useState("");
  const [permissions, setPermissions] = useState<InstanceEntry["permissions"]>(
    editing?.permissions || "read",
  );
  const [enabled, setEnabled] = useState(editing?.enabled ?? true);
  const [busy, setBusy] = useState<"save" | "test" | "">("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  function entry() {
    return {
      name: name.trim(),
      type,
      host: host.trim(),
      port,
      database: type === "redis" ? Number(database) : database.trim(),
      ...(type === "postgres"
        ? { user: username.trim(), schema: schema.trim() || "public" }
        : { username: username.trim() }),
      permissions,
      enabled,
      ...(password ? { password } : {}),
    };
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy("save");
    setError("");
    setMessage("");
    try {
      await api(
        editing
          ? `/api/instances/${encodeURIComponent(editing.name)}`
          : "/api/instances",
        json(editing ? "PUT" : "POST", entry()),
      );
      onSaved();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy("");
    }
  }
  async function test() {
    if (!form.current?.reportValidity()) return;
    setBusy("test");
    setError("");
    setMessage("");
    try {
      const result = await api<{
        ok: boolean;
        message?: string;
        error?: string;
      }>("/api/instances/test", json("POST", entry()));
      if (result.ok) setMessage(result.message || "连接成功");
      else setError(result.error || "连接失败");
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy("");
    }
  }
  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
      wide
      title={editing ? `编辑 ${editing.name}` : "添加数据库实例"}
      description="配置 PostgreSQL 或 Redis。密码只写不读，保存后对后续调用生效。"
    >
      <form ref={form} onSubmit={save} className="form-stack">
        <Feedback message={error} />
        <Feedback message={message} success />
        <fieldset disabled={!!busy} className="form-fields">
          <div className="form-section">
            <h3>连接信息</h3>
            <div className="form-grid">
              <Field
                label="实例名称"
                hint="仅字母、数字、下划线和连字符，保存后不可更名。"
              >
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  pattern="[A-Za-z0-9_-]+"
                  maxLength={100}
                  disabled={!!editing}
                  placeholder="pg-main"
                />
              </Field>
              <Field label="数据库类型">
                <select
                  className="input"
                  value={type}
                  onChange={(e) => {
                    const value = e.target.value as InstanceEntry["type"];
                    setType(value);
                    setPort(value === "postgres" ? 5432 : 6379);
                    setDatabase(value === "postgres" ? "" : "0");
                    setPassword("");
                  }}
                >
                  <option value="postgres">PostgreSQL</option>
                  <option value="redis">Redis</option>
                </select>
              </Field>
              <Field label="目标主机">
                <Input
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                  required
                  placeholder="IP 或主机名"
                />
              </Field>
              <Field label="端口">
                <Input
                  type="number"
                  min={1}
                  max={65535}
                  value={port}
                  onChange={(e) => setPort(Number(e.target.value))}
                  required
                />
              </Field>
              <Field
                label={type === "postgres" ? "数据库名称" : "数据库编号"}
                hint={type === "redis" ? "从 0 开始的非负整数。" : undefined}
              >
                <Input
                  type={type === "redis" ? "number" : "text"}
                  min={type === "redis" ? 0 : undefined}
                  step={type === "redis" ? 1 : undefined}
                  value={database}
                  onChange={(e) => setDatabase(e.target.value)}
                  required
                  placeholder={type === "postgres" ? "appdb" : "0"}
                />
              </Field>
              <Field
                label={type === "postgres" ? "连接账号" : "ACL 用户名（可选）"}
              >
                <Input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required={type === "postgres"}
                  autoComplete="off"
                  placeholder={type === "postgres" ? "reader" : "default"}
                />
              </Field>
              {type === "postgres" && (
                <Field label="默认 Schema">
                  <Input
                    value={schema}
                    onChange={(e) => setSchema(e.target.value)}
                    placeholder="public"
                  />
                </Field>
              )}
            </div>
            <Field
              label="连接密码"
              hint={
                editing?.has_password
                  ? "已配置密码；留空沿用原密码或环境变量引用。"
                  : "可使用 ${ENV_VAR} 引用服务端环境变量。"
              }
            >
              <PasswordInput
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                placeholder={editing ? "留空保留原密码" : "输入密码（可选）"}
              />
            </Field>
          </div>
          <div className="form-section">
            <h3>访问边界</h3>
            <Field
              label="工具读写权限"
              hint="只读实例拒绝写入；PostgreSQL 高危 DDL 仍需调用方显式确认。"
            >
              <select
                className="input"
                value={permissions}
                onChange={(e) =>
                  setPermissions(e.target.value as InstanceEntry["permissions"])
                }
              >
                <option value="read">只读 · read</option>
                <option value="write">读写 · write</option>
              </select>
            </Field>
            {permissions === "write" && (
              <p className="muted small">
                此实例允许 MCP 工具修改数据库内容，请确认连接账号的数据库权限。
              </p>
            )}
            <Switch
              checked={enabled}
              onCheckedChange={setEnabled}
              label="启用实例"
            />
          </div>
        </fieldset>
        <div className="dialog-actions">
          <Button type="button" onClick={() => void test()} disabled={!!busy}>
            <Cable size={16} />
            {busy === "test" ? "正在检测…" : "测试连接"}
          </Button>
          <div className="button-row">
            <Button
              type="button"
              variant="quiet"
              onClick={onClose}
              disabled={!!busy}
            >
              取消
            </Button>
            <Button type="submit" variant="primary" disabled={!!busy}>
              {busy === "save" ? "正在保存…" : "保存配置"}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
