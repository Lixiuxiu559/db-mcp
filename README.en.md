<div align="center">

# DB MCP

**Connect AI to your databases, with a clear trail for every operation.**

[![Docker Hub](https://img.shields.io/badge/Docker_Hub-images-146ED0?logo=docker&logoColor=white)](https://hub.docker.com/r/lijx559/db-mcp)
[![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-4B5FC4)](docs/releases/2026-10-30-dockerhub.md)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.1.4-17242B)](https://gofastmcp.com)

[简体中文](README.md) · **English**

[Features](#features) · [Quick start](#quickstart) · [Docker](#deploy) · [MCP setup](#connect) · [Operations](#operations)

</div>

A self-hosted MCP gateway for PostgreSQL and Redis. Manage connections centrally, scope client access by workspace, preserve per-instance read/write safeguards, and trace management actions and database calls in one console.

![DB MCP instances: workspace assignments, endpoints, and read/write permissions](docs/images/workspace.png)

<sub>Captured from the current build on 2026-11-17. Resources, members, and audit history are isolated demo data, with no production data or credentials. The console UI is in Chinese.</sub>

<a id="features"></a>

## Features

| Capability | Current behavior |
|---|---|
| Resources | Manage PostgreSQL / Redis connections, manual connection tests, and live configuration changes. |
| Workspace access | Assign a resource to multiple projects. Each Token belongs to exactly one workspace and can discover and operate only on its currently assigned resources. |
| MCP tools | 31 tools: 2 instance tools, 6 PostgreSQL tools, and 23 Redis tools. |
| Database safeguards | Read-only by default, with SQL checks, read-only transactions, and explicit confirmation for high-risk DDL. |
| Token lifecycle | Create, enable, disable, and confirm deletion. Newly issued Tokens support repeat copying plus Claude Code JSON and Codex TOML connection examples. |
| Token expiry | No expiry by default. Set, change, or clear an expiry; expired Tokens reject subsequent calls while preserving identity and history. |
| Members and sessions | Start with admin/admin; ordinary passwords require 6–1024 characters. Enabled members have equal access, the last enabled member is protected, and password changes or disabling/deletion revoke sessions. |
| Two audit trails | Trace management actions and MCP calls by credential, workspace, and actual target. Filters, details, CSV export, and 30-day default retention. |
| Desktop console | Ant Design 6, OPPO Sans 4.0, light/dark themes, a collapsible sidebar, and consistent pagination. |

<details>
<summary>Token copying, controls, and expiry states</summary>

![DB MCP · Token copying, controls, and expiry states](docs/images/tokens.png)

</details>

<details>
<summary>MCP call audit in the dark theme</summary>

![DB MCP · MCP call audit in the dark theme](docs/images/audit.png)

</details>

<a id="quickstart"></a>

## Quick start

Requires Python 3.10+ and uv. Built web assets are committed, so starting the service does not require Node.js.

```bash
git clone --branch master https://github.com/Lixiuxiu559/db-mcp.git
cd db-mcp
uv sync --locked
uv run --locked db-mcp --init --config ./runtime/config.yaml
uv run --locked db-mcp --config ./runtime/config.yaml
```

CLI options include `--host`, `--port`, `--config`, and `--data-dir`. The latter resolves against the working directory and overrides data paths without moving existing data.

Open **http://127.0.0.1:8807/** and complete the following steps:

1. Sign in with **`admin / admin`** and change the password from the account menu if desired. Ordinary password creation, changes, and resets require **6–1024 characters**; the installation default is the only exception.
2. Create a project in Workspaces.
3. Add resources in Instances and assign one or more workspaces. Choose read-only or read/write access per instance. Database account privileges continue to apply.
4. Create a client credential in MCP Token, selecting exactly one workspace. Leave expiry empty for no expiry.
5. Copy the Token from the list or open its connection settings, select Claude Code / Codex, and use the client to discover accessible resources.

The default account is created only when the identity database has no users. Existing users prevent creation or resets. Routine restarts and upgrades preserve users, workspaces, valid Tokens, and expiry settings. Unassigned resources are inaccessible to MCP clients.

### Build Docker from source

Run in the source directory above. This uses a separate local image name; `runtime/` holds all persistent data:

```bash
mkdir -p runtime
umask 077
cat > .env <<EOF
COMPOSE_PROJECT_NAME=db-mcp
DOCKER_UID=$(id -u)
DOCKER_GID=$(id -g)
DBMCP_IMAGE=db-mcp:local
DBMCP_BIND_ADDRESS=127.0.0.1
DBMCP_PORT=8807
TZ=Asia/Shanghai
EOF
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose -f docker-compose.yml -f docker-compose.build.yml run --rm db-mcp db-mcp --init --config /data/config.yaml
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
curl -fsS http://127.0.0.1:8807/healthz
```

`--init` never overwrites an existing configuration; skip it for an existing runtime directory. Stop a source process before starting a container on the same port. When deploying as root, set a dedicated non-root UID/GID in `.env` and give it read/write ownership of `runtime/`.

<a id="deploy"></a>

## Docker Compose: versioned images

This no-checkout option uses the pinned `2.1.0` image. Its features and account setup follow the corresponding [release record](docs/releases/2026-10-30-dockerhub.md). The source build above includes the functionality shown in this README.

Requires Docker Engine (or Docker Desktop) and Docker Compose v2. No source checkout is needed; the image includes the complete web console. The deployment host must be able to reach your databases.

### 1. Prepare a deployment directory

The following commands target a regular Linux/macOS user. `.env` persists the image, port, and runtime UID/GID for future restarts.

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

For deployment as root, set `DOCKER_UID` / `DOCKER_GID` in `.env` to a dedicated non-root account and give that account ownership of `runtime/`, for example `chown -R 1000:1000 runtime`. The image defaults to UID/GID `1000:1000`.

### 2. Save `docker-compose.yml`

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

Versioned tags and `latest` support **`linux/amd64` / `linux/arm64`**, selected automatically by Docker. Prefer a fixed version for deployments. You can also download [docker-compose.yml](docker-compose.yml) and [.env.example](.env.example) from this repository.

### 3. Initialize and start

```bash
docker compose pull
docker compose run --rm db-mcp db-mcp --init --config /data/config.yaml
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8807/healthz
```

Run `--init` only for the first deployment: it creates an empty configuration and never overwrites an existing file. Skip initialization when upgrading. Once health returns `{"ok":true}`, open **http://127.0.0.1:8807/**.

### 4. Complete setup for that image version

Follow the first-account setup instructions in the pinned image's console and startup logs, then configure resources and create a client-specific MCP Token. Source-built installations start with `admin / admin` when the identity database has no users.

### Compose configuration

| Setting | Default | Purpose |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | `db-mcp` | Use distinct project names, host ports, and runtime directories for multiple deployments. |
| `DBMCP_IMAGE` | `lijx559/db-mcp:2.1.0` | Pinned image; change explicitly when upgrading. |
| `DBMCP_BIND_ADDRESS` | `127.0.0.1` | Local access only; use `0.0.0.0` when direct LAN access is required. |
| `DBMCP_PORT` | `8807` | Host port; the container continues to use `8807`. |
| `DOCKER_UID` / `DOCKER_GID` | `1000` / `1000` | Must have read/write access to `runtime/`. |
| `./runtime:/data` | Directory mount | Persists configuration, identities, tokens, and audit; mount the whole directory. |
| `env_file: .env` | `.env` | Supplies resource credentials to the container and values for Compose interpolation. |
| `TZ` | `Asia/Shanghai` | Container timezone; audit timestamps are stored in UTC. |

See [config.example.yaml](config.example.yaml). The live configuration is `runtime/config.yaml`; relative configuration paths resolve against its parent directory. `${VARIABLE}` reads the **container environment** and fails if missing. Add resource credentials to `.env`; web passwords and MCP tokens belong in their respective console pages. Web edits preserve environment references but discard YAML comments.

Inside a container, `127.0.0.1` refers to that container. For resources on the host, Docker Desktop supports `host.docker.internal`; Linux deployments can add `extra_hosts: ["host.docker.internal:host-gateway"]` when appropriate. Use a container-reachable address for remote resources.

<a id="connect"></a>

## Connect an MCP client

Create an MCP Token for the target workspace. Copy it again from the list or open its connection settings for Claude Code JSON / Codex TOML. These examples assume a local client:

Claude Code uses the project's `.mcp.json` file.

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

Codex uses `~/.codex/config.toml` or a trusted project's `.codex/config.toml`.

```toml
[mcp_servers.db-mcp]
url = "http://127.0.0.1:8807/mcp"
http_headers = { Authorization = "Bearer <MCP_TOKEN>" }
```

This example assumes the client and gateway run on the same machine. Remote clients should use the gateway's actual HTTPS URL. Users of an existing deployment do not need a separate gateway process. Call `list_instances` first to discover resource names.

| Tool | Purpose |
|---|---|
| `list_instances` / `db_ping` | Discover instances and check connectivity, version, and latency. |
| `postgres_query` / `postgres_execute` | Read-only SQL and controlled writes. |
| `postgres_list_schemas` / `postgres_list_tables` | Browse schemas, tables, and views. |
| `postgres_describe_table` / `postgres_list_indexes` | Inspect columns, constraints, and indexes. |
| `redis_get` / `redis_scan` / `redis_type`, etc. | Read string/hash/list/set/zset data, scan keys, and inspect metadata. |
| `redis_set` / `redis_del` / `redis_expire`, etc. | Write, delete, update collections, and manage expiry on write-enabled instances. |

Each instance uses `permissions: read` or `write`, defaulting to `read`. Only one SQL statement is accepted per call. High-risk `DROP`/`TRUNCATE`/`ALTER` requires `confirm=true`. Defaults cap results at 1,000 rows and 50,000 characters; prefer `redis_scan` for large datasets. `COUNT` is a scan hint: a page may be empty, so keep using the returned cursor until it is `0`. Lexical SQL checks cannot infer arbitrary stored-procedure behavior; database read-only transactions provide the final guard. Connection retries cannot guarantee exactly-once writes.

## People, credentials, and audit

- Web access uses username/password, HttpOnly session cookies, and CSRF protection. All enabled users have equal management access.
- The console manages all workspaces and resources. Each database instance is created once and can belong to multiple workspaces. Unassigned resources are inaccessible to MCP tokens. A token belongs to exactly one workspace, fixed at creation. Removing a resource assignment blocks subsequent calls from that workspace, including established MCP sessions.
- A workspace with assigned resources or undeleted tokens, including disabled tokens, cannot be deleted. Changes to shared connection settings affect every workspace using the resource.
- MCP tokens are separate from users. Newly issued Tokens support copying from the list and generating Claude Code or Codex connection settings. Tokens are stored as verification hashes plus encrypted copies; ordinary list responses expose metadata only. Older hash-only tokens cannot be recovered and must be recreated to enable copying. Tokens authorize `/mcp`, not web login.
- Token creation and editing support an optional expiry in the browser time zone. Leave it empty for no expiry, or explicitly clear an existing limit. Expired Tokens reject subsequent MCP requests and calls, including new attempts after connection waits and retries. Staff may still copy or adjust the expiry; copying never restores access. Restarts preserve the limit, and deleted Tokens cannot be revived.
- Usernames, display names, and Token names allow up to 128 Unicode characters; Token descriptions allow up to 1000. Sign-in and editing share these limits.
- Restarts, upgrades, and container replacement preserve valid tokens. Creating a token, disabling its creator, or deleting its creator does not revoke other tokens. Password changes, disabling, and deletion revoke that user's web sessions. Member switches save immediately; deletion requires confirmation and at least one enabled member must remain. With another enabled member present, you can disable or delete your own account and will be signed out.
- Token switches directly enable or disable access. Confirmed deletion permanently invalidates the credential and removes it from the management list while retaining its tombstone and audit history.
- Management audit records logins and configuration changes. MCP audit records token identity, actual instance type/host/port/database/account and SQL or Redis operation, outcome, and duration. Retention defaults to 30 days, with filters, details, and CSV export.
- Audit excludes credentials and complete query results. SQL and Redis arguments may contain business data; redaction of recognized credentials cannot detect every business secret. Local SQLite audit is neither tamper-proof nor a cross-system transaction guarantee.

<a id="operations"></a>

## Operations and upgrades

### Persistent data

| Host path | Contents |
|---|---|
| `runtime/config.yaml` | Service and resource configuration. |
| `runtime/data/service.token.key` | Persistent encryption key for token copying; back up and restore with the identity database. |
| `runtime/data/service.db` | Users, sessions, workspaces, token assignments, verification hashes, and encrypted copies. |
| `runtime/audit/audit.db` | Management and MCP audit records. |

Keep these files, `.env`, and `backups/` out of Git. Preserve the entire `runtime/` directory across upgrades and container replacement.

```bash
docker compose logs -f --tail=100
docker compose restart
docker compose down
```

`down` removes containers without deleting the host `runtime/` directory. Stopping the service interrupts active calls.

### Back up and update

Stop the service before backing up the full directory, including SQLite WAL/SHM files and `data/service.token.key`, or use a reliable SQLite online backup procedure and preserve the matching encryption key. A missing or damaged key blocks copying and new token issuance without changing existing hash authentication. On restoration, the service tightens key-file permissions to `0600`; preserve a regular file and correct ownership.

```bash
mkdir -p backups
docker compose stop
(umask 077; tar -czf "backups/runtime-$(date +%Y%m%d-%H%M%S).tar.gz" runtime .env docker-compose.yml)
docker compose start
```

For a source-built image, update the source and repeat the earlier `build` and `up -d` steps, preserving `.env` and `runtime/`. The pull commands below apply to published images.

Set `DBMCP_IMAGE` in `.env` to the new published version, then run:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Workspace access is designed for a fresh deployment. After initialization, create workspaces, assign resources, and issue tokens in the console. Legacy `auth.users` credentials are not imported automatically, and tokens without a workspace cannot call MCP. Routine restarts and upgrades preserve workspaces and valid credentials. Historical audit attribution is retained. Backups and rollback still require matching configuration and data.

### HTTPS and reverse proxy

The default host binding is local, suitable for an HTTPS reverse proxy. Example subpath configuration:

```nginx
location /db-mcp/ {
    proxy_pass http://127.0.0.1:8807/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

Set `auth.cookie_secure: true` and `auth.cookie_path: /db-mcp/` in `runtime/config.yaml`, restart, and use `https://mcp.example.com/db-mcp/` with MCP at `.../db-mcp/mcp`. The trailing slash in `proxy_pass` strips the prefix. Assets and API calls use relative URLs.

### Troubleshooting

| Symptom | Action |
|---|---|
| Configuration or SQLite is not writable | Match `.env` UID/GID to the owner of `runtime/`; keep the whole directory writable. |
| Remote browsers cannot open the console | Default binding is local; use HTTPS proxying, an SSH tunnel, or an explicit network binding. |
| Web login works but MCP authentication fails | Web accounts and MCP tokens are separate; create a token in the MCP Token page. |
| Resources are unreachable from the container | Check networking, target addresses, and firewalls; avoid accidentally targeting the container's own loopback. |
| How to sign in after installation | Source-built installations use `admin / admin` if no users exist. Existing users retain their credentials; pinned images follow their version-specific setup instructions. |

## Other installation options

Source, Docker, and Python packages share the full HTTP MCP service and console. The following builds a local package from the current source.

<details>
<summary>Python package / uvx</summary>

```bash
uv build
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --init --config /absolute/path/db-mcp/config.yaml
uvx --from ./dist/db_mcp-2.1.0-py3-none-any.whl db-mcp --config /absolute/path/db-mcp/config.yaml
```

wheel/sdist includes default configuration and web assets. Local wheels can also be installed with `uv tool install`. Only Docker images are currently published; this does not claim a PyPI package or GitHub Release exists. uvx does not use the source `uv.lock`; prefer a pinned image or source lockfile for production reproducibility. Keep runtime data outside installation caches.

</details>

<a id="development"></a>

## Development and project

The console uses **Ant Design 6**, OPPO Sans 4.0, native light/dark themes, a 56px header, and a 220px/64px collapsible sidebar. Top-level pages show content without repeating menu titles. All table headers and cells are centered, do not stick by default, and share 10/20/50 pagination with a default of 10. The theme defaults to light and is switched only in the header. Components, original font and license, dependencies, and static assets are held within this service. See the [development guide](CLAUDE.md) for layout, interaction, and validation rules. Local source and build migration does not update published images.

Font files and the original license are bundled with the service; see the [OPPO Sans Fonts License Agreement](webui/public/assets/fonts/OPPO-Sans-LICENSE.txt).

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check src tests
cd webui
npm ci
npm run build
```

Rebuild static assets after changing the frontend. To build a container from source, run from the repository root:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

[Development guide](CLAUDE.md) · [Published digests](docs/releases/2026-10-30-dockerhub.md) · [Report an issue](https://github.com/Lixiuxiu559/db-mcp/issues)

This service is independently developed, released, and deployed. It shares conventions with the [MCP workspace](https://github.com/Lixiuxiu559/mcp) and has no runtime dependency on a parent directory or another MCP repository.
