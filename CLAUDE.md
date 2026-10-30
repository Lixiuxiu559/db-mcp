# db-mcp 开发指引

独立 PostgreSQL / Redis MCP 服务仓库。遵循工作空间最新统一架构和 Linkage 1.0 规范，运行、测试、打包不能依赖父目录或兄弟服务。
当前版本 2.1.0，Python FastMCP 精确 4.1.4。

## 架构

- `cli.py`：配置初始化、路径与监听覆盖、HTTP 启动。
- `config.py`：Pydantic、原始 YAML 原子保存、环境变量插值、实例判别联合。
- `auth/`：SQLite 人员、scrypt 密码、可撤销会话、独立 token、幂等旧凭证迁移、身份事件 outbox；公开 TokenVerifier 每请求验证。
- `state.py`：配置热更新、连接池失效、身份事件投递和 30 天审计清理生命周期。
- `server.py`：FastMCP 认证、lifespan、中间件、工具与管理 API 组装。
- `audit.py`：独立人员/MCP/尝试审计、开始与终态、实际目标快照、旧表迁移。
- `sqlguard.py` / `db/pg.py` / `db/pool.py`：SQL 分类、数据库只读事务、流式查询、连接池与重试。
- `tools/`：inventory、postgres、redis。`web/api.py`：Cookie + CSRF 管理 API，不复用 MCP token。
- `webui/`：React19 TS Vite Tailwind4 BaseUI shadcn源码 TanStack Lucide；构建产物 `src/db_mcp/web/static/`。

MCP 请求路径：认证 → 审计开始 → 工具权限 → SQL 静态分析 → 只读事务/连接池 → 审计终态。
配置写入后刷新内存并失效相关连接池；SQL防护、参数限制、危险操作确认不因网页同权而改变。

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
    MCP token 仅创建时一次展示；其他接口只能返回元数据。
    审计库只记语句明文，**绝不记密码**（有测试断言审计文件里不含密码字节）。

11. **`${ENV_VAR}` 在保存时保留原文、在校验时展开**。
    `ConfigFile.raw` 存引用原文，绝不落盘明文；但每次 `load()` 都 `expand_env_vars`
    校验，**缺变量直接失败**（fail-fast）。所以容器里保存配置时，引用到的变量
    必须在容器环境中存在。

12. **配置文件写入是"原子写"（写 `.tmp` 再 `os.replace`）**。
    因此容器部署**必须挂目录、不能挂单个文件**——单文件 bind mount 会让临时文件与
    目标落在不同文件系统，rename 必然失败。

13. **网页人员与 MCP token 完全分离**：用户名密码登录 + 可撤销会话 Cookie +
    CSRF 校验，所有启用人员同权；人员与 token 独立管理。禁止默认账号、token 登录网页、
    将 token 存 localStorage。旧 auth.users 仅用于幂等迁移 MCP token，不恢复旧角色模型。

14. **前端资源必须相对路径**（`vite base: './'` + api 客户端 `apiUrl()` 去前导 `/`）。
    网关 nginx 按 `location /db-mcp/ { proxy_pass .../; }` 剥前缀，任何指向根路径的
    绝对引用（`/assets/...`、`/api/...`）都会在子路径下 404（ssh-mcp 白屏事故根因）。

15. **SQLite 审计连接是惰性的**（`_connection()` 在 close 后自动重开）。
    不要假设连接常驻——测试里 lifespan 会被多次进入，连接可能已被 close。

16. **redis-py `decode_responses=True`**，所以返回值是 `str` 而非 `bytes`。
    但 `info()` 在 redis-py 5/6 返回 awaitable，`inventory.py` 里做了兼容判断。

## 新增功能要求

新增工具必须通过统一 FastMCP 注册路径和审计中间件，保留可读领域错误与实际目标追溯。
新增页面遵循 Linkage 1.0：服务内持有 tokens.css、字体和组件源码，石墨窄导航、紧凑表格、语义状态，禁止 Ant Design 与玻璃布局。
三态主题 `light/dark/auto`，存储键 `linkage-theme`，实际主题写 `data-theme`；相对 API/资源路径、键盘操作、390px 窄屏必须验证。

审计默认30天，身份存储不受审计清理影响。稳定token身份与目标快照必须留存；旧token升级继续可用，显式停用/吊销才拒绝新请求。
身份修改与outbox同事务，资源配置副作用记录开始和结果；不得记录密码/token/会话秘密或把跨文件操作写成原子事务。

所有新服务/交付维持源码uv、Docker/Compose、wheel/sdist+uvx三种方式，包含完整后台与MCP HTTP。运行数据使用持久化可写路径，不进Git、镜像、包缓存。

## 验证与发布

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check src tests
cd webui
npm ci
npm run build
```

改前端必须重建静态产物并入库。测试fake必须对齐真实asyncpg与redis行为，不能使用宽松mock掩盖真实API错误。
回归要覆盖只读防护、write实例SELECT返回数据、token持久/撤销、Cookie与MCP认证隔离、同权、双审计及子路径。
升级FastMCP必须精确pin并提交uv.lock，按分支回归/部署版本/备份数据回滚，不在启动时自动更新。
发布镜像标签只增不覆盖，目标x86_64必须构建linux/amd64；本地验证不代表远端发布，不自动push。
已发布旧版2.0.1不可被新标签覆盖。本次2.1.0发布前先备份并演练旧auth.users和audit_records迁移。
