# MCP — Setup

> **Status:** M0–M5 (2026-04-20). Covers connecting Claude Code,
> Claude Desktop, and remote Claude.ai clients to the two MCP
> servers. Both stdio (local) and HTTP (remote) transports work.
> **Read first:** `docs/mcp.md` for the design, `docs/mcp-why.md`
> for the pitch.

## Prerequisites

```bash
docker compose up --build     # brings the backend up so the DB is reachable
pip install "mcp>=1.2.0"       # on the host, if running MCP outside docker
```

The MCP server connects to the same MySQL instance as the FastAPI
backend. It does **not** go through HTTP — it imports the service
layer directly. Running `docker compose up` is sufficient for the
database; the MCP subprocess can run on the host with `.env.<site>`
loaded, or inside a container.

## Run on the host (recommended for Claude Code / Desktop)

From the repo root, `APP_SITE` picks the site and the corresponding
`.env.<site>` file is loaded by pydantic-settings:

```bash
cd backend
APP_SITE=site_b  python -m mcp_main    # binds to siteb DB
APP_SITE=site_a python -m mcp_main    # binds to sitea DB
```

`mcp_main` runs FastMCP over stdio and blocks until a client
attaches. Ctrl-C exits.

## Claude Code

Add to `.mcp.json` at the repo root (or your user-level MCP
config):

```json
{
  "mcpServers": {
    "platform-site_b": {
      "command": "python",
      "args": ["-m", "mcp_main"],
      "cwd": "/Users/joshsiegel/repos/platform/backend",
      "env": {
        "APP_SITE": "site_b",
        "STOREFRONT_MODE": "fake",
        "ENVIRONMENT": "dev"
      }
    },
    "platform-site_a": {
      "command": "python",
      "args": ["-m", "mcp_main"],
      "cwd": "/Users/joshsiegel/repos/platform/backend",
      "env": {
        "APP_SITE": "site_a",
        "STOREFRONT_MODE": "fake",
        "ENVIRONMENT": "dev"
      }
    }
  }
}
```

Then in a Claude Code session: `/mcp list` to verify both servers
register; `/mcp tools platform-site_b` to list tools.

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "platform-site_b": {
      "command": "/Users/joshsiegel/repos/platform/backend/venv/bin/python",
      "args": ["-m", "mcp_main"],
      "cwd": "/Users/joshsiegel/repos/platform/backend",
      "env": { "APP_SITE": "site_b" }
    },
    "platform-site_a": {
      "command": "/Users/joshsiegel/repos/platform/backend/venv/bin/python",
      "args": ["-m", "mcp_main"],
      "cwd": "/Users/joshsiegel/repos/platform/backend",
      "env": { "APP_SITE": "site_a" }
    }
  }
}
```

Desktop requires an absolute python path (no PATH resolution),
hence the venv binary. Restart Claude Desktop after editing.

## First session

Once connected, every session begins with a login:

```
You: login as me
Claude: [calls login(email=..., password=...)]
        Signed in as _test_admin (u_...), expires 2026-04-21T...
You: list the last 10 orders
Claude: [calls list_orders(limit=10)]
        10 orders: ...
```

The session token is cached in the MCP subprocess memory. It
persists across tool calls until the subprocess exits or you
`logout`.

## Tools available in M0

- `login(email, password)` — admin-only; caches session token.
- `whoami()` — who am I logged in as?
- `logout()` — clear cache + invalidate server-side session.
- `list_orders(state?, limit?, cursor?)` — paginated order list.
- `signal_workflow(business_key, signal_name, payload?)` — fire a
  BPMN signal; advances the named workflow.

M1 adds full reader coverage; M2 adds every BPMN signal + the
reactive channel surface; M3/M4 bring per-site domain tools
(~170+ tools total per site); M5 adds HTTP transport + long-lived
bearer tokens for remote use.

## HTTP transport (remote — phone / Claude.ai / shared laptop)

Production path. The MCP server runs as a systemd unit on the VPS,
nginx proxies `https://<site>/mcp` to it, bearer-token auth on every
request.

### On the VPS

One-time install — a systemd unit per site plus an nginx `location /mcp`
block that proxies to it:

```bash
# Install systemd units + patch nginx
sudo cp deploy/{sitea-mcp,siteb-mcp}.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sitea-mcp siteb-mcp
# Add the `location /mcp` proxy block to each server block, then
sudo nginx -t && sudo systemctl reload nginx
```

### Mint a token

Sign into the site's admin UI → `/admin/mcp-tokens.html` →
"New token" → give it a name → copy the `mcp_…` value it reveals
**once**. The server never shows it again.

Revoke from the same page any time.

### Connect Claude Code / Desktop to a remote server

Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "platform-siteb-remote": {
      "type": "http",
      "url": "https://site-b.example.com/mcp",
      "headers": {
        "Authorization": "Bearer mcp_<paste your token here>"
      }
    }
  }
}
```

Claude Desktop config — same shape under `mcpServers`.

For Claude.ai (phone / browser) the integration path depends on
Anthropic's current remote-MCP support; follow the URL + bearer
token pattern in their docs.

### Bearer acceptance

The server accepts **two** token kinds:

1. **Long-lived MCP tokens** (prefix `mcp_`, minted above). Preferred
   for anything you type once and leave in place.
2. **UserSession tokens** (bare UUID from `/api/auth/login`). Cheap
   bootstrap — log into the site, paste the `sc_token` /
   `ff_token` localStorage value. Rotates automatically; revoke by
   logging out.

Both paths land on the same `require_current_user` resolver
server-side.

## Troubleshooting

- **"AUTH_REQUIRED" on every tool call** — you haven't logged in
  yet, or the subprocess restarted (killed by Claude's MCP
  lifecycle). Re-run `login`.
- **"AUTH_EXPIRED"** — session TTL lapsed. Re-run `login`.
- **SQLAlchemy "mapper cannot locate ConsumerProfile"** — only
  happens if `mcp_main` is started without loading the full site
  import chain. The entrypoint already does this via
  `app.sites.<site>.mcp.register` → core handlers → internal
  functions → storefront + workflow model imports. If you see
  this, double-check `APP_SITE` is set.
- **"mcp module not found"** — `pip install "mcp>=1.2.0"` on
  whichever interpreter your config points at.
