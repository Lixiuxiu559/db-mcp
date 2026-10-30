# db-mcp — 数据库链路工作台

基于 Python FastMCP 的独立 PostgreSQL / Redis 网关。2.1.0 管理端采用 Linkage 1.0 统一设计，网页人员、MCP token 和双审计独立管理。

## 本地启动

需要 Python 3.10+ 与 uv，默认只监听本机 127.0.0.1:8807。

```bash
uv sync --locked
uv run --locked db-mcp --init --config ./runtime/config.yaml
uv run --locked db-mcp --config ./runtime/config.yaml
```

打开 http://127.0.0.1:8807/。首次启动从终端提示的本机 `setup.key` 文件读取一次性初始化码，设置用户名与密码；没有默认登录账号。
`--init` 仅创建最小空配置，不覆盖已有文件。可选 `--host`、`--port`、`--data-dir`，后者显式覆盖身份和审计数据位置，不自动迁移数据。

## 人员与 MCP token

- 网页以用户名和密码登录，所有启用人员同权；提供用户创建、显示名修改、密码重置与启停。不能停用最后一个可登录人员。
- 密码采用带盐 scrypt 哈希，网页使用可撤销的 HttpOnly Cookie 会话（默认 8 小时）及写操作 CSRF 校验。账号停用、密码变化撤销其会话。
- MCP token 在独立页面创建、命名、备注、启停、吊销，查看创建人和最近调用时间，生成客户端接入配置。新 token 只展示一次明文；服务仅保存验证哈希。
- MCP token 仅用于 `/mcp`，不能登录网页；网页登录会话不能调用 MCP。
- 重启、升级和容器重建保留 token，创建新 token 不自动使旧 token 失效。人员停用也不自动吊销其创建的 token，需独立处理。
- 数据库实例的 `read` / `write` 权限与高危 SQL 确认继续生效，与网页人员同权相互独立。

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

## 三种交付方式

每种方式都启动完整 HTTP MCP 服务和管理页面。

### 源码

使用上面的 `uv sync --locked` 与 `uv run --locked db-mcp`。已有部署继续指定原配置文件路径，不能用空配置替换真实配置。

### Docker / Compose

