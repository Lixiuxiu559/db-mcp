<div align="center">

# DB MCP

**Connect AI to your databases, with a clear trail for every operation.**

[![Docker Hub](https://img.shields.io/badge/Docker_Hub-2.1.0-146ED0?logo=docker&logoColor=white)](https://hub.docker.com/r/lijx559/db-mcp)
[![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-4B5FC4)](docs/releases/2026-10-30-dockerhub.md)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.1.4-17242B)](https://gofastmcp.com)

[简体中文](README.md) · **English**

[Features](#features) · [Docker Compose](#deploy) · [MCP setup](#connect) · [Operations](#operations)

</div>

A self-hosted MCP gateway for PostgreSQL and Redis. Centralize connections and credentials, enforce per-instance read/write boundaries, and review management actions alongside database calls.

![DB MCP workspace preview](docs/images/workspace.png)

<sub>Screenshot from an isolated demo environment. No production resources or credentials are shown. The UI labels in this preview are in Chinese.</sub>

<a id="features"></a>

## Features

| Capability | What you can do |
|---|---|
| Multiple instances | Manage PostgreSQL and Redis, test connectivity, and apply configuration updates without a restart. |
| Data operations | 31 MCP tools for SQL, schema inspection, and common Redis data types. |
| Read/write boundaries | Instances default to read-only; PostgreSQL combines tool permissions, SQL checks, and read-only transactions. |
| Explicit confirmation | High-risk DDL requires `confirm=true`. |
| Separate identity and audit | Independent web accounts and MCP tokens, actual database target snapshots, and 30-day audit retention. |
| Unified workspace | Ant Design 6, OPPO Sans, light/dark themes, a collapsible sidebar, and shared table pagination. |

<a id="deploy"></a>

## Deploy with Docker Compose

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

### 4. Sign in with the installation defaults

The current source and images built from it automatically create username **`admin`** and password **`admin`** on the first installation startup when the identity database has no users. No setup code is required. Source, Docker/Compose, installed packages, and uvx share this rule. The versioned images above require a separate release to include this change.

You can change the password from the account menu after signing in. Creating, changing, or resetting a password requires **6–1024 characters**; the installation default is the only length exception. Existing users prevent default-account creation or password resets. Restarts and upgrades preserve existing accounts, passwords, and valid MCP tokens.

Add a PostgreSQL or Redis instance, choose read-only or read/write access, and test the connection. Then create a client-specific token in MCP Token. The container must be able to reach the database; database account privileges still apply.

### Workspaces in the current source

The workspace feature is included in the current source and local web build. Use the current source or a locally built image for this feature. The versioned images in the quick start above require a separate release to include these changes.

Create a project in Workspaces, add each database instance, and select one or more workspaces for it. Creating a client token requires exactly one workspace. The client can only list and operate on resources assigned to that workspace. Choose read-only or read/write access for each instance. The container must reach the target database, and database account privileges still apply.

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

Create and save a token in the console, then add a configuration to an HTTP-capable MCP client:

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

Each instance uses `permissions: read` or `write`, defaulting to `read`. Only one SQL statement is accepted per call. High-risk `DROP`/`TRUNCATE`/`ALTER` requires `confirm=true`. Defaults cap results at 1,000 rows and 50,000 characters; prefer `redis_scan` for large datasets. Lexical SQL checks cannot infer arbitrary stored-procedure behavior; database read-only transactions provide the final guard. Connection retries cannot guarantee exactly-once writes.

## People, credentials, and audit

- Web access uses username/password, HttpOnly session cookies, and CSRF protection. All enabled users have equal management access.
- The console manages all workspaces and resources. Each database instance is created once and can belong to multiple workspaces. Unassigned resources are inaccessible to MCP tokens. A token belongs to exactly one workspace, fixed at creation. Removing a resource assignment blocks subsequent calls from that workspace, including established MCP sessions.
- A workspace with assigned resources or undeleted tokens, including disabled tokens, cannot be deleted. Changes to shared connection settings affect every workspace using the resource.
- MCP tokens are separate from users. The current source supports copying newly issued tokens from the list and generating Claude Code or Codex connection settings. Tokens are stored as verification hashes plus encrypted copies; ordinary list responses expose metadata only. Older hash-only tokens cannot be recovered and must be recreated to enable copying. Tokens authorize `/mcp`, not web login.
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

Stop the service before backing up the full directory, including SQLite WAL/SHM files and `data/service.token.key`, or use a reliable SQLite online backup procedure and preserve the matching encryption key. A missing or damaged key blocks copying and new token issuance without changing existing hash authentication.

```bash
mkdir -p backups
docker compose stop
(umask 077; tar -czf "backups/runtime-$(date +%Y%m%d-%H%M%S).tar.gz" runtime .env docker-compose.yml)
docker compose start
```

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
| How to sign in after installation | Current source creates `admin / admin` on first startup if no users exist; existing installations keep their credentials. |

## Other installation options

All three distribution methods include the HTTP MCP service and web console.

<details>
<summary>Source + uv</summary>

Requires Python 3.10+ and uv. Full dependency compatibility follows the lockfile and verified runtime.

```bash
uv sync --locked
uv run --locked db-mcp --init --config ./runtime/config.yaml
uv run --locked db-mcp --config ./runtime/config.yaml
```

CLI options include `--host`, `--port`, `--config`, and `--data-dir`. The latter resolves relative to the working directory and overrides paths without moving existing data.

</details>

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

### Credential expiry and field limits in the current source

- In the current source, Token creation and editing support an optional expiry time in the browser time zone. Leave it empty for no expiry. Expired tokens reject new MCP requests and calls; staff may still copy or explicitly adjust the expiry. Copying never restores access. Restarting preserves the expiry, and deleted tokens cannot be revived.
- Usernames, display names, and Token names allow up to 128 Unicode characters; Token descriptions allow up to 1000. Sign-in and editing share these limits.
