# db-mcp — 团队共享数据库网关 MCP 服务

基于 [FastMCP](https://gofastmcp.com) 的团队级数据库网关：PostgreSQL / Redis 实例与
密码集中托管，成员的 Claude Code / Codex 只需配置一个 URL 和个人 token，对话里直接说
「查一下 pg-main 的 users 表」即可，无需再关心地址、账户、密码。

核心安全设计：**每个实例按 `read` / `write` 分级**。只读实例上一切写操作都会被拒绝
（三层防护：工具层权限校验 + SQL 静态分析 + 数据库只读事务兜底），高危 DDL
（DROP/TRUNCATE/ALTER）还需 `confirm=true` 二次确认。

另提供**浏览器管理页**（token 登录）：管理员可在网页上增删改实例、一键测试连接、
管理成员 token、查看审计日志，保存后所有成员立即可用（无需重启）。

## 功能

| MCP 工具 | 说明 |
|---|---|
| `list_instances` | 列出实例清单（名称/类型/权限/启用状态/连接信息，不含密码） |
| `db_ping` | 检测某实例连通性，返回版本与延迟 |
| `postgres_query` | 只读查询（SELECT/WITH/SHOW/EXPLAIN/TABLE/VALUES），结果以 Markdown 表格返回 |
| `postgres_execute` | 写操作（INSERT/UPDATE/DELETE/DDL），仅 `write` 实例；高危 DDL 需 `confirm=true` |
| `postgres_list_schemas` / `postgres_list_tables` | 浏览 schema 与表/视图 |
| `postgres_describe_table` / `postgres_list_indexes` | 查看表结构与索引 |
| `redis_get` / `redis_mget` / `redis_type` / `redis_ttl` / `redis_exists` | Redis string 读取与元信息 |
| `redis_scan` / `redis_keys` | key 遍历（推荐 SCAN，KEYS 仅适合小实例） |
| `redis_hash_get` / `redis_list_range` / `redis_set_members` / `redis_zset_range` | hash / list / set / zset 读取 |
| `redis_set` / `redis_del` / `redis_expire` / `redis_persist` | string 写入与过期管理 |
| `redis_hash_set` / `redis_hash_del` | hash 写入 |
| `redis_list_push` / `redis_list_pop` | list 写入 |
| `redis_set_add` / `redis_set_remove` | set 写入 |
| `redis_zset_add` / `redis_zset_remove` | zset 写入 |

安全策略：**只读实例拦截写 + 高危 DDL 二次确认 + 全量审计**。每次工具调用写入 SQLite
审计库，**记录 SQL / Redis 命令明文**（超长截断，默认保留 30 天），便于事后回溯
「谁在什么时候执行了什么」；密码等凭据绝不入库。

### 只读实例是怎么被拦住的

不要只靠 `sql.upper().startswith("SELECT")` 判定只读——那会被这些绕过：
`WITH x AS (DELETE ... RETURNING *) SELECT ...`（data-modifying CTE）、
`SELECT 1; DROP TABLE t`（多语句）、`SELECT ... INTO new_table`、
`SELECT ... FOR SHARE`、`SELECT pg_read_file('/etc/passwd')`。

本服务做三层防护：

1. **工具层**：`read` 实例收到写工具/写语句直接拒绝，给出可读提示
2. **SQL 静态分析**（`sqlguard.py`）：先做词法扫描（正确剥离注释、字符串、
   美元引号、引号标识符），把 SQL 归一成 token 再分类；拒绝多语句、写关键字、
   危险函数、`SELECT INTO`、`FOR SHARE`。`EXPLAIN ANALYZE DELETE ...` 会按内层语句判定
3. **数据库只读事务**（`db/pg.py`）：`read` 实例的查询一律跑在 `BEGIN READ ONLY` 里，
   即便 1/2 漏判，PostgreSQL 自己会拒绝写入

## 部署（网关机）

网关机需满足：能直连所有目标数据库，安装 [uv](https://docs.astral.sh/uv/)。

```bash
cd db-mcp   # 在工作空间中先 cd mcps/db-mcp
uv sync                              # 安装依赖（自动创建虚拟环境）

cp config.example.yaml config.yaml   # 编辑实例清单与用户 token
cp .env.example .env                 # 填写各成员 token、数据库密码，然后 source .env
chmod 600 config.yaml                # 配置里可能含明文密码，收紧权限

uv run db-mcp -c config.yaml         # 启动，默认监听 0.0.0.0:8807
```

生产环境建议用 systemd 常驻（`Restart=always`），并给 `audit/` 目录配置轮转。

## Docker 部署

镜像已包含前端构建产物，配置 / 审计数据通过挂载卷持久化。

### 从镜像仓库部署（推荐）

> **多项目共存**：一个项目一套。镜像仓库共用 `base/db-mcp`（靠 tag 区分版本），
> 但容器名、部署目录、nginx 子路径**都要带项目前缀**，否则多套服务会互相覆盖。
> 下面以 `<项目>` 作占位，例如 example-project 项目就是 `example-project-db-mcp`、
> `/opt/example-project-db-mcp/`、`/example-project-db-mcp/`。

```bash
mkdir -p /opt/<项目>-db-mcp/audit
cd /opt/<项目>-db-mcp
# 把源码仓库里的 config.example.yaml 复制成这里的 config.yaml 并编辑实例清单；
# 首次也可留空 auth.users —— 启动后会启用内置引导账号 admin/admin
```

`/opt/<项目>-db-mcp/docker-compose.yml`：

```yaml
services:
  <项目>-db-mcp:
    image: docker.io/lijx559/db-mcp:2.0.1
    container_name: <项目>-db-mcp
    restart: unless-stopped
    ports:
      - "127.0.0.1:8807:8807"
    # 整个目录挂载到 /data：网页改配置、审计落库都写这里
    volumes:
      - /opt/<项目>-db-mcp:/data
    # 以 uid 1000 运行（镜像内 appuser），目录属主需对齐
    user: "1000:1000"
    environment:
      TZ: Asia/Shanghai
      # 数据库密码用环境变量注入（配置里以 ${VAR} 引用）
      DBMCP_PG_MAIN_PASSWORD: ${DBMCP_PG_MAIN_PASSWORD}
```

```bash
chown -R 1000:1000 /opt/<项目>-db-mcp && chmod 600 /opt/<项目>-db-mcp/config.yaml
docker compose up -d
docker compose ps           # 确认 healthy
```

访问 `http://<网关机>:8807/`；经网关 nginx 子路径则是
`https://mcp.example.com/<项目>-db-mcp/`（子路径统一约定见工作空间根 CLAUDE.md）。

### 本地构建（开发 / 离线场景）

```bash
cd db-mcp   # 在工作空间中先 cd mcps/db-mcp
DOCKER_UID=$(id -u) DOCKER_GID=$(id -g) docker compose up -d --build
```

### 常用命令

```bash
docker compose logs -f      # 看日志
docker compose restart      # 重启（数据都在挂载卷里）
docker compose down         # 停止
```

### Docker 部署要点

- **必须挂「目录」而非单个文件**。配置保存走原子写（先写 `.tmp` 再 rename），
  单文件 bind mount 会让临时文件与目标落在不同文件系统，rename 必然失败
- **容器内工作目录是 `/data`**，`config.yaml` 里的相对路径（`audit/audit.db`）
  都落在挂载卷内
- **`${ENV_VAR}` 引用的变量必须在容器环境里存在**，否则管理端保存配置会因校验
  失败而报错（这是刻意设计的 fail-fast）。在 compose 的 `environment:` 里补齐
- 容器以非 root 运行，`/healthz` 已配置为镜像 HEALTHCHECK
- 目标数据库不在容器可达网络时，用 compose 的 `extra_hosts` 或自定义 network 处理

### 配置要点

- **实例权限**（每个实例独立配置）：`permissions: read`（只读，拒绝一切写）或
  `permissions: write`（读写，高危 DDL 仍需 `confirm=true`）。**默认 read**
- **`${ENV_VAR}` 插值**：密码/token 建议引用环境变量；缺变量启动即报错；
  `$$` 表示字面 `$`
- **`enabled: false`** 可临时禁用某实例（配置保留，agent 无法使用）
- **用户与 token**：`auth.users` 中每项含 `username`、`token`、`admin`（管理员）。
  未配置任何成员时，服务会启用**内置默认管理员 `admin` / `admin`** 作为引导账号
  （启动日志有醒目提示），登录后请在管理页「成员与 Token」里改成正式成员——
  一旦配置了成员，默认账号自动失效。成员的增删改在管理页保存后**即时热生效**

### 浏览器管理页

打开 `https://mcp.example.com/<项目>-db-mcp/`（如 example-project 项目是
`https://mcp.example.com/example-project-db-mcp/`）——经网关 nginx 子路径；
内网直连则是 `http://<网关机>:8807/`。粘贴个人 token 登录（**仅 admin 可进入**）：

- 实例表格：新增 / 编辑 / 删除；按类型（PostgreSQL / Redis）自动切换字段；
  编辑时密码留空 = 保持原密码；「测试连接」用表单配置建一次性连接验证，
  保存前就能确认密码正确
- 权限级别在表单里选「只读 / 读写」，新建实例默认只读
- 「成员与 Token」：增删成员、查看与重置 token、切换 admin 标志，保存即热生效
- 审计记录：按实例/状态/时间/关键词筛选，点行看完整语句，可导出 CSV

## 成员接入

MCP 端点 URL 二选一：**经网关（推荐）**
`https://mcp.example.com/<项目>-db-mcp/mcp`（如 `.../example-project-db-mcp/mcp`），
或内网直连 `http://<网关机>:8807/mcp`。URL 写法约定见工作空间根 CLAUDE.md
「网关子路径统一约定」。

### Claude Code

项目根目录 `.mcp.json`（或 `claude mcp add` 命令）：

```json
{
  "mcpServers": {
    "db": {
      "type": "http",
      "url": "https://mcp.example.com/example-project-db-mcp/mcp",
      "headers": { "Authorization": "Bearer ${DBMCP_TOKEN}" }
    }
  }
}
```

把个人 token 放进环境变量 `DBMCP_TOKEN`，避免提交进 git。配置格式以
[官方文档](https://docs.claude.com/en/docs/claude-code/mcp) 为准。

### Codex

`~/.codex/config.toml`（字段名以官方文档为准）：

```toml
[mcp_servers.db]
url = "https://mcp.example.com/example-project-db-mcp/mcp"
http_headers = { "Authorization" = "Bearer <个人token>" }
```

### 使用示例

> pg-main 里有哪些表？
>
> 查一下 users 表里 id=42 的记录
>
> redis-cache 里 user:123 是什么值？还有多久过期？
>
> 在 pg-ops 上把 users 表里 status='stale' 的记录删掉

## 开发

```bash
uv sync
uv run pytest            # 单元测试（Fake asyncpg 池 + Fake Redis）
uv run ruff check .

# 前端
cd webui && npm install
cd webui && npm run build       # 构建产物直接输出到 src/db_mcp/web/static/
cd webui && npx tsc --noEmit    # 类型检查（改了 .tsx 必跑）
```

## 已知边界

- `postgres_query` / `postgres_execute` 每次只执行**单条**语句（多语句会被拒绝），
  请拆成多次调用
- SQL 静态分析是词法级的，无法判定存储过程内部的运行期行为；只读实例靠
  数据库只读事务兜底，`write` 实例的高危操作请自行确认影响范围
- 查询结果受 `limits.max_rows` / `max_result_chars` 约束（默认 1000 行 / 50000 字符），
  超限会截断并提示收窄条件
- `redis_keys`（KEYS 命令）在大数据集上会阻塞 Redis，生产实例优先用 `redis_scan`
- 审计为本地 SQLite 库（默认保留 30 天，`audit.retention_days` 可调），
  记录语句明文（`audit.max_statement_chars` 控制单条上限），如需防篡改请外接日志采集
- 配置文件由网页保存时会重写整个 YAML（注释会丢失）；`${ENV}` 引用会原样保留
