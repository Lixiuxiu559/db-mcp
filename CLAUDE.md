# db-mcp — 开发指引

团队共享数据库网关 MCP 服务（PostgreSQL / Redis）。本文件记录**架构地图**与
**实测踩出来的非显然约束**，改代码前请先读「关键不变量」一节。功能与部署说明见 `README.md`。

## 架构地图

```
cli.py            入口：load_dotenv → ConfigFile → create_mcp → mcp.run(transport="http")
server.py         组装：StaticTokenVerifier 认证 + lifespan + AuditMiddleware + 注册工具/路由
config.py         pydantic 配置模型、YAML 原始树读写（原子写）、${ENV} 展开、实例判别联合
state.py          DbState：内存配置 + ConnectionPool + AuditLogger（配置热更新入口）
sqlguard.py       SQL 词法分析与安全判定（只读/写/高危/多语句/危险函数）——本服务的核心安全模块
audit.py          SQLite 审计（中间件 + 查询 + 30 天保留），记录语句明文
errors.py         领域异常（DbMcpError 及其子类）
dependencies.py   get_state（工具通过 Depends 拿状态）
db/pool.py        连接池：按实例缓存 asyncpg 池 / redis 客户端、断线重试、并发限流
db/pg.py          PostgreSQL 执行封装：只读事务 + 流式游标截断 + 错误翻译
models.py         工具返回值的 pydantic 模型
tools/            inventory / postgres / redis（各含 register(mcp)）
web/api.py        管理端 API + 静态资源路由（与 MCP 共用一套 token 认证）
web/static/       webui 构建产物（入库，部署零额外步骤）
webui/            React + TS + AntD6 前端源码；vite build → ../src/db_mcp/web/static/
```

请求流：`MCP/HTTP → 认证 → AuditMiddleware（无论如何都记一条）→ 工具 → 权限校验 →
sqlguard → ConnectionPool → asyncpg/redis`

配置热更新：管理端写配置 → `ConfigFile.save()` 原子写回 → `state.upsert_instance()` →
参数变化时 `pool.invalidate()` → 工具下次调用即生效（无需重启）。

## 关键不变量（改代码前必读）

1. **三层防护缺一不可**，只靠字符串前缀匹配判定只读是不安全的。
   `WITH x AS (DELETE ... RETURNING *) SELECT ...`、`SELECT 1; DROP TABLE t`、
   `SELECT ... INTO t`、`SELECT ... FOR SHARE`、`SELECT pg_read_file(...)` 都能绕过
   朴素的前缀检查。工具层（权限）+ `sqlguard.py`（词法分析）+ 数据库只读事务
   （`db/pg.py` 的 `conn.transaction(readonly=True)`）三层叠加才可靠。
   **只读事务是最后一道防线**，不要为了"性能"去掉它。

2. **sqlguard 必须剥离注释/字符串/美元引号/引号标识符后再判关键字**。
   `SELECT 'DROP TABLE t'` 不是写语句；`/* DROP */ SELECT 1` 也不是。
   反之，字符串里的内容不能当关键字（会误报），真实关键字也不能漏（会漏防）。

3. **`AS` 之后的 token 是别名，不是关键字**（`_scan_keywords` 专门处理）。
   `SELECT x AS comment` 里的 COMMENT 是列别名——不排除它，一条正常只读查询会被
   误判成 DDL（设计 `pg_describe_table` 的 `AS comment` 时实测踩到）。

4. **`EXPLAIN ANALYZE <写语句>` 会真的执行写语句**，必须按内层语句判定，
   不能看到 EXPLAIN 就放行（`_strip_explain`）。

5. **asyncpg 游标用 `async for` 迭代，不是 `async with` / `fetch(n)`**。
   `conn.cursor(...)` 返回 `CursorFactory`，只实现了 `__aiter__`。
   曾用 `async with conn.cursor(...) as cur: await cur.fetch(n)` 写，**fake 层比真实宽松
   掩盖了这个 bug，直到连真库才炸**（`TypeError: 'CursorFactory' object does not
   support the asynchronous context manager protocol`）。改测试 mock 时要问：
   **真实 asyncpg 会怎样？**（这条与 ssh-mcp 的不变量 2 同类）

6. **列名只能从 `prepare()` 的 `get_attributes()` 拿**。
   结果是空集时无从从行里取键，所以先 `await conn.prepare(sql)` 取列名，
   再用游标流式取行（`Cursor` 不暴露列名）。

