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

## Quick deployment with Docker

Requires Docker Engine or Docker Desktop. Pull the image and start it; Git, Python, Node.js, an `.env` file, and a pre-created configuration are not required. Choose either `docker run` or Compose below.

```bash
docker pull lijx559/db-mcp:2.2.0
docker run -d --name db-mcp \
  --restart unless-stopped \
  -p 127.0.0.1:8807:8807 \
  -v db-mcp-data:/data \
  lijx559/db-mcp:2.2.0
```

Open **http://127.0.0.1:8807/** and sign in with **`admin / admin`** on a new installation. The image creates a default configuration if missing and creates the default account only when the identity database has no users. Existing accounts, passwords, workspaces, Tokens, and audit data are preserved.

Docker creates the named volume automatically. It stores configuration, identities, the Token encryption key, and audit data under `/data`, surviving container replacement. When upgrading an existing deployment, retain its original volume or bind mount, UID/GID, and environment settings; update only the image to avoid switching to an empty volume.

<a id="deploy"></a>

## Use Docker Compose

Save this single `compose.yaml` file:

```yaml
name: db-mcp
services:
  db-mcp:
    image: lijx559/db-mcp:2.2.0
    container_name: db-mcp
    restart: unless-stopped
    ports:
      - "127.0.0.1:8807:8807"
    volumes:
      - runtime:/data
    stop_grace_period: 20s
volumes:
  runtime:
    name: db-mcp-data
```

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Both methods use the same named volume and default container name. Stop and remove the previous container before switching methods, retaining the volume. Do not run two service instances against the same identity database.

### Sign in and connect resources

1. Sign in with `admin / admin` and change the password from the account menu if desired. Ordinary passwords require 6–1024 characters.
2. Create a project in Workspaces.
3. Add resources in Instances and assign one or more workspaces.
4. Create an MCP Token for that workspace; leave expiry empty for no expiry.
5. Copy the Token from the list or open its connection settings for Claude Code / Codex configuration.

The published host port is local-only by default. For direct LAN access, replace `127.0.0.1` in the port mapping with `0.0.0.0` and use `http://SERVER_IP:8807/`. Use HTTPS reverse proxying for public access. Inside the container, `127.0.0.1` refers to the container itself; use a container-reachable address for resources.

The image runs as UID/GID `1000:1000`; named volumes need no manual directory ownership setup. For a custom host bind mount, grant the container user read/write access. Resource credentials can be entered in the console. If using `${VARIABLE}` references, supply them through Compose `environment` / optional `env_file` or `docker run -e`.

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

The default persistent volume is **`db-mcp-data`**, mounted at `/data`:

| Container path | Contents |
|---|---|
| `/data/config.yaml` | Service and resource configuration. |
| `/data/data/service.db` | Users, sessions, workspaces, Tokens, and encrypted copies. |
| `/data/data/service.token.key` | Token encryption key; back up and restore with the identity database. |
| `/data/audit/audit.db` | Management and MCP call audit. |

```bash
docker logs -f --tail=100 db-mcp
docker restart db-mcp
curl -fsS http://127.0.0.1:8807/healthz
```

For Compose, use `docker compose logs -f --tail=100`, `docker compose restart`, and `docker compose stop`. `docker compose down` retains named volumes; `down -v` deletes the data and is not an upgrade command.

### Backup and update

Stop the container and copy its entire data directory to the host, including SQLite files and the Token key:

```bash
umask 077
docker stop db-mcp
docker cp db-mcp:/data "./backup-db-mcp-$(date +%Y%m%d-%H%M%S)"
docker start db-mcp
```

To update with Compose, set `image` in `compose.yaml` to the new version, then run:

```bash
docker compose pull
docker compose up -d
```

For `docker run`, pull the new version, stop and remove the old container, and run again with the original ports and **the same data volume**. Upgrades and rollback retain matching configuration, identity database, key, and audit data. Legacy `auth.users` credentials are not imported automatically, and Tokens without a workspace are not granted access.

### Source and Python packages

Use source for development or custom builds; ordinary Docker deployment does not require these commands.

```bash
git clone --branch master https://github.com/Lixiuxiu559/db-mcp.git
cd db-mcp
uv sync --locked
uv run --locked db-mcp --init --config ./runtime/config.yaml
uv run --locked db-mcp --config ./runtime/config.yaml
```

The CLI supports `--config`, `--host`, `--port`, and `--data-dir`. Containers additionally use `--init-if-missing`, preserving existing configuration. Standalone `--init` creates configuration and exits. Configuration paths resolve against its parent directory; `--data-dir` resolves against the working directory and does not move existing data.

```bash
uv build
uvx --from ./dist/db_mcp-2.2.0-py3-none-any.whl db-mcp --init --config /absolute/path/db-mcp/config.yaml
uvx --from ./dist/db_mcp-2.2.0-py3-none-any.whl db-mcp --config /absolute/path/db-mcp/config.yaml
```

Local wheels/sdists include default configuration and console assets. Keep runtime data outside installation caches. uvx does not use the source `uv.lock`; package availability follows the release records.

<a id="development"></a>

## Development and project

The console uses **Ant Design 6**, OPPO Sans 4.0, native light/dark themes, a 56px header, and a 220px/64px collapsible sidebar. Top-level pages show content without repeating menu titles. All table headers and cells are centered, do not stick by default, and share 10/20/50 pagination with a default of 10. The theme defaults to light and is switched only in the header. Components, original font and license, dependencies, and static assets are held within this service. See the [development guide](CLAUDE.md) for layout, interaction, and validation rules.

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
export DBMCP_IMAGE=db-mcp:local
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

[Development guide](CLAUDE.md) · [Published digests](docs/releases/2026-10-30-dockerhub.md) · [Report an issue](https://github.com/Lixiuxiu559/db-mcp/issues)

This service is independently developed, released, and deployed. It shares conventions with the [MCP workspace](https://github.com/Lixiuxiu559/mcp) and has no runtime dependency on a parent directory or another MCP repository.
