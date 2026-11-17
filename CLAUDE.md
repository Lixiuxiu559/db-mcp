# db-mcp 开发指引

独立 PostgreSQL / Redis MCP 服务仓库。遵循工作空间最新统一架构和 Ant Design 6 控制台规范，运行、测试、打包不能依赖父目录或兄弟服务。
当前版本 2.1.0，Python FastMCP 精确 4.1.4。

## Git 分支

日常开发使用已有 `dev` 分支，`main` 保持稳定；创建其他分支需用户允许，合并、推送与发布按用户授权执行。

## 架构

- `cli.py`：配置初始化、路径与监听覆盖、HTTP 启动。
- `config.py`：Pydantic、原始 YAML 原子保存、环境变量插值、实例判别联合。
- `auth/`：SQLite 人员、scrypt 密码、可撤销会话、独立 token、工作空间与身份事件 outbox；公开 TokenVerifier 每请求验证。
- `state.py`：配置热更新、连接池失效、身份事件投递和 30 天审计清理生命周期。
- `server.py`：FastMCP 认证、lifespan、中间件、工具与管理 API 组装。
- `audit.py`：独立人员/MCP/尝试审计、开始与终态、实际目标快照、旧表迁移。
- `sqlguard.py` / `db/pg.py` / `db/pool.py`：SQL 分类、数据库只读事务、流式查询、连接池与重试。
- `tools/`：inventory、postgres、redis。`web/api.py`：Cookie + CSRF 管理 API，不复用 MCP token。
- `webui/`：React 19 + TypeScript + Vite + Ant Design 6 + Lucide；构建产物 `src/db_mcp/web/static/`。

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
    MCP token 保存哈希与加密副本，列表仅返回元数据；人员会话加 CSRF 保护的独立接口可按需复制，并记录脱敏审计。
    审计库只记语句明文，**绝不记密码**（有测试断言审计文件里不含密码字节）。

11. **`${ENV_VAR}` 在保存时保留原文、在校验时展开**。
    `ConfigFile.raw` 存引用原文，绝不落盘明文；但每次 `load()` 都 `expand_env_vars`
    校验，**缺变量直接失败**（fail-fast）。所以容器里保存配置时，引用到的变量
    必须在容器环境中存在。

12. **配置文件写入是"原子写"（写 `.tmp` 再 `os.replace`）**。
    因此容器部署**必须挂目录、不能挂单个文件**——单文件 bind mount 会让临时文件与
    目标落在不同文件系统，rename 必然失败。

13. **网页人员与 MCP token 完全分离**：用户名密码登录 + 可撤销会话 Cookie +
    CSRF 校验，所有启用人员同权；人员与 token 独立管理。首次启动且身份库无用户时自动创建
    `admin / admin`，无需 setup.key；已有用户时不补建、不重置。普通新建、修改和重置密码要求
    6–1024 个字符，默认初始密码为唯一长度例外，前后端校验一致。禁止 token 登录网页、
    将 token 存 localStorage。MCP token 必须归属单个工作空间，资源可以多选归属。

14. **前端资源必须相对路径**（`vite base: './'` + api 客户端 `apiUrl()` 去前导 `/`）。
    网关 nginx 按 `location /db-mcp/ { proxy_pass .../; }` 剥前缀，任何指向根路径的
    绝对引用（`/assets/...`、`/api/...`）都会在子路径下 404（ssh-mcp 白屏事故根因）。

15. **SQLite 审计连接是惰性的**（`_connection()` 在 close 后自动重开）。
    不要假设连接常驻——测试里 lifespan 会被多次进入，连接可能已被 close。

16. **redis-py `decode_responses=True`**，所以返回值是 `str` 而非 `bytes`。
    但 `info()` 在 redis-py 5/6 返回 awaitable，`inventory.py` 里做了兼容判断。

## 工作空间授权

- 后台已登录人员统一管理所有项目、资源、token 和审计。
- 每个数据库实例只创建一次，workspace_ids 支持多个工作空间，默认空数组不授权；每枚 token 必须绑定单个 workspace_id，签发后固定。
- 每次 MCP 调用基于当前服务端 token 与归属检查权限，覆盖枚举、直接调用、批量操作、等待及重试。已建立会话和缓存连接不能绕过撤权。错误候选名仅包含当前空间可见资源。
- 有资源或未删除 token 引用的空间禁止删除。空间改名保留 ID，删除重建使用新 ID。调用审计保留工作空间 ID 与名称快照，归属变更记人员审计。
- 全新部署不建立兼容工作空间，不推断存量 token 归属。资源与身份数据由服务独立持久化。

## 新增功能要求

