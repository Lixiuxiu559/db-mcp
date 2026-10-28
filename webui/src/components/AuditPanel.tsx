import { useCallback, useEffect, useState } from 'react'
import {
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownloadOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { api, apiUrl, getToken } from '../api'
import type { AuditRecord, InstanceEntry } from '../types'

const PAGE_SIZE = 20
const TIME_RANGES = [
  { value: 0, label: '全部' },
  { value: 1, label: '24 小时内' },
  { value: 7, label: '7 天内' },
  { value: 30, label: '30 天内' },
]
const MUTED = { color: 'var(--color-text-secondary)' }

const KIND_LABEL: Record<string, string> = {
  sql: 'SQL',
  redis: 'Redis',
  meta: '元数据',
}

const pad = (n: number) => String(n).padStart(2, '0')

/* 服务的 ts 是 UTC ISO 串：转本地时区显示准确时间（精确到秒） */
function localFull(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts || ''
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

type Filters = { q: string; instance?: string; status?: string; days: number }

/* 审计面板：筛选 + 分页 + 详情抽屉 + CSV 导出。 */
export default function AuditPanel() {
  const { message } = AntApp.useApp()
  const [records, setRecords] = useState<AuditRecord[] | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [detail, setDetail] = useState<AuditRecord | null>(null)
  const [instances, setInstances] = useState<string[]>([])

  const [search, setSearch] = useState('') // 输入框即时值
  const [filters, setFilters] = useState<Filters>({ q: '', days: 0 }) // 实际生效的筛选（搜索防抖后同步）

  /* 搜索防抖：停止输入 300ms 后再请求 */
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((f) => (f.q === search ? f : { ...f, q: search }))
      setPage(1)
    }, 300)
    return () => clearTimeout(t)
  }, [search])

  const buildQuery = useCallback((f: Filters, pageNo: number) => {
    const p = new URLSearchParams()
    if (f.q) p.set('q', f.q)
    if (f.instance) p.set('instance', f.instance)
    if (f.status) p.set('status', f.status)
    if (f.days) p.set('days', String(f.days))
    p.set('limit', String(PAGE_SIZE))
    p.set('offset', String((pageNo - 1) * PAGE_SIZE))
    return p.toString()
  }, [])

  const load = useCallback(
    (f: Filters, pageNo: number) => {
      setLoading(true)
      api(`/api/audit?${buildQuery(f, pageNo)}`)
        .then((d) => {
          setRecords(d.records as AuditRecord[])
          setTotal(d.total as number)
        })
        .catch((e) => {
          message.error((e as Error).message)
          setRecords([])
        })
        .finally(() => setLoading(false))
    },
    [buildQuery, message],
  )

  // 筛选或页码变化时重新拉取
  useEffect(() => {
    load(filters, page)
  }, [filters, page, load])

  useEffect(() => {
    api('/api/instances')
      .then((d) => setInstances((d.instances as InstanceEntry[]).map((s) => s.name)))
      .catch(() => setInstances([]))
  }, [])

  const patch = (part: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...part }))
    setPage(1)
  }

  /* CSV 导出：带 Authorization 头请求，转 blob 触发下载 */
  const exportCsv = async () => {
    setExporting(true)
    try {
      const p = new URLSearchParams()
      if (filters.q) p.set('q', filters.q)
      if (filters.instance) p.set('instance', filters.instance)
      if (filters.status) p.set('status', filters.status)
      if (filters.days) p.set('days', String(filters.days))
      const resp = await fetch(apiUrl(`/api/audit/export?${p.toString()}`), {
        headers: { Authorization: 'Bearer ' + getToken() },
      })
      if (!resp.ok) throw new Error(`导出失败（HTTP ${resp.status}）`)
      const url = URL.createObjectURL(await resp.blob())
      const a = document.createElement('a')
      a.href = url
      a.download = `audit-${localFull(new Date().toISOString()).replace(/[-: ]/g, '')}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      message.success('已导出当前筛选条件下的记录')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setExporting(false)
    }
  }

  const columns = [
    {
      title: '时间',
      dataIndex: 'ts',
      width: 172,
      render: (v: string) => (
        <span className="mono" style={{ ...MUTED, fontSize: 'var(--text-small)', whiteSpace: 'nowrap' }}>
          {localFull(v)}
        </span>
      ),
    },
    { title: '用户', dataIndex: 'user', width: 82 },
    {
      title: '工具',
      dataIndex: 'tool',
      width: 148,
      render: (v: string, r: AuditRecord) => (
        <span className="mono" style={{ whiteSpace: 'nowrap', fontSize: 'var(--text-small)' }}>
          {KIND_LABEL[r.kind || ''] ? `${KIND_LABEL[r.kind || '']} ` : ''}
          {v}
        </span>
      ),
    },
    {
      title: '实例',
      dataIndex: 'instance',
      width: 106,
      render: (v: string) => (
        <span className="mono" style={{ fontSize: 'var(--text-small)' }}>
          {v || '—'}
        </span>
      ),
    },
    {
      title: '语句 / 命令',
      key: 'stmt',
      ellipsis: true,
      render: (_: unknown, r: AuditRecord) => (
        <div style={{ minWidth: 0 }}>
          {r.statement ? (
            <code className="mono" style={{ fontSize: 'var(--text-small)' }}>
              {r.statement}
            </code>
          ) : (
            <span style={MUTED}>—</span>
          )}
          {/* 失败原因直接内联展示，无需点开详情即可定位 */}
          {r.status !== 'ok' && r.error && (
            <div
              style={{
                color: 'var(--color-error)',
                fontSize: 'var(--text-caption)',
                marginTop: 2,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={r.error}
            >
              {r.error}
            </div>
          )}
        </div>
      ),
    },
    {
      title: '行数',
      dataIndex: 'row_count',
      width: 76,
      render: (v: number | undefined) => (
        <span className="mono" style={{ ...MUTED, fontSize: 'var(--text-small)' }}>
          {v === null || v === undefined ? '—' : v}
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 84,
      render: (s: string) =>
        s === 'ok' ? (
          <Tag bordered={false} color="green" icon={<CheckCircleOutlined />}>
            成功
          </Tag>
        ) : (
          <Tag bordered={false} color="red" icon={<CloseCircleOutlined />}>
            失败
          </Tag>
        ),
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      width: 82,
      render: (v: number) => (
        <span className="mono" style={{ ...MUTED, fontSize: 'var(--text-small)' }}>
          {v}ms
        </span>
      ),
    },
  ]

  const hasFilter = !!(filters.q || filters.instance || filters.status || filters.days)

  return (
    <>
      <Card
        title={
          <Space>
            <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
            最近审计记录
          </Space>
        }
        extra={
          <Space>
            <Typography.Text type="secondary" style={{ fontSize: 'var(--text-caption)' }}>
              仅保留最近 30 天
            </Typography.Text>
            <Button icon={<DownloadOutlined />} onClick={exportCsv} loading={exporting}>
              导出 CSV
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => load(filters, page)}>
              刷新
            </Button>
          </Space>
        }
      >
        {/* 筛选栏 */}
        <div
          style={{
            display: 'flex',
            gap: 'var(--space-3)',
            flexWrap: 'wrap',
            alignItems: 'center',
            marginBottom: 'var(--space-4)',
          }}
        >
          <Input
            prefix={<SearchOutlined style={{ color: 'var(--color-text-tertiary)' }} />}
            placeholder="搜索语句 / 用户 / 工具"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 260 }}
          />
          <Select
            allowClear
            placeholder="全部实例"
            value={filters.instance}
            onChange={(v) => patch({ instance: v || undefined })}
            options={instances.map((s) => ({ value: s, label: s }))}
            style={{ width: 150 }}
          />
          <Segmented
            value={filters.status ?? 'all'}
            onChange={(v) => patch({ status: v === 'all' ? undefined : (v as string) })}
            options={[
              { value: 'all', label: '全部' },
              { value: 'ok', label: '成功' },
              { value: 'error', label: '失败' },
            ]}
          />
          <Select
            value={filters.days}
            onChange={(v) => patch({ days: v })}
            options={TIME_RANGES}
            style={{ width: 130 }}
          />
          {hasFilter && (
            <Button
              type="link"
              size="small"
              onClick={() => {
                setSearch('')
                setFilters({ q: '', days: 0 })
                setPage(1)
              }}
            >
              清除筛选
            </Button>
          )}
        </div>

        <Table
          rowKey={(_, i) => String(i)}
          columns={columns}
          dataSource={records || []}
          loading={loading}
          size="small"
          onRow={(r) => ({
            onClick: () => setDetail(r),
            style: { cursor: 'pointer' },
          })}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p) => setPage(p),
          }}
          scroll={{ x: 980 }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  hasFilter
                    ? '没有符合筛选条件的记录，试试放宽条件'
                    : '暂无审计记录，成员通过 agent 执行 SQL/Redis 命令后会在这里留下记录'
                }
              />
            ),
          }}
        />
      </Card>

      {/* 记录详情：完整语句、失败原因、请求 ID */}
      <Drawer
        title={
          detail && (
            <Space>
              {detail.status === 'ok' ? (
                <CheckCircleOutlined style={{ color: 'var(--color-success)' }} />
              ) : (
                <CloseCircleOutlined style={{ color: 'var(--color-error)' }} />
              )}
              <span className="mono">{detail.tool}</span>
            </Space>
          )
        }
        open={!!detail}
        onClose={() => setDetail(null)}
        width={560}
      >
        {detail && (
          <Descriptions column={1} size="small" styles={{ label: { color: 'var(--color-text-tertiary)', width: 120 } }}>
            <Descriptions.Item label="时间">{localFull(detail.ts)}</Descriptions.Item>
            <Descriptions.Item label="用户">{detail.user}</Descriptions.Item>
            <Descriptions.Item label="工具">
              <span className="mono">{detail.tool}</span>
            </Descriptions.Item>
            <Descriptions.Item label="实例">
              <span className="mono">{detail.instance || '—'}</span>
            </Descriptions.Item>
            <Descriptions.Item label="类别">{KIND_LABEL[detail.kind || ''] || '—'}</Descriptions.Item>
            <Descriptions.Item label="状态">
              {detail.status === 'ok' ? (
                <Tag bordered={false} color="green">
                  成功
                </Tag>
              ) : (
                <Tag bordered={false} color="red">
                  失败
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="耗时">{detail.duration_ms}ms</Descriptions.Item>
            {detail.row_count !== undefined && detail.row_count !== null && (
              <Descriptions.Item label="影响/返回行数">{detail.row_count}</Descriptions.Item>
            )}
            {detail.statement && (
              <Descriptions.Item label="语句 / 命令">
                <code className="mono" style={{ wordBreak: 'break-all', whiteSpace: 'pre-wrap' }}>
                  {detail.statement}
                </code>
                {detail.statement_truncated ? (
                  <div style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--text-caption)', marginTop: 4 }}>
                    语句过长，已截断记录
                  </div>
                ) : null}
              </Descriptions.Item>
            )}
            {detail.error && (
              <Descriptions.Item label="失败原因">
                <Typography.Text type="danger" style={{ fontSize: 'var(--text-small)' }}>
                  {detail.error}
                </Typography.Text>
              </Descriptions.Item>
            )}
            {detail.request_id && (
              <Descriptions.Item label="请求 ID">
                <span className="mono">{detail.request_id}</span>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="记录说明">
              <Typography.Text type="secondary" style={{ fontSize: 'var(--text-caption)' }}>
                <ThunderboltOutlined style={{ marginRight: 4 }} />
                记录语句明文（超长截断），不含数据库密码
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        )}
      </Drawer>
    </>
  )
}
