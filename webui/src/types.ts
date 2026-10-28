/* 领域类型：db-mcp API 契约 */

export type InstanceType = 'postgres' | 'redis'
export type Permissions = 'read' | 'write'

export interface InstanceEntry {
  name: string
  type: InstanceType
  host: string
  port: number
  permissions: Permissions
  enabled: boolean
  has_password: boolean
  // postgres
  database?: string
  user?: string
  schema?: string
  // redis
  username?: string
}

export interface AuditRecord {
  ts: string
  user: string
  tool: string
  instance?: string
  kind?: 'sql' | 'redis' | 'meta' | string
  statement?: string
  statement_truncated?: number
  row_count?: number
  status: 'ok' | string
  duration_ms: number
  request_id?: string
  error?: string
}

export interface Me {
  username: string
  admin: boolean
  default_admin?: boolean
}

export const TYPE_LABEL: Record<InstanceType, string> = {
  postgres: 'PostgreSQL',
  redis: 'Redis',
}

/* 各类型的表单字段（决定新增/编辑弹窗展示哪些输入项） */
export const TYPE_FIELDS: Record<InstanceType, string[]> = {
  postgres: ['host', 'port', 'database', 'user', 'password', 'schema'],
  redis: ['host', 'port', 'username', 'password', 'database'],
}

export const DEFAULT_PORT: Record<InstanceType, number> = {
  postgres: 5432,
  redis: 6379,
}