公开镜像：[lijx559/db-mcp](https://hub.docker.com/r/lijx559/db-mcp)，固定版本 `2.1.0` 和 `latest` 均支持 `linux/amd64` / `linux/arm64`，拉取时自动选择架构。
镜像内已包含管理页面，不要求运行环境安装 Node；Compose 使用 `lijx559/db-mcp:2.1.0`。

```bash
mkdir -p runtime
export DOCKER_UID=$(id -u)
export DOCKER_GID=$(id -g)
docker compose pull
# 仅首次部署初始化；已有配置升级时跳过此行
docker compose run --rm db-mcp db-mcp --init --config /data/config.yaml
docker compose up -d
docker compose logs -f
```

Compose 把 `runtime/` 整体挂载到 `/data`，默认仅宿主回环地址暴露 8807。
必须挂目录，配置通过临时文件原子替换；人员/token、审计和配置要放在同一个持久化部署目录中。
容器内监听 `0.0.0.0`，以非 root 用户运行，健康接口 `/healthz`。
配置用到的 `${ENV_VAR}` 必须显式加入 Compose `environment`，缺变量启动即失败。
`build: .` 保留为源码构建选项；本地修改后可用 `docker compose up -d --build`。生产部署优先使用固定版本，升级前备份 `runtime/`。

### Python 包 / uvx

```bash
uv build
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --init --config /absolute/path/db-mcp/config.yaml
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --config /absolute/path/db-mcp/config.yaml
```

也可 `uv tool install` 安装 wheel。wheel/sdist 内含默认配置、管理页与字体，运行数据必须使用外部可写路径，不能写到包或 uvx 缓存目录。
uvx 解析包依赖，不使用源码 `uv.lock`；生产完整复现优先源码锁文件或版本化镜像。当前不宣称新版本已发布 PyPI 或 GitHub Release。

## 配置与路径

`config.example.yaml` 展示 PostgreSQL、Redis、read/write 与环境变量引用。新安装建议先 `--init` 得到无真实资源的配置。

| 数据 | 默认相对配置文件父目录的位置 |
|---|---|
| 人员、会话、token | `data/service.db` |
| 双审计 SQLite | `audit/audit.db` |
| 一次性初始化码 | `data/setup.key`（终端会提示） |

配置中的相对持久化路径以配置目录为基准；CLI `--data-dir` 相对启动工作目录解析。SQLite、初始化码、配置与凭据均不入 Git 或镜像。
密码只写不读；编辑或测试实例时密码留空保留已有值。保存 YAML 保留 `${ENV_VAR}` 原文但会丢失注释，`$$` 表示字面 `$`。
`GET /api/config-raw` 导出脱敏副本，移除旧 token 并遮蔽数据库密码，不能直接当作完整秘密备份。

网关子路径示例：

```nginx
location /项目-db-mcp/ {
    proxy_pass http://127.0.0.1:8807/;
    proxy_http_version 1.1;
    proxy_buffering off;
}
```

前端资源和 API 使用相对路径。HTTPS 设置 `auth.cookie_secure: true`，子路径建议 `auth.cookie_path: /项目-db-mcp/`。
多个项目分别配置容器名、端口、部署目录和网关子路径，不能共享身份或审计目录。

## MCP 接入

在后台 MCP Token 页创建凭证，配置客户端 HTTP 端点与认证头：

```json
{
  "mcpServers": {
    "db-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8807/mcp",
      "headers": {"Authorization": "Bearer <MCP_TOKEN>"}
    }
  }
}
```

已部署服务的用户只需接入 URL，无需自己另启一份服务。

## 双审计

人员操作审计记录登录与失败、退出、人员与 token 变更、实例增删改、连接测试、导出等；身份变更与审计 outbox 在同一 SQLite 事务提交，再幂等投递到审计库。
YAML/外部副作用用同一操作 ID 记录开始与结果，不宣称跨数据库与配置文件的全局原子性。

MCP 调用审计记录稳定 token 身份、工具、实际实例类型/主机/端口/数据库/连接账号快照、SQL 或 Redis 操作、行数、请求编号、可观测来源 IP 与结果。
外部执行前记录 running，完成后记录成功/错误/超时/取消；重启发现残留执行标为 unknown。重连尝试另行关联，不能承诺外部写入只发生一次。
旧 `audit_records` 保留迁移为 legacy，不伪造无法证实的人员/token 归属。

两类审计默认保留 30 天，支持分别筛选、分页、详情与 CSV 导出。密码/token/会话秘密不入审计，不保存完整查询结果。
SQL 与 Redis 参数可能包含业务敏感值，已识别凭据脱敏与长度截断不能自动识别任意业务秘密。SQLite 是本地追溯存储，不保证防篡改。

## 从旧版本迁移

1. 停止旧实例，备份原配置、环境变量及审计和身份数据库；存在 WAL 时先正常停止或使用 SQLite 备份。
2. 在副本演练新版本启动。显式配置的旧 `auth.users` token 会幂等导入独立身份库，保留 MCP 使用；网页使用新的用户名和密码。
3. 用本机初始化码创建首位网页登录人员，检查实例列表、read/write 防护、旧 token、新会话、双审计与子路径部署。
4. 已吊销凭证不会因重启重新导入。不得删除唯一身份库或用 `--init` 覆盖配置来修复登录。
5. 空配置不生成旧的 `admin/admin` token。若旧配置显式包含该 token，仍按用户保留旧 token 的要求迁移，部署人员可单独吊销。
6. 发布时使用新的版本标签；回滚需恢复匹配的配置和数据备份，不能只回滚镜像后假定迁移兼容。

## 开发验证

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check src tests
cd webui
npm ci
npm run build
```

FastMCP 精确固定 `4.1.4`，升级在分支上更新声明与锁文件并回归，禁止启动时自动升级。
管理界面使用 React 19、TypeScript、Tailwind v4、Base UI 和 TanStack Table，构建输出到 `src/db_mcp/web/static/` 并随服务交付。
