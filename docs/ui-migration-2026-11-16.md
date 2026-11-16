# DB MCP 控制台迁移验收

日期：2026-11-16。范围为现有 `dev` 分支的本地源码、静态产物与开发文档；未提交、推送或发布镜像，未修改既有运行配置与数据。

## 实现

- React 19 + TypeScript + Vite + Ant Design 6，移除 Base UI、TanStack Table、Tailwind 与旧配色组件。
- 服务内独立持有公共组件、主题、OPPO Sans 4.0 原始 TTF 及许可。仅浅色/深色，默认浅色，使用 `db-mcp-theme`，兼容旧主题偏好。
- 56px 顶栏、220px/64px 侧栏、16px/20px 工作区边距。一级菜单没有可见重复标题，详情保留资源名与返回。
- 所有数据表格复用 AppTable，表头和正文居中、默认不吸顶，分页仅 10/20/50，默认 10；后端分页使用真实 total 和 limit/offset。
- 成员无头像，直接开关启停、确认删除；前后端保护最后一个启用成员。有其他启用成员时可自禁用/自删并退出。删除同事务撤销会话、删除账号及写入 outbox，独立 Token 和历史审计保留。
- Token 直接启停、确认删除；删除后隐藏并保留不可恢复的失效标记，一次明文与客户端接入配置保留。
- 保留 PostgreSQL/Redis 实例字段、配置热更新、密码只写及留空保留、手动连接测试、独立详情、双审计筛选/语句/目标快照/CSV。数据库驱动、工具层和 SQL 三层防护未修改。

## 自动检查

| 检查 | 结果 |
| --- | --- |
| `PYTHONPATH=src UV_CACHE_DIR=/private/tmp/db-mcp-uv-cache uv run --locked --no-sync pytest -q` | 208 passed |
| 同环境 `uv run --locked --no-sync ruff check src tests` | 通过 |
| `npm run typecheck` / `npm run build` | 通过，静态产物已更新 |
| `uv build --offline`，输出到临时目录 | wheel / sdist 构建通过 |
| 包资源核对 | wheel / sdist 包含 HTML、JS、CSS、字体原件与许可；字体哈希与原件一致 |
| `git diff --check` | 通过 |
| 独立三轴评审 | Correctness / Standards / Spec 均 0 条 |

新增身份测试覆盖自删、最后启用成员、会话撤销、Token 独立保留、审计历史、CSRF、认证隔离、outbox 失败回滚、独立 SQLite 连接并发删除/禁用及旧配置不复活已删除 Token。

复杂度与突变审计未运行：项目没有现成的对应工具链，本轮不为审计安装额外依赖。Vite 构建保留单个 JS chunk 大于 500kB 的体积提示；当前约 1.18MB，gzip 约 379kB。

## 桌面走查

使用 Ego 浏览器及独立临时目录的配置、身份库和审计库，在 `/db-mcp/` 剥前缀代理下验收。未读取真实配置、未连接实际数据库；错误反馈使用本机未监听端口，演示资源使用 `.invalid` 域名，调用历史为隔离演示数据。

- 首次初始化、登录、空态、成员创建与直接启停、Token 创建及一次明文、实际子路径接入 URL。
- 实例配置保存、失败连接反馈、密码脱敏、编辑时留空保留、独立详情。
- 六个菜单、实例详情审计的统一默认分页、表头正文居中、不吸顶、无重复标题和无整页横向溢出。
- 10/20/50 选择、第二页改变大小回第一页、详情后端 `offset=10`、筛选无结果、审计语句与数据库目标详情、CSV 完整导出。
- Token 删除确认与列表移除、实例末页删除回退、浅深主题与侧栏折叠、当前账号操作退出。
- 子路径 HTML/JS/CSS/字体加载；最终 CSS 字体引用为 `../assets/fonts/OPPO-Sans-4.0.ttf`。

演示截图：[工作区](images/workspace.png)。本地验收不代表镜像或已部署服务已更新，也不替代实际 PostgreSQL/Redis 环境的连接验收。