7. **只读查询用流式游标只取 `max_rows + 1` 行**，多取的 1 行用于判断
   "是否被截断"。不要 `fetch` 全量——`SELECT * FROM 大表` 会把整表读进内存。

7b. **`read_only` 参数表示"这条 SQL 走只读执行路径"，与实例权限无关**。
   `_execute_read` 必须恒传 `read_only=True`。曾错写成
   `read_only=inst.permissions == "read"`，后果是 **write 实例上的所有查询返回空数据**
   （`row_count` 有值但 `columns`/`rows` 为空）——因为 write 实例的 SELECT 被当写语句
   走了 `execute()` 路径，只拿命令状态串，行数据全丢。线上实测才发现（read 实例正常、
   write 实例全空，很容易被误当成"库里没数据"）。回归测试：
   `test_postgres_query_on_write_instance_returns_data`。
   两层含义别混：**实例权限**决定"能不能写"（`_require_write` 校验），
   **执行路径**决定"怎么取结果"（读路径取行、写路径取状态串）。

8. **`mask_error_details=True`，所以所有面向用户的失败都必须 raise 领域异常**
   （`DbMcpError` 子类）。否则底层异常（asyncpg/redis/OSError）会被 FastMCP
   mask 成无用的 `Error calling tool 'xxx'`。已覆盖：实例不存在/已禁用、
   权限不足、只读事务被拒、多语句、危险函数、超时、连接失败。

9. **只读事务被数据库拒绝时要翻译成可读提示**（`db/pg.py` 的 `_translate_error`）。
   PostgreSQL 原文是 `cannot execute UPDATE in a read-only transaction`，
   要让 LLM 看懂"该实例是只读的、请换 write 实例"。

10. **密码只写不读**：`instances` CRUD 永不返回密码内容，只给 `has_password` 布尔。
    成员 token 是例外——管理页可查看（便于运维重置）。
    审计库只记语句明文，**绝不记密码**（有测试断言审计文件里不含密码字节）。

11. **`${ENV_VAR}` 在保存时保留原文、在校验时展开**。
    `ConfigFile.raw` 存引用原文，绝不落盘明文；但每次 `load()` 都 `expand_env_vars`
    校验，**缺变量直接失败**（fail-fast）。所以容器里保存配置时，引用到的变量
    必须在容器环境中存在。

12. **配置文件写入是"原子写"（写 `.tmp` 再 `os.replace`）**。
    因此容器部署**必须挂目录、不能挂单个文件**——单文件 bind mount 会让临时文件与
    目标落在不同文件系统，rename 必然失败。

13. **权限模型**：管理页仅 `admin` 可进；`instances`/`audit` 的 GET 只需认证；
    写操作 + 成员管理 + 配置导出要求 `admin`。`auth.users` 为空时自动启用内置
    引导账号 `admin/admin`（`/api/me` 返回 `default_admin` 标记），配置了成员即失效。

14. **前端资源必须相对路径**（`vite base: './'` + api 客户端 `apiUrl()` 去前导 `/`）。
    网关 nginx 按 `location /db-mcp/ { proxy_pass .../; }` 剥前缀，任何指向根路径的
    绝对引用（`/assets/...`、`/api/...`）都会在子路径下 404（ssh-mcp 白屏事故根因）。

15. **SQLite 审计连接是惰性的**（`_connection()` 在 close 后自动重开）。
    不要假设连接常驻——测试里 lifespan 会被多次进入，连接可能已被 close。

16. **redis-py `decode_responses=True`**，所以返回值是 `str` 而非 `bytes`。
    但 `info()` 在 redis-py 5/6 返回 awaitable，`inventory.py` 里做了兼容判断。

## 开发命令

```bash
uv sync                                # 安装依赖
uv run pytest                          # 测试（Fake asyncpg 池 + Fake Redis）
uv run ruff check src tests            # lint
uv run db-mcp -c config.yaml           # 本地启动（默认 0.0.0.0:8807）

# 前端
cd webui && npm install
cd webui && npm run build              # 构建产物直接输出到 src/db_mcp/web/static/
cd webui && npx tsc --noEmit           # 类型检查（改了 .tsx 必跑）

# 镜像：见下方「发布镜像」
```

**提交前**：`uv run pytest` 全绿 + `ruff check` 干净 + 改了前端要 `tsc --noEmit`。

## 发布镜像（ACR）

镜像仓库：`docker.io/lijx559/db-mcp`

### 1. 前置：改了前端先构建

镜像构建**不会**跑 npm——`src/db_mcp/web/static/` 是入库的构建产物，Dockerfile 直接
COPY 它。**改了 `webui/` 就必须先 `cd webui && npm run build`**，否则镜像里的前端还是旧的。

