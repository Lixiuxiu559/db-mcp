import { useEffect, useState } from 'react'
import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  LogoutOutlined,
  PlusOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { api, clearToken } from '../api'
import type { InstanceEntry, Me } from '../types'
import { TYPE_LABEL } from '../types'
import PageHeader from '../components/PageHeader'
import InstanceFormModal from '../components/InstanceFormModal'
import UserManager from '../components/UserManager'
import AuditPanel from '../components/AuditPanel'

const MUTED = { color: 'var(--color-text-secondary)' }

/* db-mcp 管理端：数据库实例配置管理 + 审计日志，单页应用。 */
export default function AdminHome({ me }: { me: Me }) {
  const { message } = AntApp.useApp()
  const [instances, setInstances] = useState<InstanceEntry[] | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<InstanceEntry | null>(null)

  const loadInstances = () => {
    api('/api/instances')
      .then((d) => setInstances(d.instances))
      .catch((e) => {
        message.error((e as Error).message)
        setInstances([])
      })
  }

  useEffect(() => {
    loadInstances()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const delInstance = async (name: string) => {
    try {
      await api('/api/instances/' + encodeURIComponent(name), { method: 'DELETE' })
      message.success('已删除')
      loadInstances()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const columns = [
    {
      title: '实例名',
      dataIndex: 'name',
      render: (v: string) => (
        <Tag bordered={false} color="primary" className="mono" style={{ fontSize: 12.5 }}>
          {v}
        </Tag>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 110,
      render: (t: string) => (
        <span style={MUTED}>
          <DatabaseOutlined style={{ marginRight: 6 }} />
          {TYPE_LABEL[t as 'postgres' | 'redis'] || t}
        </span>
      ),
    },
    {
      title: '连接地址',
      key: 'addr',
      render: (_: unknown, r: InstanceEntry) => (
        <span className="mono" style={{ color: 'var(--color-text-primary)' }}>
          {r.host}
          <span style={{ color: 'var(--color-text-tertiary)' }}>:{r.port}</span>
          {r.type === 'postgres' && (
            <span style={{ color: 'var(--color-text-tertiary)' }}>/{r.database}</span>
          )}
          {r.type === 'redis' && (
            <span style={{ color: 'var(--color-text-tertiary)' }}>/db{r.database}</span>
          )}
        </span>
      ),
    },
    {
      title: '账户',
      key: 'user',
      render: (_: unknown, r: InstanceEntry) => (
        <span style={MUTED}>{r.type === 'postgres' ? r.user : r.username || '—'}</span>
      ),
    },
    {
      title: '权限',
      dataIndex: 'permissions',
      width: 96,
      render: (p: string) =>
        p === 'write' ? (
          <Tag bordered={false} color="orange">
            读写
          </Tag>
        ) : (
          <Tag bordered={false} color="green">
            只读
          </Tag>
        ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 84,
      render: (enabled: boolean) =>
        enabled ? (
          <Tag bordered={false} color="blue">
            启用
          </Tag>
        ) : (
          <Tag bordered={false}>禁用</Tag>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 170,
      render: (_: unknown, r: InstanceEntry) =>
        me.admin ? (
          <Space>
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() => {
                setEditing(r)
                setModalOpen(true)
              }}
            >
              编辑
            </Button>
            <Popconfirm
              title={`确定删除实例 ${r.name} 吗？`}
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={() => delInstance(r.name)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          <Typography.Text type="secondary">仅管理员</Typography.Text>
        ),
    },
  ]

  return (
    <div className="page">
      <PageHeader
        title="db-mcp 数据库实例管理"
        subtitle="网页上改完配置，所有成员的 agent 立即可用"
        extra={
          <Space>
            <Tag icon={<UserOutlined />} bordered={false} style={{ padding: '3px 10px' }}>
              {me.username}
            </Tag>
            {me.admin && (
              <Tag color="primary" bordered={false} style={{ padding: '3px 10px' }}>
                ADMIN
              </Tag>
            )}
            <Button icon={<LogoutOutlined />} onClick={() => { clearToken(); location.reload() }}>
              退出
            </Button>
          </Space>
        }
      />

      {me.default_admin && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 'var(--space-5)' }}
          message="当前使用内置默认管理员账号 admin / admin（未配置任何成员时的引导账号）"
          description="请在下方「成员与 Token」中设置正式成员与 token；保存后该默认账号将自动失效。"
        />
      )}

      <section className="section">
        <Card
          title={
            <Space>
              <DatabaseOutlined style={{ color: 'var(--color-primary)' }} />
              数据库实例
            </Space>
          }
          extra={
            me.admin && (
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
                新增实例
              </Button>
            )
          }
        >
          <Table
            rowKey="name"
            columns={columns}
            dataSource={instances || []}
            loading={instances === null}
            pagination={false}
            scroll={{ x: 940 }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    me.admin ? '点击右上角新增第一个数据库实例' : '请联系管理员在网页端添加实例'
                  }
                />
              ),
            }}
          />
        </Card>
      </section>

      <section className="section">
        <AuditPanel />
      </section>

      <InstanceFormModal
        open={modalOpen}
        editing={editing}
        onClose={() => {
          setModalOpen(false)
          setEditing(null)
        }}
        onSaved={() => {
          setModalOpen(false)
          setEditing(null)
          loadInstances()
        }}
      />

      <section className="section">
        <UserManager />
      </section>
    </div>
  )
}
