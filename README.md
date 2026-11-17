<div align="center">

# DB MCP

**让 AI 连接数据库，也让每次数据操作有迹可循。**

[![Docker Hub](https://img.shields.io/badge/Docker_Hub-published_2.1.0-146ED0?logo=docker&logoColor=white)](https://hub.docker.com/r/lijx559/db-mcp)
[![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-4B5FC4)](docs/releases/2026-10-30-dockerhub.md)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.1.4-17242B)](https://gofastmcp.com)

**简体中文** · [English](README.en.md)

[产品能力](#features) · [快速开始](#quickstart) · [Docker 部署](#deploy) · [MCP 接入](#connect) · [运维](#operations)

</div>

面向 PostgreSQL 与 Redis 的自托管 MCP 网关。集中管理数据库连接，按工作空间隔离客户端访问，保留实例读写防护，并在同一个后台追踪人员操作与数据库调用。

> 本文功能与截图对应当前 **`dev` 分支**。首次启动默认账号为 **`admin / admin`**。Docker Hub 的 `2.1.0` 是既有发布版本；本次工作空间、Token 与界面改动需从 `dev` 源码运行或自行构建，尚未发布为新镜像。

![DB MCP 实例管理：工作空间归属、目标端点与读写权限](docs/images/workspace.png)

<sub>2026-11-17 从当前构建拍摄；资源、成员与审计记录均为隔离演示数据，不含真实业务或凭据。</sub>

<a id="features"></a>

## 产品能力

| 能力 | 当前功能 |
|---|---|
| 资源管理 | 管理 PostgreSQL / Redis 连接，支持手动测试与配置热更新。 |
| 工作空间授权 | 一份资源可归属多个项目；每枚 MCP Token 固定属于一个工作空间，只能发现和操作当前归属资源。 |
| MCP 工具 | 31 个工具：2 个实例工具、6 个 PostgreSQL 工具、23 个 Redis 工具。 |
| 数据库防护 | 实例默认只读；SQL 静态分析与只读事务叠加保护，高危 DDL 需要显式确认。 |
| Token 生命周期 | 独立创建、启停、确认删除；新 Token 支持列表复制及 Claude Code JSON / Codex TOML 接入示例。 |
| Token 到期 | 默认长期有效，可设置、调整或清除到期时间；到期后拒绝后续调用，保留身份和历史。 |
| 成员与会话 | 默认 admin/admin，普通密码 6–1024 字符；人员同权，保护最后一个启用成员，密码变更及停用/删除撤销会话。 |
| 双审计 | 分别查询人员操作与 MCP 调用，按凭证、工作空间和实际目标追溯，支持详情、筛选与 CSV 导出，默认保留 30 天。 |
| 桌面控制台 | Ant Design 6、OPPO Sans 4.0、浅/深主题、可折叠侧栏与统一分页。 |

<details>
<summary>Token 复制、启停与到期状态</summary>

![DB MCP · Token 复制、启停与到期状态](docs/images/tokens.png)

</details>

<details>
<summary>MCP 调用审计与深色主题</summary>

![DB MCP · MCP 调用审计与深色主题](docs/images/audit.png)

</details>

<a id="quickstart"></a>

## 快速开始：当前 dev 源码

需要 Python 3.10+ 和 uv。仓库已包含管理页面构建产物，启动服务无需 Node.js。

```bash
git clone --branch dev https://github.com/Lixiuxiu559/db-mcp.git
cd db-mcp
uv sync --locked
uv run --locked db-mcp --init --config ./runtime/config.yaml
uv run --locked db-mcp --config ./runtime/config.yaml
```

CLI 支持 `--host`、`--port`、`--config` 与 `--data-dir`。`--data-dir` 相对启动目录解析，只覆盖数据路径，不自动移动已有数据。

打开 **http://127.0.0.1:8807/**，按以下顺序完成接入：

1. 用 **`admin / admin`** 登录，可在账号菜单修改密码。普通新建、修改和重置密码要求 **6–1024 个字符**，安装默认密码是唯一例外。
2. 在「工作空间」创建项目。
3. 在「实例管理」添加资源并选择一个或多个所属工作空间。为每个实例选择只读或读写权限，数据库账号原有权限继续生效。
4. 在「MCP Token」为客户端创建凭证，单选一个工作空间；到期时间留空即长期有效。
5. 从列表复制 Token 或打开「接入」，选择 Claude Code / Codex 配置，再用客户端列出可访问资源。

首次启动只在身份库没有任何用户时创建默认账号。已有用户时不补建、不重置；常规重启与升级保留账号、工作空间、有效 Token 和设置的期限。未归属工作空间的资源不向 MCP 客户端开放。

### 用当前源码构建 Docker

在上面的源码目录中执行。以下使用独立的本地镜像名，`runtime/` 保存全部运行数据：

```bash
mkdir -p runtime
umask 077
cat > .env <<EOF
COMPOSE_PROJECT_NAME=db-mcp
DOCKER_UID=$(id -u)
DOCKER_GID=$(id -g)
DBMCP_IMAGE=db-mcp:dev
DBMCP_BIND_ADDRESS=127.0.0.1
DBMCP_PORT=8807
TZ=Asia/Shanghai
EOF
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose -f docker-compose.yml -f docker-compose.build.yml run --rm db-mcp db-mcp --init --config /data/config.yaml
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
curl -fsS http://127.0.0.1:8807/healthz
```

`--init` 不覆盖已有配置；已有运行目录跳过该步。源码进程和容器使用相同端口，运行前先停止另一种启动方式。以 root 部署时，为 `.env` 指定专用非 root UID/GID，并让它拥有 `runtime/` 的读写权限。

<a id="deploy"></a>

## Docker Compose 部署：既有发布镜像

以下无需克隆源码，使用既有 `2.1.0` 镜像。该版本的界面、账号初始化与 Token 能力以[发布记录](docs/releases/2026-10-30-dockerhub.md)和实际镜像为准；体验本文当前功能请使用上面的 `dev` 源码或源码构建镜像。

需要 Docker Engine（或 Docker Desktop）与 Docker Compose v2。无需克隆源码，镜像已包含完整管理页面。部署机器需要能访问目标数据库。

### 1. 准备部署目录

以下命令适用于 Linux/macOS 普通用户。`.env` 会保存端口、镜像和运行 UID/GID；后续重启继续使用相同配置。

```bash
mkdir -p db-mcp/runtime
cd db-mcp
umask 077
cat > .env <<EOF
COMPOSE_PROJECT_NAME=db-mcp
DOCKER_UID=$(id -u)
DOCKER_GID=$(id -g)
DBMCP_IMAGE=lijx559/db-mcp:2.1.0
DBMCP_BIND_ADDRESS=127.0.0.1
DBMCP_PORT=8807
TZ=Asia/Shanghai
EOF
```

若以 root 部署，在 `.env` 中把 `DOCKER_UID` / `DOCKER_GID` 设为专用非 root 账号的 UID/GID，并将 `runtime/` 属主调整为相同值，例如 `chown -R 1000:1000 runtime`。镜像默认使用 UID/GID `1000:1000`。

### 2. 保存 `docker-compose.yml`

```yaml
name: db-mcp
services:
  db-mcp:
    image: ${DBMCP_IMAGE:-lijx559/db-mcp:2.1.0}
    restart: unless-stopped
    user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"
    ports:
      - "${DBMCP_BIND_ADDRESS:-127.0.0.1}:${DBMCP_PORT:-8807}:8807"
    volumes:
      - ./runtime:/data
    env_file:
      - .env
    environment:
      TZ: ${TZ:-Asia/Shanghai}
    stop_grace_period: 20s
```

固定版本与 `latest` 均支持 **`linux/amd64` / `linux/arm64`**，Docker 自动选择架构。建议部署使用固定版本。也可直接下载本仓库的 [docker-compose.yml](docker-compose.yml) 与 [.env.example](.env.example)。

### 3. 初始化并启动

```bash
docker compose pull
docker compose run --rm db-mcp db-mcp --init --config /data/config.yaml
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8807/healthz
```

`--init` 仅首次创建空配置，不覆盖已有文件；已有部署升级时跳过初始化命令。健康接口返回 `{"ok":true}` 后，打开 **http://127.0.0.1:8807/**。

### 4. 完成镜像对应的账号初始化

按该固定版本管理页和启动日志的提示完成首次账号设置。本文前面的 `admin / admin` 安装默认规则对应当前 `dev` 实现，不代表既有镜像已更新。随后在后台配置资源并创建客户端专用 MCP Token。

### Compose 配置说明

| 配置 | 默认值 | 说明 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | `db-mcp` | 同机多套部署使用不同项目名、宿主端口与运行目录。 |
| `DBMCP_IMAGE` | `lijx559/db-mcp:2.1.0` | 固定镜像版本，升级时显式修改。 |
| `DBMCP_BIND_ADDRESS` | `127.0.0.1` | 默认仅本机可访问；需要局域网直连时改为 `0.0.0.0`。 |
| `DBMCP_PORT` | `8807` | 宿主端口；容器内端口仍为 `8807`。 |
| `DOCKER_UID` / `DOCKER_GID` | `1000` / `1000` | 必须对 `runtime/` 有读写权限。 |
| `./runtime:/data` | 目录挂载 | 同时持久化配置、身份、token 与审计，不能改为仅挂载单个配置文件。 |
| `env_file: .env` | `.env` | 将资源凭据注入容器；`.env` 同时参与 Compose 变量替换。 |
| `TZ` | `Asia/Shanghai` | 容器时区；审计时间以 UTC 存储。 |

配置样例见 [config.example.yaml](config.example.yaml)。实际配置为 `runtime/config.yaml`；配置中的相对路径以配置文件目录为基准。`${VARIABLE}` 从**容器环境**读取，缺少变量会报错；凭据可添加到 `.env`，不要把网页密码或 MCP token 写进该文件。网页保存配置保留环境变量引用，但会丢失 YAML 注释。

容器中的 `127.0.0.1` 指向容器自身。目标服务运行在宿主机时，Docker Desktop 可使用 `host.docker.internal`；Linux 可按环境添加 `extra_hosts: ["host.docker.internal:host-gateway"]`。连接远端资源时填写容器可达的地址。

<a id="connect"></a>

## MCP 接入

先在工作台为目标工作空间创建 MCP Token。可从列表重复复制，或打开「接入」直接复制 Claude Code JSON / Codex TOML；以下是同机接入示例：

Claude Code 在项目目录的 `.mcp.json` 中配置。

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

Codex 在 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml` 中配置。

```toml
[mcp_servers.db-mcp]
url = "http://127.0.0.1:8807/mcp"
http_headers = { Authorization = "Bearer <MCP_TOKEN>" }
```

上例适用于客户端和服务在同一台机器。远程接入请把 URL 换成网关的实际 HTTPS 地址；已有网关的用户无需再启动一份服务。使用 token 后可先调用 `list_instances` 获取资源名称。

| 工具 | 用途 |
|---|---|
| `list_instances` / `db_ping` | 发现实例，检查版本、连通性与延迟。 |
| `postgres_query` / `postgres_execute` | 只读查询与受控写入。 |
| `postgres_list_schemas` / `postgres_list_tables` | 浏览 schema、表和视图。 |
| `postgres_describe_table` / `postgres_list_indexes` | 查看字段、约束与索引。 |
| `redis_get` / `redis_scan` / `redis_type` 等 | 读取 string/hash/list/set/zset、遍历 key 与查询元信息。 |
| `redis_set` / `redis_del` / `redis_expire` 等 | 在 write 实例上写入、删除、修改集合与过期时间。 |

每个实例使用 `permissions: read` 或 `write`，默认 `read`。SQL 每次只允许一条语句；高危 `DROP`/`TRUNCATE`/`ALTER` 需要 `confirm=true`。查询默认最多返回 1000 行、50000 个结果字符；大数据集优先用 `redis_scan`；`COUNT` 是扫描提示，每页可能为空，应继续使用返回游标直到其为 `0`。SQL 词法分析不能判断任意存储过程的内部行为，只读事务是数据库侧的兜底。网络错误后的重试不保证写操作仅执行一次。

## 人员、凭证与审计

- 网页使用用户名/密码、HttpOnly Cookie 会话与 CSRF 校验；所有启用人员同权。
- 后台统一管理全部工作空间和资源。每个数据库实例只创建一次，可归属多个项目；留空时任何 MCP token 都无法访问。每枚 token 创建时必须单选空间，归属固定。移除资源的项目归属后，该项目已有 MCP 会话的后续调用也会被拒绝。
- 工作空间仍关联资源或未删除的 token（含停用 token）时，删除会被拒绝。修改共享资源的连接配置影响所有使用它的项目。
- MCP Token 与人员账号独立：新建的 Token 支持列表复制，接入提供 Claude Code 与 Codex 两种示例。服务保存验证哈希和加密副本，列表仅返回元数据；原有仅哈希的 token 无法恢复，需新建才能复制。token 仅用于 `/mcp`，不能登录网页。
- Token 创建与编辑可设置到期时间，留空长期有效；时间按浏览器时区输入。到期后停止新的 MCP 请求与调用，人员仍可复制或调整期限；复制不会自动恢复权限。重启与升级保留原期限，已删除 Token 不能复活。
- 用户名、显示名和 Token 名称上限 128 个 Unicode 字符，Token 备注上限 1000 个字符；登录和编辑使用相同边界。
- 重启、升级或重建容器保留有效 token；创建新 token、停用或删除创建者不会自动吊销旧 token。密码修改、人员停用或删除会撤销该人员会话。成员开关直接保存，删除须确认，至少保留一个启用成员；有其他启用成员时可禁用或删除自己，随后退出登录。
- Token 开关直接启停；确认删除后永久失效并从管理列表移除，保留失效标记与历史审计。
- 人员审计记录登录与配置变更；MCP 审计记录 token、实际实例类型/主机/端口/数据库/连接账号及 SQL 或 Redis 操作、结果和耗时。默认保留 30 天，可筛选、查看详情与导出 CSV。
- 审计不保存凭据或完整查询结果。SQL 和 Redis 参数可能包含业务数据，已识别凭据脱敏不能识别任意业务秘密。SQLite 审计不提供防篡改或跨系统事务保证。

<a id="operations"></a>

## 运维与升级

### 持久化位置

| 宿主路径 | 内容 |
|---|---|
| `runtime/config.yaml` | 服务与资源配置。 |
| `runtime/data/service.token.key` | Token 复制的持久化加密密钥，必须随身份数据库一起备份和恢复。 |
| `runtime/data/service.db` | 人员、会话、工作空间与 token 归属、验证值和加密副本。 |
| `runtime/audit/audit.db` | 人员操作与 MCP 调用审计。 |

这些文件与 `.env`、`backups/` 均不应提交到 Git；升级或更换容器时保留整个 `runtime/`。

```bash
docker compose logs -f --tail=100
docker compose restart
docker compose down
```

`down` 停止并移除容器，不删除宿主 `runtime/`。停止服务会中断在途调用。

### 备份与更新

先停止服务再备份完整目录，包含 SQLite 的 WAL/SHM 文件与 `data/service.token.key`；使用 SQLite 在线备份时也要保存匹配的加密密钥。密钥丢失或损坏会阻止复制和新签发，已有哈希认证继续独立工作。恢复密钥文件后，服务会将其权限收紧为 `0600`，请保留普通文件及正确属主。

```bash
mkdir -p backups
docker compose stop
(umask 077; tar -czf "backups/runtime-$(date +%Y%m%d-%H%M%S).tar.gz" runtime .env docker-compose.yml)
docker compose start
```

使用源码构建的 `dev` 镜像时，更新源码后重新执行前面的 `build` 与 `up -d`，保留原 `.env` 和 `runtime/`。下面的拉取命令适用于已发布镜像。

修改 `.env` 中的 `DBMCP_IMAGE` 为已发布的新版本后执行：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

本次工作空间功能按全新部署设计。初始化后在后台创建工作空间、分配资源并签发 token；旧 `auth.users` 凭证不会自动导入，缺少工作空间的 token 无法调用 MCP。常规重启和升级保留工作空间及有效凭证。历史审计保留已有归属，备份与回滚仍需匹配配置和数据。

### HTTPS 与反向代理

默认端口只绑定本机，适合放在 HTTPS 反向代理后。子路径部署示例：

```nginx
location /db-mcp/ {
    proxy_pass http://127.0.0.1:8807/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

在 `runtime/config.yaml` 中设置 `auth.cookie_secure: true`、`auth.cookie_path: /db-mcp/`，重启服务后使用 `https://mcp.example.com/db-mcp/` 与对应的 `.../db-mcp/mcp`。`proxy_pass` 尾斜杠会剥除前缀；页面资源和 API 使用相对路径。

### 常见问题

| 现象 | 处理 |
|---|---|
| 无法写配置或 SQLite | 检查 `.env` 中 UID/GID 与 `runtime/` 属主；保持整个目录可写。 |
| 远程浏览器无法打开页面 | 默认仅本机监听；使用 HTTPS 反代、SSH 隧道，或显式修改绑定地址。 |
| 网页登录后 MCP 仍认证失败 | 网页账号与 MCP token 分开管理，在 MCP Token 页面创建专用凭证。 |
| 容器连不上资源 | 检查容器网络、目标地址与防火墙，避免误用容器自己的 `127.0.0.1`。 |
| 首次安装如何登录 | 当前 dev 首次启动无用户时使用 `admin / admin`；已有用户使用原凭据，旧镜像按其初始化提示操作。 |

## 其他启动方式

源码、Docker 与 Python 包共用完整 HTTP MCP 服务及管理页面。下面演示从当前源码构建本地安装包。

<details>
<summary>Python 包 / uvx</summary>

```bash
uv build
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --init --config /absolute/path/db-mcp/config.yaml
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --config /absolute/path/db-mcp/config.yaml
```

wheel/sdist 包含默认配置和管理页面；也可 `uv tool install` 安装本地 wheel。当前只发布了 Docker 镜像，未宣称 PyPI 包或 GitHub Release 已发布。uvx 不读取源码的 `uv.lock`；生产完整复现优先使用固定镜像或源码锁文件。运行数据放在安装缓存以外的可写目录。

</details>

<a id="development"></a>

## 开发与项目

管理页面统一采用 **Ant Design 6 控制台**：OPPO Sans 4.0、原生浅深主题、56px 顶栏与 220px/64px 可折叠侧栏。一级菜单直接展示内容，不重复菜单标题；所有列表表头与正文居中，默认不吸顶，分页为 10/20/50、默认 10。主题默认浅色，仅在顶栏切换。公共组件、字体原件与许可、依赖和静态产物均由服务独立持有。具体布局、交互和验证约定见[开发指引](CLAUDE.md)。本地源码和构建产物的迁移不代表已发布镜像更新。

字体文件与授权原文随服务交付，见 [OPPO Sans Fonts License Agreement](webui/public/assets/fonts/OPPO-Sans-LICENSE.txt)。

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check src tests
cd webui
npm ci
npm run build
```

前端改动后必须重建静态产物。需要本地构建容器时，在仓库根目录执行：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

[开发约定](CLAUDE.md) · [发布摘要](docs/releases/2026-10-30-dockerhub.md) · [反馈问题](https://github.com/Lixiuxiu559/db-mcp/issues)

本服务独立开发、发布和部署，与 [MCP 工作空间](https://github.com/Lixiuxiu559/mcp) 共享统一规范，运行时不依赖父目录或其他 MCP 仓库。
