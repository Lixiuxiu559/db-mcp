import { useId, useState } from "react";
import {
  Alert,
  Button,
  Col,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { Cable, Save } from "lucide-react";
import type { InstanceEntry } from "../types";
import { api, json } from "../api";
import { errorText } from "../lib/utils";
import { AppModal } from "./AppModal";
import { Feedback } from "./Feedback";

interface InstanceValues {
  name: string;
  type: InstanceEntry["type"];
  host: string;
  port: number;
  database: string | number;
  username: string;
  schema: string;
  password: string;
  permissions: InstanceEntry["permissions"];
  enabled: boolean;
}

export function InstanceEditor({
  editing,
  onClose,
  onSaved,
}: {
  editing: InstanceEntry | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form] = Form.useForm<InstanceValues>();
  const formId = useId();
  const type = Form.useWatch("type", form) || editing?.type || "postgres";
  const permissions = Form.useWatch("permissions", form) || editing?.permissions || "read";
  const [busy, setBusy] = useState<"save" | "test" | "">("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  function entry(values: InstanceValues) {
    return {
      name: values.name.trim(),
      type: values.type,
      host: values.host.trim(),
      port: values.port,
      database:
        values.type === "redis"
          ? Number(values.database)
          : String(values.database).trim(),
      ...(values.type === "postgres"
        ? {
            user: values.username.trim(),
            schema: values.schema?.trim() || "public",
          }
        : { username: values.username?.trim() || "" }),
      permissions: values.permissions,
      enabled: values.enabled,
      ...(values.password ? { password: values.password } : {}),
    };
  }

  async function save(values: InstanceValues) {
    if (busy) return;
    setBusy("save");
    setError("");
    setMessage("");
    try {
      await api(
        editing
          ? `/api/instances/${encodeURIComponent(editing.name)}`
          : "/api/instances",
        json(editing ? "PUT" : "POST", entry(values)),
      );
      onSaved();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy("");
    }
  }

  async function test() {
    if (busy) return;
    let values: InstanceValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setBusy("test");
    setError("");
    setMessage("");
    try {
      const result = await api<{
        ok: boolean;
        message?: string;
        error?: string;
      }>("/api/instances/test", json("POST", entry(values)));
      if (result.ok) setMessage(result.message || "连接成功");
      else setError(result.error || result.message || "连接失败");
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy("");
    }
  }

  return (
    <AppModal
      opened
      onClose={() => {
        if (!busy) onClose();
      }}
      busy={!!busy}
      size="lg"
      title={editing ? `编辑 ${editing.name}` : "添加数据库实例"}
      description="配置 PostgreSQL 或 Redis 连接，保存后对后续 MCP 调用生效。"
      footer={
        <Space>
          <Button onClick={onClose} disabled={!!busy}>
            取消
          </Button>
          <Button
            onClick={() => void test()}
            disabled={!!busy}
            loading={busy === "test"}
            icon={<Cable size={15} aria-hidden="true" />}
          >
            测试连接
          </Button>
          <Button
            type="primary"
            htmlType="submit"
            form={formId}
            disabled={!!busy}
            loading={busy === "save"}
            icon={<Save size={15} aria-hidden="true" />}
          >
            保存配置
          </Button>
        </Space>
      }
    >
      <Form<InstanceValues>
        id={formId}
        form={form}
        layout="vertical"
        disabled={!!busy}
        onFinish={save}
        scrollToFirstError={{ focus: true }}
        initialValues={{
          name: editing?.name || "",
          type: editing?.type || "postgres",
          host: editing?.host || "",
          port: editing?.port || 5432,
          database: editing?.database ?? "",
          username: editing?.user || editing?.username || "",
          schema: editing?.schema || "public",
          password: "",
          permissions: editing?.permissions || "read",
          enabled: editing?.enabled ?? true,
        }}
      >
        <Feedback message={error} />
        <Feedback message={message} success />
        <Typography.Title level={3}>连接信息</Typography.Title>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="name"
              label="实例名称"
              extra="仅字母、数字、下划线和连字符，保存后不可更名。"
              rules={[
                { required: true, message: "请输入实例名称" },
                {
                  pattern: /^[A-Za-z0-9_-]+$/,
                  message: "仅支持字母、数字、下划线和连字符",
                },
              ]}
            >
              <Input maxLength={100} disabled={!!editing || !!busy} placeholder="pg-main" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="type" label="数据库类型">
              <Select
                options={[
                  { value: "postgres", label: "PostgreSQL" },
                  { value: "redis", label: "Redis" },
                ]}
                onChange={(value: InstanceEntry["type"]) => {
                  form.setFieldsValue({
                    port: value === "postgres" ? 5432 : 6379,
                    database: value === "postgres" ? "" : 0,
                    password: "",
                  });
                  setError("");
                  setMessage("");
                }}
              />
            </Form.Item>
          </Col>
          <Col span={16}>
            <Form.Item
              name="host"
              label="目标主机"
              rules={[{ required: true, whitespace: true, message: "请输入目标主机" }]}
            >
              <Input placeholder="IP 或主机名" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item
              name="port"
              label="端口"
              rules={[{ required: true, type: "integer", min: 1, max: 65535, message: "请输入 1–65535 之间的整数端口" }]}
            >
              <InputNumber min={1} max={65535} precision={0} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="database"
              label={type === "postgres" ? "数据库名称" : "数据库编号"}
              extra={type === "redis" ? "从 0 开始的非负整数。" : undefined}
              rules={type === "redis"
                ? [{ required: true, type: "integer", min: 0, message: "请输入非负整数数据库编号" }]
                : [{ required: true, whitespace: true, message: "请输入数据库名称" }]}
            >
              {type === "redis" ? (
                <InputNumber min={0} precision={0} placeholder="0" style={{ width: "100%" }} />
              ) : (
                <Input placeholder="appdb" />
              )}
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="username"
              label={type === "postgres" ? "连接账号" : "ACL 用户名（可选）"}
              rules={type === "postgres"
                ? [{ required: true, whitespace: true, message: "请输入连接账号" }]
                : []}
            >
              <Input autoComplete="off" placeholder={type === "postgres" ? "reader" : "default"} />
            </Form.Item>
          </Col>
          {type === "postgres" && (
            <Col span={12}>
              <Form.Item name="schema" label="默认 Schema">
                <Input placeholder="public" />
              </Form.Item>
            </Col>
          )}
          <Col span={type === "postgres" ? 12 : 24}>
            <Form.Item
              name="password"
              label="连接密码"
              extra={editing
                ? `${editing.has_password ? "已配置密码；" : ""}留空沿用原密码或环境变量引用。`
                : "密码只写不读；可使用 ${ENV_VAR} 引用服务端环境变量。"}
            >
              <Input.Password autoComplete="new-password" placeholder={editing ? "留空保留原密码" : "输入密码（可选）"} />
            </Form.Item>
          </Col>
        </Row>
        <Typography.Title level={3}>访问边界</Typography.Title>
        <Form.Item name="permissions" label="工具读写权限">
          <Select options={[
            { value: "read", label: "只读 · read — 允许读取，拒绝写入" },
            { value: "write", label: "读写 · write — 允许读取和修改数据库内容" },
          ]} />
        </Form.Item>
        <Alert
          showIcon
          type={permissions === "write" ? "warning" : "info"}
          title={permissions === "write" ? "此实例允许 MCP 工具修改数据库内容，请确认连接账号的数据库权限。" : "只读实例拒绝写入，新增实例默认使用只读权限。"}
          description="PostgreSQL 只读查询仍由数据库只读事务保护；高危 DDL 仍需调用方显式确认。"
          style={{ marginBottom: 16 }}
        />
        <Form.Item name="enabled" label="实例状态" valuePropName="checked" extra="停用后，MCP 工具将拒绝使用此实例。">
          <Switch checkedChildren="启用" unCheckedChildren="停用" aria-label="启用实例" />
        </Form.Item>
      </Form>
    </AppModal>
  );
}
