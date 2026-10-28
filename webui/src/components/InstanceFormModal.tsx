import { useEffect, useState } from 'react'
import {
  App as AntApp,
  Alert,
  Button,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Switch,
  Tooltip,
} from 'antd'
import {
  CheckOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { api } from '../api'
import { DEFAULT_PORT, type InstanceEntry, type InstanceType } from '../types'

const TYPE_OPTIONS = [
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'redis', label: 'Redis' },
]

/* 表单分组标题：给长表单建立视觉层次 */
function SectionTitle({ icon, title, desc }: { icon: React.ReactNode; title: string; desc?: string }) {
  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 'var(--text-body)' }}>
        <span style={{ color: 'var(--color-primary)', display: 'inline-flex' }}>{icon}</span>
        {title}
      </div>
      {desc && (
        <div style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--text-small)', marginTop: 2 }}>
          {desc}
        </div>
      )}
    </div>
  )
}

/* 新增/编辑数据库实例弹窗。编辑时密码留空 = 保持原密码不变。 */
export default function InstanceFormModal({
  open,
  editing,
  onClose,
  onSaved,
}: {
  open: boolean
  editing: InstanceEntry | null
  onClose: () => void
  onSaved: () => void
}) {
  const { message } = AntApp.useApp()
  const [form] = Form.useForm()
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    if (!open) return
    setTestResult(null)
    form.resetFields()
    form.setFieldsValue(
      editing
        ? {
            name: editing.name,
            type: editing.type,
            host: editing.host,
            port: editing.port,
            database: editing.database,
            user: editing.user,
            username: editing.username,
            schema: editing.schema,
            permissions: editing.permissions,
            enabled: editing.enabled,
          }
        : { type: 'postgres', port: DEFAULT_PORT.postgres, permissions: 'read', enabled: true },
    )
    // 每次打开都回到表单顶部（弹窗内容区滚动位置不会随开关自动重置）
    requestAnimationFrame(() => {
      const body = document.querySelector<HTMLElement>('.ant-modal .ant-modal-body')
      if (body) body.scrollTop = 0
    })
  }, [open, editing, form])

  const buildEntry = (vals: any) => {
    const entry: any = {
      name: vals.name?.trim(),
      type: vals.type,
      host: vals.host?.trim(),
      port: vals.port,
      permissions: vals.permissions || 'read',
      enabled: vals.enabled !== false,
    }
    if (vals.type === 'postgres') {
      entry.database = vals.database?.trim()
      entry.user = vals.user?.trim()
      if (vals.schema?.trim()) entry.schema = vals.schema.trim()
    } else {
      entry.database = vals.database ?? 0
      if (vals.username?.trim()) entry.username = vals.username.trim()
    }
    // 密码留空 = 编辑时保留原值（后端据此沿用已保存的密码）
    if (vals.password) entry.password = vals.password
    return entry
  }

  /* 校验失败时滚动并聚焦到第一个无效字段 */
  const validateOrFocusFirst = async (fields?: string[]) => {
    try {
      await form.validateFields(fields)
      return true
    } catch (err) {
      const errorFields = (err as { errorFields?: { name: (string | number)[] }[] })?.errorFields
      const firstName = errorFields?.[0]?.name
      if (firstName) {
        form.scrollToField(firstName, { behavior: 'smooth', block: 'center' })
        setTimeout(() => {
          const el = document.querySelector<HTMLElement>(
            '.ant-modal .ant-form-item-has-error input, .ant-modal .ant-form-item-has-error textarea',
          )
          el?.focus()
        }, 120)
      }
      return false
    }
  }

  const handleTest = async () => {
    if (!(await validateOrFocusFirst())) return
    const entry = buildEntry(form.getFieldsValue(true))
    setTesting(true)
    setTestResult(null)
    try {
      const r = await api('/api/instances/test', { method: 'POST', body: JSON.stringify(entry) })
      setTestResult(
        r.ok ? { ok: true, text: r.message || '连接成功' } : { ok: false, text: r.error || '连接失败' },
      )
    } catch (e) {
      setTestResult({ ok: false, text: (e as Error).message })
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async () => {
    if (!(await validateOrFocusFirst())) return
    const entry = buildEntry(form.getFieldsValue(true))
    setSaving(true)
    try {
      if (editing) {
        await api('/api/instances/' + encodeURIComponent(editing.name), {
          method: 'PUT',
          body: JSON.stringify(entry),
        })
      } else {
        await api('/api/instances', { method: 'POST', body: JSON.stringify(entry) })
      }
      message.success('已保存，所有成员立即可用')
      onSaved()
    } catch (e) {
      setTestResult({ ok: false, text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  /* 弹窗底部固定操作区：测试结果紧邻按钮，点完立刻可见 */
  const footer = (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-4)' }}>
      <div style={{ flex: 1, minWidth: 0, textAlign: 'left' }} role="status" aria-live="polite">
        {testResult && (
          <Alert
            type={testResult.ok ? 'success' : 'error'}
            showIcon
            message={testResult.text}
            style={{ padding: '4px 12px', fontSize: 'var(--text-small)' }}
          />
        )}
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flex: 'none' }}>
        <Tooltip title="用当前填写的配置新建一次性连接验证，不会保存">
          <Button icon={<ThunderboltOutlined />} loading={testing} onClick={handleTest}>
            测试连接
          </Button>
        </Tooltip>
        <Button onClick={onClose}>取消</Button>
        <Button type="primary" icon={<CheckOutlined />} loading={saving} onClick={handleSave}>
          保存
        </Button>
      </div>
    </div>
  )

  return (
    <Modal
      open={open}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <DatabaseOutlined style={{ color: 'var(--color-primary)' }} />
          {editing ? '编辑 ' + editing.name : '新增数据库实例'}
        </span>
      }
      width={640}
      centered
      styles={{ body: { maxHeight: 'calc(100dvh - 260px)', overflowY: 'auto', paddingTop: 'var(--space-5)' } }}
      onCancel={onClose}
      maskClosable={false}
      footer={footer}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark
        scrollToFirstError={{ behavior: 'smooth', block: 'center' }}
        style={{ marginBottom: 0 }}
      >
        {/* ── 基本信息 ── */}
        <div>
          <SectionTitle icon={<DatabaseOutlined />} title="基本信息" />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 var(--space-4)' }}>
            <Form.Item
              label="实例名 name"
              name="name"
              rules={[
                { required: true, message: '请填写实例名' },
                { pattern: /^[\w-]+$/, message: '只能包含字母、数字、下划线、连字符' },
              ]}
              extra="agent 用它指定数据库"
            >
              <Input disabled={!!editing} placeholder="如 pg-main" />
            </Form.Item>
            <Form.Item label="数据库类型" name="type" rules={[{ required: true }]}>
              <Select
                options={TYPE_OPTIONS}
                disabled={!!editing}
                onChange={(t: InstanceType) => {
                  // 切换类型时端口跟着换成该类型默认值
                  form.setFieldValue('port', DEFAULT_PORT[t])
                }}
              />
            </Form.Item>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '0 var(--space-4)' }}>
            <Form.Item
              label="主机地址"
              name="host"
              rules={[{ required: true, message: '请填写主机地址' }]}
            >
              <Input placeholder="IP 或主机名" />
            </Form.Item>
            <Form.Item label="端口" name="port" rules={[{ required: true, message: '请填写端口' }]}>
              <InputNumber min={1} max={65535} style={{ width: '100%' }} />
            </Form.Item>
          </div>
        </div>

        <Divider style={{ margin: 'var(--space-2) 0 var(--space-5)' }} />

        {/* ── 连接凭据（按类型切换字段）── */}
        <div>
          <SectionTitle
            icon={<LockOutlined />}
            title="连接凭据"
            desc="密码只写不读，保存后不会在页面上回显"
          />
          <Form.Item noStyle shouldUpdate={(p, c) => p.type !== c.type}>
            {({ getFieldValue }) => {
              const type = getFieldValue('type') as InstanceType
              return type === 'postgres' ? (
                <>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 var(--space-4)' }}>
                    <Form.Item
                      label="数据库名"
                      name="database"
                      rules={[{ required: true, message: '请填写数据库名' }]}
                    >
                      <Input placeholder="如 appdb" />
                    </Form.Item>
                    <Form.Item
                      label="登录用户"
                      name="user"
                      rules={[{ required: true, message: '请填写登录用户' }]}
                    >
                      <Input placeholder="如 app_readonly" />
                    </Form.Item>
                  </div>
                  <Form.Item
                    label="密码"
                    name="password"
                    rules={editing ? [] : [{ required: false }]}
                    extra={editing ? '留空 = 保持原密码不变' : '支持 ${ENV_VAR} 引用环境变量'}
                  >
                    <Input.Password autoComplete="new-password" placeholder={editing ? '留空则不变' : ''} />
                  </Form.Item>
                  <Form.Item
                    label="默认 schema"
                    name="schema"
                    extra="list_tables / describe_table 不指定 schema 时用它，默认 public"
                  >
                    <Input placeholder="public" />
                  </Form.Item>
                </>
              ) : (
                <>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 var(--space-4)' }}>
                    <Form.Item
                      label="ACL 用户名"
                      name="username"
                      extra="Redis 6+ ACL，未启用则留空"
                    >
                      <Input placeholder="可选" />
                    </Form.Item>
                    <Form.Item
                      label="逻辑库编号"
                      name="database"
                      rules={[{ required: true, message: '请填写逻辑库编号' }]}
                      extra="0-15"
                    >
                      <InputNumber min={0} max={15} style={{ width: '100%' }} />
                    </Form.Item>
                  </div>
                  <Form.Item
                    label="密码"
                    name="password"
                    extra={editing ? '留空 = 保持原密码不变' : '支持 ${ENV_VAR} 引用环境变量'}
                  >
                    <Input.Password autoComplete="new-password" placeholder={editing ? '留空则不变' : ''} />
                  </Form.Item>
                </>
              )
            }}
          </Form.Item>
        </div>

        <Divider style={{ margin: 'var(--space-2) 0 var(--space-5)' }} />

        {/* ── 权限 ── */}
        <div>
          <SectionTitle
            icon={<SafetyCertificateOutlined />}
            title="访问权限"
            desc="决定 agent 能用这个实例做什么"
          />
          <Form.Item
            label={
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                权限级别
                <Tooltip title="只读：仅允许 SELECT/SHOW 等查询，一切写操作会被拒绝。读写：允许 INSERT/UPDATE/DELETE/DDL（高危 DDL 还需 confirm 确认）。">
                  <InfoCircleOutlined style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }} />
                </Tooltip>
              </span>
            }
            name="permissions"
            rules={[{ required: true }]}
          >
            <Radio.Group
              options={[
                { value: 'read', label: '只读（推荐）' },
                { value: 'write', label: '读写' },
              ]}
              optionType="button"
            />
          </Form.Item>
          <Form.Item
            label="启用"
            name="enabled"
            valuePropName="checked"
            extra="禁用后 agent 无法使用该实例，配置保留"
          >
            <Switch />
          </Form.Item>
        </div>
      </Form>
    </Modal>
  )
}