### 2. 登录（首次或凭据过期）

```bash
docker login docker.io
# 用户名/密码来自阿里云 ACR 控制台，不是阿里云账号密码
```

### 3. 构建并推送（一条命令）

```bash
cd db-mcp   # 在工作空间中先 cd mcps/db-mcp
docker buildx build --platform linux/amd64 \
  -t docker.io/lijx559/db-mcp:2.0.1 \
  --push .
```

`--push` 构建完直接推送，无需再 `docker push`。
（想先在本地跑一遍验证，可用 `--load` 代替 `--push`，验证无误后再推。）

### 4. 验证推送结果

```bash
docker buildx imagetools inspect \
  docker.io/lijx559/db-mcp:2.0.1 \
  --format '{{json .Image}}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['os'], d['architecture'])"
# 必须输出：linux amd64
```

### 关键注意（与 ssh-mcp 同源，都是踩过的坑）

1. **必须 `--platform linux/amd64`**。目标机是 x86_64；在 Apple Silicon 上直接
   `docker build` 产出 arm64 镜像，推上去在目标机报 `exec format error`。
2. **基础镜像必须同时提供目标架构**。Dockerfile 的 `ARG BASE_IMAGE` 默认
   `docker.m.daocloud.io/library/python:3.12-slim`（amd64 + arm64 都有）。
3. **ACR 端点用公网（不带 `-vpc`）**。`-vpc` 只能在阿里云 VPC 内拉取。
4. **版本号只增不覆盖**。compose 靠 tag 判断是否更新；覆盖已发布 tag 会导致目标机
   拉到缓存旧镜像。改代码 → 递增版本（2.0.0 → 2.0.1）。

## 测试约定

- `tests/conftest.py` 提供 `FakePgPool`/`FakePgConn`/`FakeCursorFactory`/`FakeRedis`/
  `Harness`，monkeypatch `asyncpg.create_pool` 与 `redis.asyncio.Redis`。
- **fake 必须对齐真实驱动行为**：`FakePgConn.prepare` 是 async（真实是 async）；
  `cursor()` 返回异步可迭代对象（真实是 `CursorFactory`，无 `async with`）；
  只读事务里执行写语句会抛 `asyncpg.PostgresError`（真实行为）。
  比真实宽松的 mock 会掩盖 bug（不变量 5）。
- `call(gw, "tool_name", **args)` 是调用工具的统一入口（in-memory Client）。
- 新增错误路径时，**断言错误信息可读**（含关键词），而不是只断言 `is_error`。
- 安全相关的测试集中在 `test_sqlguard.py`（分类逻辑）与 `test_tools.py`
  （read 实例拒绝写、DDL confirm、只读事务兜底、危险函数）。

## 已知边界

- 每条 SQL 只执行单条语句（多语句被拒），请拆成多次调用
- SQL 静态分析是词法级的，无法判定存储过程内部行为；只读实例靠只读事务兜底
- 查询结果受 `limits.max_rows` / `max_result_chars` 截断
- `redis_keys`（KEYS）在大数据集上阻塞 Redis，优先用 `redis_scan`
- 网页保存配置会重写整个 YAML（**注释丢失**），`${ENV}` 引用原样保留
- 审计默认保留 30 天（`audit.retention_days`），记录语句明文（`max_statement_chars` 截断）

## 版本记录

- `0.1.x` 内部开发版本（未发布）
- `2.0.0` 首个发布版本，**完全重写**：取代 ACR 上原有的 TS 版 `base/db-mcp:1.x`
  （Express + pg + ioredis → Python + FastMCP）。配置格式与工具集都不兼容，
  不是 1.x 的增量升级，故直接跳到 2.0.0。能力：PostgreSQL / Redis 多实例、
  按实例 read/write 分级、三层只读防护、高危 DDL 二次确认、完整管理端、语句明文审计
- `2.0.1` 修复：**write 实例上的查询返回空数据**（不变量 7b）——`read_only` 被错绑到
  实例权限，导致 write 实例的 SELECT 走了写路径、丢失行列数据

> **多项目共存**：一个项目部署一套。ACR 仓库共用 `base/db-mcp`（靠 tag 区分版本），
> 但**容器名与 nginx 子路径必须带项目前缀**，否则多套服务会互相覆盖。
> 例如 example-project 项目：容器 `example-project-db-mcp`、子路径 `/example-project-db-mcp/`、
> 部署目录 `/opt/example-project-db-mcp/`。