新增工具必须通过统一 FastMCP 注册路径和审计中间件，保留可读领域错误与实际目标追溯。
新增或调整页面统一使用 Ant Design 6 原生组件、ConfigProvider 与浅深主题算法，主题参数集中在 `webui/src/theme.ts`，布局集中在 `global.css`，页面读取 `--ant-*` 语义变量。字体采用服务内 OPPO Sans 4.0 原始 TTF，保留许可；命令、配置与秘密值使用系统等宽字体。
应用壳、主题 Provider、会话/导航、登录账号、总览、成员/Token 功能页和审计工具栏来自 MCP 工作空间的 `design-system/react/` 权威源，服务内持有独立副本。工作空间开发时先改公共源，再运行 `node design-system/scripts/sync-ui.mjs --write` 和 `--check`；本服务只适配品牌、导航、实例字段与数据库领域交互。独立克隆本服务可直接构建，不需要父目录。
跨顶部品牌栏高 56px，侧栏展开 220px、折叠 64px，工作区上下 16px、左右 20px。一级菜单不重复显示菜单名称，`PageHeading` 仅保留屏幕阅读器 h1，操作放入内容面板工具栏；详情页保留资源名与返回。
所有表格统一经过 `AppTable`（`DataTable` 也复用它），表头、正文与多行容器居中，默认不吸顶。分页仅 10/20/50、默认 10，改变大小回第一页；服务端分页使用 `useListPagination`，筛选变化回第一页、删除后处理越界。
成员列表不显示头像，Switch 直接启停，删除须确认；最后一个启用成员前后端保护，其他启用成员存在时可自禁用/自删并退出。禁用、删除撤销网页会话，删除成员保留独立 MCP Token 与历史审计；身份变更和 outbox 同事务。Token 列表开关直接保存，删除后永久失效并隐藏，保留 tombstone 与审计；列表提供复制按钮，按需解密且记审计；接入提供 Claude Code JSON 与 Codex TOML。
主题仅 `light/dark`，默认浅色，顶栏为唯一切换入口；服务专属存储键为 `db-mcp-theme`，兼容旧 `mantine-color-scheme-value` / `linkage-theme` 并归一为浅深，不清会话或凭据。不提供配色模板或自动跟随系统。账号菜单仅身份、个人资料、修改密码与退出；网关状态在侧栏底部，连接测试明确为手动检测。
服务独立持有组件、字体许可、依赖锁和构建产物，不在运行时引用父目录或兄弟服务。网页 UI 仅验收桌面、浏览器缩放、键盘、浅深模式及网关子路径。包含总览、实例列表/独立详情、登录、账号、成员/Token 和双审计；本地构建不代表已发布镜像更新。

审计默认30天，身份存储不受审计清理影响。稳定token身份与目标快照必须留存；旧token升级继续可用，显式停用/吊销才拒绝新请求。
身份修改与outbox同事务，资源配置副作用记录开始和结果；不得记录密码/token/会话秘密或把跨文件操作写成原子事务。

所有新服务/交付维持源码uv、Docker/Compose、wheel/sdist+uvx三种方式，包含完整后台与MCP HTTP。运行数据使用持久化可写路径，不进Git、镜像、包缓存。

## 验证与发布

人员审计路由必须显式声明 `PersonAuditMode.OUTBOX` 或 `LIFECYCLE`，不从 action 前缀推断。身份操作使用服务内 `web/person_audit.py` 的 `call_outbox`；已有异步 State 操作使用 `call_outbox_async`。handler 保留业务摘要，不直接传 event 或设置 outbox 标记。注册缺少模式、辅助函数误用及成功漏登记由契约检查拒绝；取消不能推断事务回滚，不补写与迟到事务矛盾的事件。Store 事务、State 投递与审计存储机制继续各自负责，保留 DB 的取消检查点。

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
既有版本标签不可被新产物覆盖。工作空间功能按全新部署验收，发布前检查初始化、空间授权和审计持久化，存量开发数据使用副本验证。

## 身份字段与 Token 到期

- 用户名、显示名和 Token 名称上限128个Unicode字符，Token备注上限1000；前后端按code point计数，普通密码仍为6–1024，初始admin/admin例外。
- Token默认长期有效，可显式创建/修改未来到期时间或清除期限；UTC持久化，到期等号立即拒绝后续认证、调用及等待/重试后的新尝试。已到期仍保留独立身份、历史与后台复制能力，复制不能恢复调用权限。
- 旧凭证增加空到期字段后继续长期有效；已删除凭证不能通过改期限复活。到期拒绝审计可识别原Token，但识别结果不授予工作空间权限。
