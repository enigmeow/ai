# MCP Servers — Admin Tooling over the BPMN Spine

> **Status:** M0–M5 shipped (2026-04-20). 174 tools on Site A, 94 on
> Site B. Both sites pass their parity gates. HTTP transport live;
> long-lived bearer tokens mintable from `/admin/mcp-tokens`.
> End-to-end HTTP test suite green (133 MCP tests, every registered
> tool exercised over the wire). Resource-subscription push surface
> still intentionally deferred — long-poll `wait_for_channel` covers
> the push use case and client support for `resources/subscribe` is
> still patchy.
> **Read first:** this doc for shape + §11 for what actually
> shipped vs design. `docs/mcp-setup.md` for stdio + HTTP client
> config. `docs/bpm.md` for how signals move workflows.
> **Covers:** architecture (two servers + shared core), tool
> taxonomy (readers / BPM signals / direct mutations), entity
> catalog per site, auth + transport, phased rollout M0–M5,
> implementation deltas (§11).
>
> ### About this document
>
> A spec distilled from a shipped implementation. **Site A** (education /
> training — instructors, courses, videos, assessments) and **Site B** (events
> + merch storefront — performers, shows, orders, payouts) are two products
> served from one codebase; per-site detail is there to show how the
> shared-core / per-site split works, not because the split is required.
>
> **The portable spec** is §1 (the parity commitment), §2 (architecture —
> shared core + site modules, execution model, transport, auth, the channels /
> long-poll reactive surface), §3 (tool taxonomy: readers / BPM signals /
> direct mutations, and the structured-output contract), §6.1 (build strategy
> with sub-agents) and §11 (what shipped vs. what was designed).
>
> **Worked examples rather than requirements:** §4 (entity catalog), §5
> (example sessions), §6 (the M0–M5 rollout).
>
> The load-bearing idea, and the reason this sits next to `docs/bpm.md`:
> **because every state transition is already a named signal against a business
> key, exposing the product to an agent is a typed façade over the workflow
> service rather than a new API.** BPM-everywhere is what makes agent
> operability cheap.

## 1. Goal

**Parity commitment:** anything an admin can do through the web UI,
they must be able to do via MCP. Anything observable in the web
admin (dashboard counts, logs, event timelines, BPMN audit,
feature flags, Stripe review queue, email outbox) is observable
via MCP. The web admin and MCP are two UIs over the same service
layer; neither has a private superset.

Expose the admin plane of each site as MCP tools so an operator
working in Claude can answer and act on questions like:

- "List orders with pending-ship status older than 48h and the
  performer they pay out to."
- "Show me stuck workflows — anything in `running` for more than
  7 days."
- "Kick the shipment workflow on order 3f2… past the label step."
- "Which performers still have `details_submitted` but not
  `payouts_enabled`? Resend the onboarding link for each."
- "Refund order X full amount, reason duplicate, and reverse the
  transfer."

Every one of these is already possible via the web admin UI + curl.
The MCP server is **not new business logic** — it's a typed,
machine-callable façade over the routers and BPM engine that
already exist.

## 2. Architecture

### 2.1 Two servers, one per site

Strict continuation of the hard rule *"never import across sites."*
Each site gets its own MCP process bound to its own DB via
`APP_SITE`:

| Server | Binary | Env | DB |
|---|---|---|---|
| Site A | `sitea-mcp` | `APP_SITE=site_a`, `.env.site_a` | `sitea` |
| Site B | `siteb-mcp` | `APP_SITE=site_b`, `.env.site_b` | `siteb` |

The two servers are **separate MCP endpoints**. An admin working on
both sites connects Claude to both. Cross-site questions ("compare
Site B order volume to Site A class enrollment") are resolved on the
client side by calling two tools.

### 2.2 Shared core + site modules

Mirrors the FastAPI layout:

```
backend/app/core/mcp/
├── server.py           # FastMCP bootstrap, APP_SITE dispatch
├── auth.py             # admin API key → User lookup
├── render.py           # pydantic → TextContent (compact human string)
├── models/             # Pydantic BaseModels — the output contract
│   ├── workflow.py     # WorkflowSummary, WorkflowDetail, SignalResult, …
│   ├── user.py
│   ├── storefront.py   # OrderSummary, OrderDetail, PaymentDetail, …
│   ├── connect.py
│   ├── observability.py  # EventEntry, FeatureEntry, LogLine, StripeEvent
│   └── settings.py
├── internal/           # composable typed functions — return model instances
│   ├── workflows.py    # list_workflows(db, …) -> list[WorkflowSummary]
│   ├── users.py
│   ├── storefront.py
│   ├── connect.py
│   ├── observability.py
│   └── signals.py      # resolve signal catalog from BPMN registry
├── handlers/           # thin MCP-facing façades (one per tool)
│   ├── workflows.py    # @mcp.tool → calls internal, wraps result
│   ├── users.py
│   ├── storefront.py
│   ├── connect.py
│   ├── observability.py
│   └── settings.py
└── inputs.py           # pydantic DTOs for tool *inputs* (filters, ids)

backend/app/sites/site_b/mcp/
├── register.py         # mounts Site B-specific handlers
├── models/             # Site B-only output models
├── internal/
└── handlers/

backend/app/sites/site_a/mcp/
├── register.py
├── models/
├── internal/
└── handlers/
```

The three-layer split is load-bearing:

- **`models/`** is the contract. Schemas advertised to clients
  (`outputSchema`) come from these. Only place where field shape
  is defined. Shared with `services/` — both the router and MCP
  return the same pydantic instances.
- **`internal/`** is the composition layer. Functions take a
  `db: Session` + typed args, return typed model instances, and
  **compose with each other**. `internal.storefront.get_order`
  can call `internal.workflows.list_for_object` without going
  through the MCP machinery. Under the hood these delegate to
  the extracted `services/` modules (see §2.3).
- **`handlers/`** is ~5 lines per tool: call the internal fn,
  render text via `render.py`, return `CallToolResult`. No business
  logic here.

**Call chain for a write:** `handler` → `internal` →
`services/<domain>` → ORM + adapters + `BpmService`. The web
router calls into the same `services/<domain>` layer. Two entry
points, one implementation — the API never drifts.

Boot sequence identical to `backend/main.py`:

```python
# backend/mcp_main.py
site_mod = importlib.import_module(f"app.sites.{settings.site}.mcp.register")
site_mod.register(mcp)  # mcp = FastMCP("platform-<site>")
```

### 2.3 Tool-call execution model

Every tool call runs inside a fresh `SessionLocal()` context — same
pattern as a FastAPI request. Writes commit before returning;
errors roll back. Tools never import FastAPI — they import the
service layer directly (`BpmService`, storefront adapters, model
queries).

**Per-call scaffolding (every handler):**

```python
def _with_request(fn):
    """Decorator that wraps a handler with a DB session + current_user
    resolution from the auth ContextVar. Mirrors get_db + get_current_user
    from the FastAPI side — but uses the token stashed at login time."""
    def wrapped(args):
        session = auth.require_session()            # raises AUTH_REQUIRED
        with SessionLocal() as db:
            try:
                result = fn(db, session.user, args)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
    return wrapped
```

Handlers never touch `SessionLocal()` directly; they receive
`(db, actor, args)` from the wrapper. Internal functions receive
`(db, *, actor, **kwargs)` and return pydantic models.

Where a router today does meaningful work on top of the ORM (e.g.
resolving `connected_account` → `guardian_user_id`, or calling
`resolve_splits` during checkout), the MCP tool calls the **same
helper the router calls**, never reimplements it.

**Rule: no duplication, always extract.** If router logic lives
inline in the handler body, the MCP phase extracts it to a
`services/` module before adding the tool. The router becomes a
thin adapter (parse request → call service → shape response);
the MCP `internal/` function calls the same service. Duplicating
even 5–10 lines is banned — it seeds API drift where a web fix
silently skips the MCP path or vice versa.

Service module homes:

| Originating router | Service module |
|---|---|
| `backend/app/core/storefront/routers/admin_*.py` | `backend/app/core/storefront/services/<domain>.py` |
| `backend/app/core/routers/*.py` (workflows, auth, admin_connect, admin_splits, features, …) | `backend/app/core/services/<domain>.py` |
| `backend/app/sites/<site>/routers/*.py` | `backend/app/sites/<site>/services/<domain>.py` |

Services take `db: Session` + typed kwargs + `actor: User` and
return pydantic models (the same ones in `mcp/models/`). No
FastAPI types crossing the boundary. Extraction PRs are
mechanical and merge cleanly against concurrent router edits
because each function moves as a contiguous block.

### 2.4 Transport

Default: **stdio** — Claude Code / Desktop run the server as a
subprocess via a config entry in `.mcp.json` / `claude_desktop_
config.json`. Zero network exposure; inherits the invoking user's
env (including `.env.<site>`).

Future: **HTTP/SSE** behind a per-admin API key for remote use
(phone → Claude.ai → VPS). Not in M0–M5.

### 2.5 Auth

**Model: login once per MCP session, token cached in process
memory.** No per-call token argument on every tool; it would bloat
every input schema and Claude would have to keep re-quoting it.
Instead, auth is stateful for the process lifetime, exactly like a
browser tab.

Three tools form the auth surface:

| Tool | Behavior |
|---|---|
| `login(email, password)` | Calls the **same auth service the HTTP router calls** (`app.core.services.auth.login(db, email, password)`) — no HTTP round-trip. On success, stashes the returned `UserSession` + `User` in process memory (a module-level `ContextVar`-backed holder) and returns `SessionInfo(user_id, username, roles, expires_at)`. Required before any other tool call. |
| `whoami()` | Returns current `SessionInfo` or an error if not logged in. Safe to call anytime. |
| `logout()` | Calls `app.core.services.auth.logout(db, session)` and clears the cached holder. |

Behavior rules:

1. **No ambient auth.** Cold process = unauthenticated. First
   tool must be `login`. Any other tool call before login returns
   a structured `{error: {code: "AUTH_REQUIRED"}}`.
2. **Admin-only.** `login()` rejects non-admin users with
   `AUTH_FORBIDDEN`. The MCP server is not a consumer surface.
3. **Token re-use.** The cached token is the same shape as
   `/api/auth/login` hands to a browser — it's a `UserSession`
   row, not a synthetic. Every downstream tool call uses it to
   resolve `current_user`, so audit trails (`state_transitions`,
   `order_events`, `stripe_reviews.events`) record the actual
   admin, not a generic "mcp".
4. **Expiry handling.** If the session token expires mid-call
   (slowapi / session revocation), the tool returns `AUTH_EXPIRED`
   and the client re-issues `login`. MCP doesn't silently
   re-login; password is never persisted.
5. **Rate limit coupling.** Logins hit the same slowapi bucket
   (10/min) as the web login. The user memory already flags this
   — fixtures should `getpass` once and reuse, not re-login on
   every tool call.
6. **Secrets hygiene.** Password arrives in the tool call
   arguments once. The handler forwards it to `/api/auth/login`
   and discards it — never logged to `state_transitions`, never
   persisted. Claude clients should elide the password from
   transcripts (we document this in the setup guide, but can't
   enforce it client-side).

**Two transports, same auth:**

- **stdio (M0–M4):** login cached in subprocess memory. One
  subprocess per admin-laptop per site. Token lifetime matches the
  subprocess — restart = new login.
- **HTTP (M5):** same `login` tool, but the returned token is
  also echoed to the caller so an HTTP client can send it as
  `Authorization: Bearer …` for resume-after-disconnect. This is
  the scenario that matches the "token per call" mental model;
  stdio hides it because the process state *is* the session.

**Optional long-lived tokens (M5+):** for automated/headless
clients we add `/admin/mcp-tokens` minting. Not needed for
interactive Claude use; the session-token path works.

### 2.6 Deployment

Local: `docker compose up` adds two services (`sitea-mcp`,
`siteb-mcp`) that idle until Claude attaches via stdio. Or,
simpler: Claude configs point at `python -m backend.mcp_main` on
the host with `APP_SITE` set — no container required for stdio.

Production: systemd units mirror the web units — `sitea-mcp.service`
and `siteb-mcp.service` only needed once HTTP transport lands.
Stdio MCP doesn't need a long-running server daemon; the admin's
Claude client spawns it on demand.

### 2.7 Reactive surface — channels + long-poll

MCP has no first-class "channel" primitive, but
`resources/subscribe` + server-side `LISTEN/NOTIFY` give us one.
Two tool flavors, one backend:

**Long-poll tool (works on every MCP client today):**

```python
list_channels() -> [ChannelMeta(name, description), ...]

wait_for_channel(
  channel: str,
  cursor: str | None = None,       # opaque; None = "from now"
  filter: dict | None = None,      # e.g. {event_types: ["refund.*"]}
  timeout_sec: int = 300,
) -> ChannelBatch(events=[...], next_cursor="...")

wait_for_event(                    # filter-flavored; scans all channels
  object_type: str | None = None,
  event_types: list[str] | None = None,
  since_ts: datetime | None = None,
  timeout_sec: int = 300,
) -> EventBatch(events=[...], next_cursor="...")
```

**Resource-subscription surface (ship when client support matures):**

- URI scheme: `channel://<name>` (e.g. `channel://orders`).
- Client calls `resources/subscribe`; server emits
  `notifications/resources/updated` when new events land.
- Same backend queues power both surfaces.

**Backend wiring (MySQL — no LISTEN/NOTIFY primitive):**

- **Short-interval polling** against the event tables, keyed by
  an auto-increment cursor. Each channel gets a
  `(table, where_clause, order_col)` binding:

  | Channel | Poll source |
  |---|---|
  | `orders`, `orders:<id>` | `SELECT … FROM order_events WHERE id > :cursor ORDER BY id LIMIT 500` |
  | `stripe-reviews` | `SELECT … FROM stripe_reviews_events WHERE id > :cursor …` |
  | `payouts` | `SELECT … FROM state_transitions WHERE object_type='entity_transfer' AND id > :cursor …` |
  | `workflows:<key>` | `SELECT … FROM state_transitions WHERE process_key=:key AND id > :cursor …` |
  | `emails:failed` | `SELECT … FROM email_messages WHERE status='failed' AND id > :cursor …` |
  | `features` | `SELECT … FROM features WHERE updated_at > :cursor …` |
  | `webhooks:stripe` | `SELECT … FROM stripe_events WHERE id > :cursor …` |
  | `errors` | journald tail (not DB) |

- **Global ticker:** one `asyncio` task per MCP connection polls
  every 1s (configurable via `MCP_POLL_INTERVAL_MS`), routes new
  rows into per-channel `asyncio.Queue` instances scoped to that
  connection. `wait_for_channel` awaits its queue; resource
  subscriptions drain from the same queues.
- **Cursor** = last-seen `id` (for auto-inc tables) or
  `updated_at` (for tables without a monotonic PK). Opaque base64
  `(kind, value)` tuple exposed to clients — cheap to compare;
  survives reconnects.
- **Why polling is fine here:** 1-second latency is imperceptible
  for operator workflows, and the existing BPM timer
  (`backend/app/core/bpm/timer.py`) already polls
  `process_instances` on a similar cadence — the infrastructure
  pattern is proven in this codebase.
- **Upgrade path:** if latency demands it, drop in Redis pub/sub
  behind the same `asyncio.Queue` abstraction. After-commit hooks
  in the web app publish; MCP server subscribes. Zero change to
  tool contracts.

**Channels baked in from day one:**

| Channel | Fed from | Use case |
|---|---|---|
| `orders` | `order_events` | Live shop activity |
| `orders:<id>` | `order_events WHERE order_id=…` | Focus on one order |
| `stripe-reviews` | `stripe_reviews.events` | Review queue live |
| `payouts` | `entity_transfers` + `state_transitions(object_type=entity_transfer)` | Connect transfer lifecycle |
| `workflows:<process_key>` | `state_transitions WHERE process_key=…` | Watch a specific BPMN flow |
| `emails:failed` | `email_messages WHERE status='failed'` | Send failures live |
| `errors` | journald ERROR lines (gated on `MCP_ALLOW_LOGS`) | App exceptions |
| `features` | `features.updated_at` | Flag flips |
| `webhooks:stripe` | `stripe_events` raw ledger | Everything Stripe sends |

**Reactive loop:** Claude calls `wait_for_channel("stripe-reviews",
timeout=1800)`; server blocks; a review lands; tool returns; Claude
reacts next turn; Claude calls `wait_for_channel` again. Works for
hours unattended. Closing the session drops the connection and
the loop stops — no retention, no replay beyond `since_ts` /
`cursor`. Server push requires an open connection; there is no
wake-from-cold.

Phase slot: M2 (long-poll + channel catalog), M5 (resource
subscriptions once HTTP transport lands and Claude Code /
Desktop subscription handling is reliable).

## 3. Tool Taxonomy

Three strict categories. Mixing them blurs the contract.

### 3.1 Readers — always safe

`list_<entity>(filters, state?, limit?, cursor?)` and
`get_<entity>(id)`. Pure reads. Never write. Always paginate (default
limit 50). Return normalized JSON with ISO timestamps + enum strings
so Claude can reason about values without guessing.

Readers include **joined context** where useful:

- `list_orders` returns cart totals + customer email + current
  status, not just order rows.
- `get_order(id)` returns the full payment + refunds + events +
  items + shipment row + current workflow instance id and status.
- `list_workflows(status='running')` returns business_key,
  process_key, started_at, last-advanced-at, waiting-task names,
  and object_type/id.

This is the heavy-lift category. Most operator questions are
answered by a good reader alone.

### 3.2 BPM signals — the primary write surface

Follows the memory rule: **routers emit signals, not state
mutations.** MCP tools do the same.

```
signal_workflow(business_key, signal_name, payload?)
signal_workflow_by_correlation(object_type, object_id, signal_name, payload?)
complete_task(task_id, form_data?)
start_workflow(process_key, business_key, object_type?, object_id?, initial_data?)
list_pending_tasks(assignee?, object_type?)
```

Under the hood these call `BpmService.signal`,
`signal_by_correlation`, `complete_task`, `start`. Every call is
audited with the MCP admin as actor.

Tool docstrings enumerate the **valid signal names for each
process_key** so Claude doesn't have to guess. Source of truth for
that list is the `.bpmn` files — the tool's schema is generated at
boot from the workflow registry.

### 3.2.1 Signal catalog — self-describing

`list_signals(process_key?)` returns every `(process_key,
signal_name, payload_schema)` tuple the engine accepts. Generated
at boot by walking each `.bpmn` file in the registry and pulling
`<signalEventDefinition>` names + matching payload DTOs from
`backend/app/core/bpm_tasks/<process>/signals.py` (to be added
during M2). No more guessing signal names from memory.

`describe_workflow(process_key)` returns the human-readable diagram
summary — start events, gateways, user tasks, service tasks, end
states, and which signals move which gateway. Enough for Claude to
answer "what happens if I signal `payout.reverse` on a running
entity_transfer?" without opening the .bpmn file.

### 3.3 Direct mutations — only where BPM wraps nothing

Not every write is workflow-driven. Examples that are direct-only:

- Toggling a feature flag (`set_feature`, `features` table only).
- Editing `platform_settings` (Stripe keys, storefront mode,
  shipping-free-threshold).
- Replying to an inquiry (Site B) — no workflow, just writes a row
  + emails.
- Manual CRUD on static content (blog post publish, gallery photo
  add) — though Site A wraps blog in a workflow, so Site A's blog
  publish goes through 3.2.

Every direct-mutation tool has a docstring note explaining **why
it's not a signal** (no workflow exists, intentionally out of BPM
scope, etc.). If a new workflow later wraps one of these, the tool
moves from 3.3 to 3.2 and the docstring reason goes away.

### 3.4 Structured-output contract (MCP spec 2025-11-25)

Every tool returns `mcp.types.CallToolResult` with **both payloads
populated from the same pydantic model**:

```python
# models/storefront.py
class OrderSummary(BaseModel):
    id: str
    created_at: datetime
    customer_email: str
    status: Literal["pending", "paid", "shipped", "delivered",
                    "refunded", "cancelled"]
    total_cents: int
    currency: str
    workflow_id: str | None
    waiting_task: str | None

class OrderListResult(BaseModel):
    orders: list[OrderSummary]
    next_cursor: str | None

# internal/storefront.py
def list_orders(db: Session, *, status: str | None = None,
                limit: int = 50, cursor: str | None = None
               ) -> OrderListResult:
    rows = _query_orders(db, status=status, limit=limit, cursor=cursor)
    return OrderListResult(
        orders=[OrderSummary.model_validate(r, from_attributes=True)
                for r in rows],
        next_cursor=_next_cursor(rows, limit),
    )

# handlers/storefront.py
@mcp.tool(
    name="list_orders",
    description="List orders with optional status filter.",
    outputSchema=OrderListResult.model_json_schema(),
)
def list_orders_tool(args: ListOrdersArgs) -> CallToolResult:
    result = internal.storefront.list_orders(
        _db(), status=args.status, limit=args.limit, cursor=args.cursor,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=render.orders(result))],
        structuredContent=result.model_dump(mode="json"),
    )
```

Rules:

1. **One model, two renderings.** `structuredContent` is
   `model.model_dump(mode="json")` (ISO dates, enums as strings).
   `content[0]` is a compact human string rendered by `render.py`
   from the *same* model — never hand-built. If the two drift,
   it's a bug.
2. **`outputSchema` is always set** on every tool, generated via
   `Model.model_json_schema()`. Clients that support structured
   outputs get typed results; older clients fall back to the text.
3. **Errors are structured too.** Exceptions in internal functions
   bubble up; the handler catches, builds an `ErrorResult` model
   (`{code, message, detail?}`), returns `CallToolResult(
   isError=True, content=[TextContent(...)],
   structuredContent=ErrorResult(...).model_dump())`. Codes match
   the HTTP families the routers already raise (404, 409, 422).
4. **Lists always wrap.** A list tool returns a `…ListResult`
   model with `items` (or named field) + `next_cursor`, never a
   bare `list[…]`. Keeps room for metadata (total counts,
   truncation flags) without a breaking change.
5. **No `Any` in output models.** If a field is genuinely
   polymorphic (e.g. `payload` on a signal result), declare it
   `dict[str, JsonValue]` with a `JsonValue = str | int | float |
   bool | None | list["JsonValue"] | dict[str, "JsonValue"]` alias.
   Claude reasons better about typed shapes.
6. **Input DTOs live in `inputs.py`.** Separate module so the
   output schema can be regenerated without touching input shapes.

### 3.5 What we do NOT expose

- Raw SQL execution. Claude asks via typed tools, not `SELECT *`.
- Migrations, schema inspection, or alembic commands. Those are
  human-driven.
- Credential mutations (cannot rotate Stripe keys via MCP;
  `set_storefront_mode` is fine, `set_stripe_secret_key` is not).
- Delete-user, delete-order, hard-delete-anything. Soft-state
  changes via signals; permanent destruction stays in the web UI
  behind explicit confirmation.
- File uploads. Claude can't stream bytes into CF Stream cleanly;
  keep uploads in the browser admin.

## 4. Entity Catalog

One row per entity. "BPMN" = process_key that wraps its lifecycle;
empty means direct-mutation or read-only.

### 4.1 Core (both sites)

| Entity | Readers | BPMN | Direct mutations |
|---|---|---|---|
| **Auth / session** | whoami, list_sessions(user_id) | — | login, logout, revoke_session |
| User | list, get, by_email, by_username | `user_account` (Site A) | add_role, remove_role, set_active |
| Video | list, get, list_drafts | `video` (Site A), `siteb_video` | — (publish/unlist/archive via signal) |
| Video upload | — | — | request_upload_url, set_thumbnail, delete_thumbnail |
| PlatformSetting | get_all, get(key) | — | set (allow-listed keys only) |
| StorefrontSettings | get | — | set (mode, stripe keys, shop on/off, thresholds, provider overrides) |
| Feature flag | list, get | — | set |
| ProcessInstance | list, get, get_by_key, list_actions, audit | n/a — this *is* the engine | start, signal, signal_by_correlation, complete_task, retry_task, cancel |
| Pending Task | list (by assignee / object_type), get | — | claim, complete |
| ConnectedAccount | list, get, list_pending | `connected_account` | invite, resend_link (signal), retrieve_from_stripe |
| RevenueSplit | list_for_entity | — | add, update, delete |
| EntityTransfer | list, get, list_for_entity | `entity_transfer` | — (create via checkout fan-out) |
| Product | list, get, by_slug | — | create, update, archive, delete |
| ProductVariant | list_for_product, get | — | create, update, delete, set_price, set_stock |
| ProductImage | list_for_product | — | upload, reorder, delete |
| InventoryItem | list, get, low_stock | — | adjust |
| Cart | list_abandoned, get | `cart_recovery` | — |
| Checkout | estimate | — | (no direct mutations — checkout is consumer-surface only) |
| Order | list, get, search, dashboard_counts | `order`, `shipment`, `refund`, `refund_reversal`, `dispute` | ship, simulate_tracking, refund, patch_status (all routed through signals) |
| Payment | get_for_order, list_stripe_events | `payment_intent` | — |
| Refund | list_for_order, get | `refund`, `refund_reversal` | — (created via order.refund signal) |
| Address | list_for_user, get | — | create, update, delete, set_default |
| EmailMessage | list, get, list_failed | — | resend |
| StripeReview | list (by state), get, get_mode | `stripe_review` | approve (signal), reject (signal), set_mode |
| WebhookEvent | list, get, by_type, by_object_id | — | — (read-only ledger) |

### 4.2 Site B

| Entity | Readers | BPMN | Direct mutations |
|---|---|---|---|
| Performer | list, get, by_slug, list_admin | — | create, update, delete, set_publish, upload_headshot |
| PerformerBio review | (surfaces as StripeReview with entity_type=performer_profile_bio) | `stripe_review` | approve / reject via signal |
| Show | list (upcoming/past), get, list_admin | — | create, update, delete, upload_poster, set_publish |
| ShowCast | list_for_show | — | add_cast, update_cast, remove_cast |
| Inquiry | list (by state), get | — | update_state (reply/archive/flag), delete |
| Booking (private event) | list (by state), get | — | update_state, delete |
| CalendarEvent | list, get, list_admin | — | create, update, delete |
| GalleryPhoto | list, list_by_show, list_by_performer, list_credits, list_admin | — | add, bulk_add, upload, update, delete |
| BlogPost (Site B) | list, get, by_slug, list_admin | — | create, update, delete |
| Site B Video | list (public), get, list_mine | `siteb_video` | create, update, upload_url, set_poster, delete |
| Contact form | — (receives public POST) | — | — (admin side handled via Inquiry) |
| Feature flag | list, get | — | set |

### 4.3 Site A

| Entity | Readers | BPMN | Direct mutations |
|---|---|---|---|
| ConsumerProfile | list, get, search | — | update_profile, update_theme, reset_onboarding |
| OnboardingQuiz | get_for_user | — | update_for_user |
| Instructor | list (by state), list_pending, get, get_by_slug | `instructor_onboarding`, `instructor_certification` | approve, reject, suspend, set_senior (direct today; moving under `instructor_onboarding` signals in M4) |
| Certification | list_for_instructor, get | `instructor_certification` | verify, reject (may move to signals) |
| Course | list, list_mine, get, get_detail | `course` | create, update, delete, publish (signal), archive (signal), attach_video, reorder_videos |
| CourseEnrollment | list, list_for_user, get | `course_enrollment` | enroll, unenroll, refund (via `class_refund` orphan flow when payment race happens) |
| ClassRefundOrphan | list, get | — | retry_refund, mark_manual |
| Assessment | list, list_mine, get | `assessment` | create, update, delete, publish, archive, add_question, update_question, delete_question |
| AssessmentAttempt | list_mine, list_for_assessment, get | `assessment_attempt` | start_attempt (consumer-surface; admin-observed) |
| Slot (class availability) | list, list_admin, get, list_for_instructor | `availability_slot` | create, update, set_visibility, delete |
| SlotBooking (class booking) | list, list_mine, list_for_instructor, get | `booking` | confirm, cancel, book (consumer-surface) |
| CalendarICS | get_token, export_ics | — | — |
| MessageThread | list, get, unread_count | `message_thread` | create_thread, send_message, archive, unarchive, mark_read, upload_attachment |
| GearBag | list, get_for_user | — | create, update, delete |
| GearItem | list_for_bag, get, list_shared_with_me | `gear_item` | create, update, move, delete, add_photo, reorder_photos, delete_photo, add_link, update_link, delete_link, reorder_links, acquire, retire, sell, accept_recommendation |
| GearShare | list_out, list_in, get | `gear_share` | create, accept, delete, copy_into_bag, add_item_under_share |
| BlogPost (Site A) | list, list_mine, get, list_by_instructor | `blog_post` | create, update, delete, submit (signal), archive (signal) |
| Site A Video | list, list_mine, list_mine_drafts, list_watching, get, get_status | `video` | upload_url, metadata_update, publish, unlist, archive, set_thumbnail, delete_thumbnail, progress_ping |
| Analytics | me, posts, assessments, bookings, videos | — | — (read-only reports) |
| Progress | me, me_detail, mark_blog_complete | — | — |

### 4.4 Observability surface

Separate from domain entities — these are the tools that let an
operator *investigate* rather than *change*. Available on both
sites.

| Surface | Tools | Source | Notes |
|---|---|---|---|
| **Features** | `list_features()`, `get_feature(key)` | `features` table | Readers only here; `set_feature` lives in §3.3 direct mutations. Returns `{key, enabled, updated_at, updated_by}`. Lets Claude answer "is the shop on?" and "who turned the blog off and when?". |
| **App logs** | `tail_logs(service?, lines=200, level?, since?)`, `search_logs(pattern, since, until?)` | `journalctl -u <service>` via subprocess on the host; `<service>` = `sitea`, `siteb`, `sitea-mcp`, `siteb-mcp` | Each tool returns structured records: `{ts, level, msg, source}`. Bounded to avoid context blowups (max 500 lines per call). Blocked on prod unless `MCP_ALLOW_LOGS=1`. Local dev reads docker logs instead. |
| **Domain events** | `list_events(object_type, object_id)`, `list_recent_events(limit=50, since?, object_type?)` | `order_events` (storefront) + `state_transitions` (BPMN audit trail) + `stripe_reviews.events` | Unified shape: `{ts, object_type, object_id, event, actor?, payload}`. Merged, sorted descending. The single best tool for "what happened to order X" and "what's been moving in the last hour". |
| **Workflow audit** | `list_workflow_events(process_instance_id)`, `get_workflow_diff(process_instance_id, from_ts, to_ts?)` | `state_transitions` table (`bpm.audit.record` writes here) | Every signal / task_completed / started / ended / errored event with the task_spec_name, actor, and metadata payload. The engine already records these; MCP just exposes them. |
| **Stripe webhooks** | `list_stripe_events(since?, type?, object_id?)`, `get_stripe_event(id)` | `payments.stripe_events` ledger (written by `/api/webhooks/stripe`) | Answers "did Stripe send us anything for this payment?" without logging into the Stripe dashboard. |
| **Signals catalog** | `list_signals(process_key?)`, `describe_workflow(process_key)` | BPMN registry | See §3.2.1. |
| **Email outbox** | `list_emails(since?, to?, status?)`, `get_email(id)`, `resend_email(id)` | `email_messages` table | Already exposed in §4.1 as EmailMessage; called out again here because it's the most-used observability tool in fake mode. |
| **Pending human work** | `list_pending_tasks(assignee?, object_type?)` | `workflow_tasks` table (assigned user tasks) | BPMN user tasks waiting for a human. Duplicate of §3.2 pointer for discoverability. |

**Cross-surface query idiom.** A question like *"what happened to
order 3f2… in the last hour — domain events, workflow signals,
Stripe webhooks, and any app-log warnings"* resolves to four
parallel reader calls, all keyed on the same `object_id`. The tool
shapes are aligned so Claude can sort-merge the four streams by
`ts` and present a single timeline.

### 4.5 Complete-visibility guarantee

Every write path in the codebase already leaves a record in one of
these tables — the MCP readers in §4.4 expose all of them. Full
visibility means every admin action, webhook, signal, or
background retry is reachable through a tool call.

| Write surface | Row lands in | MCP reader |
|---|---|---|
| Admin web action (ship / refund / approve / etc.) | `state_transitions` (via BpmService.signal) + entity-specific event table | `list_events`, `list_workflow_events` |
| Consumer action (place order, enroll, send message) | `order_events`, `course_enrollment_events`, `message_thread_events` | `list_events(object_type, object_id)` |
| Webhook arrival | `stripe_events` ledger (whether processed or not) | `list_stripe_events` |
| Email send (real or fake) | `email_messages` | `list_emails`, `get_email` |
| BPMN signal / task / start | `state_transitions` (audit) + `process_instances` (current state) | `list_workflow_events`, `list_workflows`, `get_workflow` |
| BPMN task retry (failed service task) | `workflow_task_retries` | `list_workflow_events` (retries surface as events) |
| Timer-driven tick (nightly reconcile, cart-recovery sweep) | `state_transitions` with `event="timer_fired"` | same |
| Stripe review decision | `stripe_reviews.events` jsonb | `list_events(object_type="stripe_review", …)` |
| Feature flag flip | `features.updated_at` + `state_transitions` (if wrapped) | `list_features`, `list_events` |
| StorefrontSettings change | `platform_settings` row + `state_transitions` | `get_storefront_settings`, `list_events` |
| App exception / stack trace | stderr → journald | `tail_logs(level="error")` |

If a future feature adds a new write path, the checklist is: (a)
pick a table to write to (`order_events`, `state_transitions`, or
a new one), (b) add the reader to §4.4 observability or §4.1–4.3
entity table. No silent writes.

### 4.6 Admin-page parity matrix

Each admin HTML file maps to a set of MCP tools. The table below
is the **parity contract** — every page must resolve before the
site is considered covered. Pages without ✅ on all columns are
M-phase work items.

#### Site B (`public/site_b/admin/`)

| Page | Read | Write | Observability |
|---|---|---|---|
| `admin.html` (hub) | dashboard_counts, list_features, list_events(recent) | — | — |
| `performers.html` | list_performers_admin, get_performer_admin | create/update/delete/upload_headshot | list_events(object_type=performer) |
| `shows.html` + cast | list_shows_admin, get_show_admin | create/update/delete/upload_poster/add_cast/update_cast/remove_cast | list_events(object_type=show) |
| `users.html` | list_users, get_user | add_role/remove_role/set_active | list_sessions, list_events(object_type=user) |
| `blog.html` | list_blog_posts_admin, get_blog_post_admin | create/update/delete | list_events |
| `inquiries.html` | list_inquiries, get_inquiry | update_inquiry_state, delete_inquiry | list_events |
| `bookings.html` | list_bookings, get_booking | update_booking_state, delete_booking | list_events |
| `calendar.html` | list_events_admin | create_event/update_event/delete_event | list_events |
| `gallery.html` | list_photos_admin | add_photo/bulk_add_photos/upload_photo/update_photo/delete_photo | list_events |
| `features.html` | list_features | set_feature | list_events(object_type=feature) |
| `shop-products.html` + `shop-product.html` | list_products_admin, get_product_admin | create/update/delete/archive + variants CRUD + images CRUD | list_events |
| `shop-orders.html` + `shop-order.html` | list_orders, get_order | signal(order.ship), signal(shipment.simulate_tracking), signal(order.refund), patch_status | list_events(object_type=order), list_stripe_events(object_id=payment) |
| `storefront.html` | get_storefront_settings | set_storefront_settings (mode, keys, thresholds, overrides) | list_events |
| `emails.html` + `email.html` | list_emails, get_email | resend_email | (email_messages itself is the log) |
| `stripe-review.html` | list_stripe_reviews, get_stripe_review, get_review_mode | signal(review.approve), signal(review.reject), set_review_mode | list_events(object_type=stripe_review) |
| `connect/accounts.html` | list_connected_accounts, get_connected_account | invite_connected_account, signal(onboarding.resend), retrieve_from_stripe | list_events(object_type=connected_account), list_entity_transfers |

#### Site A (`public/site_a/admin/`)

| Page | Read | Write | Observability |
|---|---|---|---|
| `admin.html` (hub) | dashboard_counts, list_events(recent) | — | — |
| `users.html` | list_users, get_user, search_consumers | add_role/remove_role/set_active | list_sessions, list_events |
| `instructors.html` | list_instructors_admin, list_pending_instructors, get_instructor, list_certifications | approve/reject/suspend/set_senior + verify/reject cert | list_workflow_events(process=instructor_onboarding) |
| `settings.html` | get_platform_settings | set_platform_setting | list_events |
| `class-refunds.html` | list_refund_orphans, get_refund_orphan | retry_refund, mark_manual | list_workflow_events(process=class_refund) |
| `shop-products.html` + `shop-product.html` | (core, same as Site B) | (core, same as Site B) | (core) |
| `shop-orders.html` + `shop-order.html` | (core, same as Site B) | (core, same as Site B) | (core) |
| `storefront.html` | (core) | (core) | (core) |
| `emails.html` + `email.html` | (core) | (core) | (core) |
| `stripe-review.html` | (core) | (core) | (core) |
| `connect/accounts.html` | (core) | (core) | (core) |
| `workflows.html` | list_workflows, get_workflow, get_workflow_actions | retry_task, cancel_workflow, signal, start_workflow | list_workflow_events |

#### Tool-count projection

Rough sizing to calibrate phase scope — not a budget.

| Area | Tools | Phase |
|---|---|---|
| Auth + session | 3 | M0 |
| Workflows + tasks + signals | 12 | M0–M2 |
| Users + roles + sessions | 8 | M1 |
| Storefront (products / orders / payments / refunds / addresses / catalog-admin / emails / settings / dashboard) | ~45 | M1–M2 |
| Connect (accounts / splits / transfers / invites / dashboard) | 12 | M1 |
| Stripe review + mode | 5 | M1 |
| Observability (events / logs / features / webhooks / signals-catalog) | 12 | M1–M3 |
| Site B domain (performers / shows / inquiries / bookings / calendar / gallery / blog / videos) | ~40 | M3 |
| Site A domain (instructors / courses / assessments / slots / messages / gear / blog / videos / analytics / progress / class-refunds) | ~70 | M4 |

**Total: ~210 tools across both servers.** The `internal/` layer
stays much smaller — typed functions are reused across multiple
list/get/filter tools via pydantic input unions.

### 4.7 Parity acceptance gate

Each domain phase (M3 Site B, M4 Site A) gates on a parity check
script, not human inspection.

`backend/tests/mcp/test_parity.py` — at the end of M3 and M4:

1. Walk every `public/<site>/admin/*.html` file.
2. Extract interactive elements: buttons with `data-action=…`,
   `onclick`, form submits, `fetch(...)` call sites in
   `public/<site>/js/admin-*.js`.
3. For each, assert a matching MCP tool exists (named lookup
   against the live MCP registry) OR an explicit
   `# PARITY_EXEMPT: <reason>` marker exists in the HTML/JS
   source.
4. Fail the build if a web action has no MCP counterpart and no
   exemption.

Exemptions are allowed (file uploads, inline DOM drag-reorder)
but the marker must quote a reason. A new exemption requires a
line in §3.5 "What we do NOT expose."

## 5. Example Sessions

### 5.1 "Which orders are stuck?"

```
tool: list_workflows
  args: { process_key: "shipment", status: "running",
          last_advanced_before: "2026-04-13T00:00:00Z" }
→ [ { business_key: "shp_3f2…", object_id: "<order_id>",
      waiting_task: "wait_for_label_purchase",
      advanced_at: "2026-04-11T14:02:11Z" }, … ]

tool: get_order
  args: { id: "<order_id>" }
→ { id, customer_email, total, status: "paid",
    shipment: { carrier: null, tracking: null, … },
    payment: { … }, events: [ … ], workflow_id: "…" }
```

Operator reads, decides the label purchase just needs a retry,
asks Claude to signal.

```
tool: signal_workflow
  args: { business_key: "shp_3f2…", signal_name: "label.retry" }
→ { status: "running", waiting_task: "wait_for_label_purchase",
    last_event: "signal: label.retry at 2026-04-20T..." }
```

### 5.2 "Resend onboarding to every performer stuck at details_submitted"

```
tool: list_connected_accounts
  args: { status: "details_submitted" }
→ [ { id, performer_id, stripe_acct_id, email, … }, … ]

(client-side loop per account:)
tool: signal_workflow_by_correlation
  args: { object_type: "connected_account", object_id: "<id>",
          signal_name: "onboarding.resend" }
```

## 6. Phased Rollout

Each phase is a branch + PR. Phases land sequentially to main. No
feature flag gates it — MCP is admin-only and pre-launch.

### Phase staging — both sites are first-class

The core tree (`app/core/mcp/*`) is site-agnostic; it serves both
Site A and Site B the moment it exists. Each phase brings up new
capability in core, and both site `register.py` modules pick it
up the same day. Site A does not trail Site B except on the
site-specific domain tools (M3 Site B / M4 Site A).

| Phase | Core work (both sites) | Site B-only | Site A-only |
|---|---|---|---|
| M0 | 3-layer skeleton, auth, 1 reader + 1 signal | bring up `siteb-mcp` | bring up `sitea-mcp` (same stubs) |
| M1 | all core readers + observability surface | — | — |
| M2 | BPM signals + reactive (channels) | Site B signals.py (2 processes) | Site A signals.py (15 processes) |
| M3 | — | Site B domain tools + parity gate | — |
| M4 | — | — | Site A domain tools + parity gate |
| M5 | HTTP transport + resource subscriptions | systemd unit | systemd unit |

### M0 — Bootstrap (1 PR)

**Goal:** prove the three-layer pattern end-to-end on one reader
and one signal, **on both sites simultaneously**, so the dual-
site dispatch is exercised from day one.

New files:
- `backend/requirements.txt` — add `mcp>=1.x` (FastMCP).
- `backend/mcp_main.py` — entrypoint; reads `APP_SITE`, imports
  `app.sites.<site>.mcp.register`, starts FastMCP over stdio.
- `backend/app/core/mcp/__init__.py`, `server.py`, `auth.py`,
  `render.py`, `inputs.py`.
- `backend/app/core/mcp/models/{__init__.py,_shared.py,auth.py,
  workflow.py,storefront.py}` (seeded; storefront has only
  OrderSummary + OrderListResult for M0).
- `backend/app/core/mcp/internal/{__init__.py,auth.py,workflows.py,
  storefront.py}` (only list_orders + signal_workflow populated).
- `backend/app/core/mcp/handlers/{__init__.py,auth.py,workflows.py,
  storefront.py}` (login + whoami + logout + list_orders +
  signal_workflow).
- `backend/app/core/services/auth.py` — extracted from
  `backend/app/core/routers/auth.py` (login, logout, session-
  lookup helpers). Router becomes a thin shim.
- `backend/app/sites/site_b/mcp/{__init__.py,register.py}` —
  wires core handlers into the Site B FastMCP instance.
- `backend/app/sites/site_a/mcp/{__init__.py,register.py}` —
  mirror of Site B; at M0 both register.py files have identical
  bodies (import core, register core handlers). Having both
  shakes out dual-site dispatch bugs before they matter.
- `backend/tests/mcp/__init__.py`, `conftest.py`, `test_bootstrap.py`
  (login→list_orders→signal_workflow happy path + 3 error paths:
  AUTH_REQUIRED, AUTH_FORBIDDEN, AUTH_EXPIRED). Parameterised
  across `APP_SITE=site_b` and `APP_SITE=site_a` so every test runs
  on both DBs.
- `docs/mcp-setup.md` — Claude Desktop `.json` + Claude Code
  `.mcp.json` config snippets.

Modified files:
- `backend/app/core/routers/auth.py` — swap inline login logic
  for call to `services.auth.login()`.

Exit: `claude mcp add site_b …` + `claude mcp add site_a …` +
`login` + `list_orders` + `signal_workflow` all work against
local DBs for both sites. `pytest backend/tests/mcp/` green
(parameterised on `APP_SITE`). `outputSchema` visible on every M0
tool via MCP Inspector.

### M1 — Core readers + observability (1–2 PRs, both sites)

**Goal:** every reader in §4.1 + §4.4 works on **both** sites. No
writes other than what M0 shipped. Because readers live entirely
in `app/core/`, both `siteb-mcp` and `sitea-mcp` pick them up
automatically.

Readers: workflows (list/get/by-key/audit), users (list/get/
by-email/list-sessions), products/variants/images (read),
orders/payments/refunds (read), email_messages (list/get/
list_failed), stripe_reviews (list/get/mode), connected_accounts
(list/get/list_pending), revenue_splits (list_for_entity),
entity_transfers (list/get), addresses (list_for_user), platform
_settings (get), storefront_settings (get), features (list/get).

Observability: `list_events` (unified), `list_recent_events`,
`list_workflow_events`, `get_workflow_diff`, `list_stripe_events`,
`get_stripe_event`.

New files (core only — site-specific is M3):
- `backend/app/core/mcp/models/*.py` fleshed out: user, storefront
  (full), connect, observability, settings.
- `backend/app/core/mcp/internal/*.py` fleshed out.
- `backend/app/core/mcp/handlers/*.py` for each reader.
- `backend/app/core/services/observability.py` — unified-event
  query (joins `order_events` + `state_transitions` +
  `stripe_reviews_events` with a common projection).

Modified files (service extraction, mechanical):
- `backend/app/core/routers/workflows.py` → extract to
  `app.core.services.workflows`.
- `backend/app/core/routers/admin_connect.py` → extract to
  `app.core.services.connect`.
- `backend/app/core/routers/admin_splits.py` → extract to
  `app.core.services.splits`.
- `backend/app/core/routers/admin_stripe_review.py` → extract to
  `app.core.services.stripe_review`.
- `backend/app/core/storefront/routers/admin_*.py` → extract to
  `app.core.storefront.services.*`.

Tests: per-module `test_<area>.py` under `backend/tests/mcp/`.

Exit: every core admin **read** path resolves through MCP on
both Site A and Site B. `list_recent_events` merges domain +
workflow + webhook events in one call. Site A-only domain reads
(instructors, courses, assessments, etc.) are still pending M4,
but anything core (orders, users, workflows, Connect, stripe
reviews) works on both sites.

### M2 — BPM surface + reactive (1 PR, +1 for reactive, both sites)

**Goal:** every BPMN signal + task operation callable via MCP.
Channels go live. Works on both sites — the engine is core.

Signal infrastructure:
- `start_workflow`, `signal_workflow`, `signal_workflow_by_
  correlation`, `complete_task`, `retry_task`, `cancel_workflow`,
  `list_pending_tasks`, `claim_task`.
- `list_signals(process_key?)` — walks BPMN registry + per-
  process `signals.py` modules to return typed signal catalog.
- `describe_workflow(process_key)` — diagram summary.

New files:
- `backend/app/core/bpm_tasks/<process>/signals.py` — one per
  core BPMN process (10 processes). Pydantic DTO per signal
  name.
- `backend/app/sites/site_b/bpm_tasks/<process>/signals.py` —
  Site B processes (2: `siteb_video`, `stripe_review` if Site B-
  scoped; `stripe_review` is actually also registered under
  Site A — keep it core).
- `backend/app/sites/site_a/bpm_tasks/<process>/signals.py` —
  Site A processes (15). Biggest signals-file batch, but each
  file is small; mechanical work.
- `backend/app/core/mcp/handlers/signals.py`,
  `backend/app/core/mcp/handlers/tasks.py`.
- `backend/app/core/mcp/internal/signal_catalog.py` — registry
  walker.

Reactive surface (second PR to keep diff readable):
- `backend/app/core/mcp/reactive/`:
  - `channels.py` — `CHANNEL_BINDINGS` table from §2.7.
  - `dispatcher.py` — per-connection asyncio poller.
  - `queues.py` — per-channel queue + cursor helpers.
- `handlers/reactive.py` — `list_channels`, `wait_for_channel`,
  `wait_for_event`.

Tests: signal catalog completeness (every .bpmn has a matching
signals.py), wait_for_channel returns within timeout, cursor
resume.

Exit: every workflow on **both sites** drivable via
`signal_workflow` (10 core + 2 Site B + 15 Site A = 27 process_keys
covered). `list_signals(process_key)` returns typed DTOs for
every signal. Claude can `wait_for_channel("stripe-reviews", 600)`
on either site and receive a live event within 1s of it landing.

### M3 — Site B domain + direct mutations (1 PR)

**Goal:** Site B admin 1:1 coverage.

Entity domains (readers + writes per §4.2): performers, shows +
cast, inquiries, private-event bookings, calendar, gallery, blog
(Site B), Site B videos, features (set).

Service extractions: `app.sites.site_b.services.{performers, shows,
inquiries, bookings, calendar, gallery, blog, videos}`.

App-log surface:
- `handlers/logs.py` — `tail_logs`, `search_logs`; gated on
  `MCP_ALLOW_LOGS=1` (off by default).
- Local dev: shell out to `docker logs <container>`; prod:
  `journalctl -u <service>`.

Parity gate:
- `backend/tests/mcp/test_parity.py` — scans
  `public/site_b/admin/*.html` + `public/site_b/js/admin-*.js`
  for interactive elements; asserts each matches a registered
  MCP tool or has a `PARITY_EXEMPT` marker.

Exit: parity gate green for Site B. Every button on every
`public/site_b/admin/*.html` page is explained by an MCP tool or a
documented exemption.

### M4 — Site A domain + direct mutations (1 PR)

**Goal:** Site A admin 1:1 coverage. The Site A MCP server has
been running since M0 (serving core tools only); this phase
populates its site-specific tree.

Entity domains (readers + writes per §4.3): consumer profiles +
search, instructors + certifications, courses + enrollments +
videos, assessments + attempts + questions, slots + class-bookings
+ ICS, messages + attachments, gear (bags + items + shares +
photos + links), blog (Site A), Site A videos, analytics,
progress, class-refund orphans.

New files:
- `backend/app/sites/site_a/mcp/models/*.py` — per-domain output
  models.
- `backend/app/sites/site_a/mcp/internal/*.py` — composable
  typed functions.
- `backend/app/sites/site_a/mcp/handlers/*.py` — one per tool.
- `backend/app/sites/site_a/services/*.py` — extracted from
  Site A routers (instructors, courses, assessments, slots,
  messages, gear, blog, videos, progress, class-refunds).

Modified files (Site A service extractions, mechanical):
- `backend/app/sites/site_a/routers/{instructors, blog, courses,
  assessments, calendar, messages, gear, analytics,
  consumer_profile, admin, admin_class_refunds, progress}.py`
  — each becomes a thin router over the new service module.

Parity gate extended to Site A:
- `backend/tests/mcp/test_parity.py` now walks
  `public/site_a/admin/*.html` + `public/site_a/js/admin-*.js`
  as well.

Exit: parity gate green for **both** sites.

### M5 — HTTP transport + push (stretch)

**Goal:** remote MCP from phone; resource-subscription push surface
(the richer channel UX).

New work:
- `backend/app/core/routers/admin_mcp_tokens.py` — mint/revoke
  long-lived MCP tokens from `/admin/mcp-tokens`.
- `backend/mcp_main.py --transport http` — run FastMCP over
  HTTP/SSE, bind to local socket, nginx proxies with cert.
- Systemd units `sitea-mcp.service`, `siteb-mcp.service`.
- `backend/app/core/mcp/reactive/resource_bridge.py` — expose
  same channel queues as `channel://<name>` resources with
  `subscribe`/`unsubscribe` support.

Out of scope for this doc: the `/admin/mcp-tokens` token-minting
UI, which deserves its own mini-spec once M5 ships.

### Phase ordering dependencies

```
M0 ──► M1 ──► M2 ──┬──► M3 (Site B domain) ──┐
                   │                        ├──► M5
                   └──► M4 (Site A domain) ─┘
```

M3 and M4 are independent — either can land first. They only
share the parity-gate test file, which already knows how to scan
either or both sites.

### Testing at every phase

- Pytest under `backend/tests/mcp/<phase>.py`.
- No Playwright — tool calls are API-shaped, test directly.
- `backend/tests/mcp/conftest.py` provides: in-memory MCP client,
  admin fixture (mint a UserSession directly, per the memory
  rule about avoiding login-rate-limit), and a `tool()` helper
  that invokes a tool and returns the typed pydantic model.
- Error-path coverage: every tool must have tests for the common
  failure modes (`AUTH_REQUIRED`, not-found, conflict).

## 6.1 Build strategy — Haiku + sub-agents

The *implementation* of MCP is itself highly parallelizable and
highly mechanical after the first domain lands. Lean into that.
Below is the recommended assignment by work type, tuned for cost
+ speed without giving up judgment on the parts that need it.

### Model assignment by work type

| Work type | Model | Why |
|---|---|---|
| Architecture decisions (auth flow, reactive dispatcher, signal-catalog shape) | **Opus 4.7** in main session | Load-bearing; one wrong choice cascades. |
| Service extraction from routers (mechanical: move body → new module, replace with thin call) | **Haiku 4.5** via sub-agent | Pattern is fixed after the first one. 50+ of these will happen. |
| `models/*.py` — pydantic output DTOs per entity | **Haiku** via sub-agent, seeded with a golden example | Grind work; reading SQLAlchemy models, writing pydantic mirrors. |
| `internal/*.py` — typed function per reader | **Haiku** via sub-agent | Templated against the golden example per category (list, get, filter). |
| `handlers/*.py` — ~5-line façades | **Haiku** via sub-agent | Identical shape per tool. Trivially generated from a handler template + the internal/ function signature. |
| `inputs.py` additions (per-tool argument DTO) | **Haiku** | One class per tool, schema-driven. |
| `signals.py` per BPMN process (27 files) | **Haiku** via sub-agent, one agent per 3–5 files | Each file walks one `.bpmn` and types each `<signalEventDefinition>` payload. |
| Tests per domain (golden path + 2–3 error paths) | **Haiku** with a test template | Parameterised `APP_SITE` + stock fixtures. |
| Parity-gate HTML scraping + exemption audit | **Haiku** | Parse `public/<site>/admin/*.html` + `admin-*.js`; flag any `fetch()` without a tool match. |
| Reactive dispatcher (asyncio poller + per-channel queues + cursor math) | **Sonnet 4.6** in main session | Concurrency bugs are nasty; worth the spend. |
| Auth flow (ContextVar holder, session expiry, admin-only gating) | **Sonnet** in main session | Security boundary. |
| Integration tests (full reactive flows, real BPMN signal propagation) | **Sonnet** in main session | Flaky tests eat way more time than they save. |
| End-of-phase review | **Sonnet** via `code-reviewer` subagent | Fresh eyes on each phase before merge. |

### Sub-agent patterns to use

**Pattern A — Fan-out service extraction (M1 + M3 + M4).**

Each router-to-service extraction is independent. Spawn one Haiku
sub-agent per router file with a locked-down prompt:

> Extract the body of `admin_orders.py::ship_order` into a new
> module `app/core/storefront/services/orders.py` function
> `ship_order(db, *, actor, order_id, payload) ->
> OrderAdminDetail`. Replace the router body with a single call
> to the new function. Do not change behavior. Return the diff.

Five to ten of these can run in parallel per message. Main
session reviews the diffs and commits. ~10× faster than doing it
sequentially in-session, ~3× cheaper than using Opus for each.

**Pattern B — Per-domain tool implementation (M3 + M4).**

Each domain (performers, shows, inquiries, bookings, …) is
structurally isolated. After the first domain lands as a golden
example, spawn parallel Haiku sub-agents:

> Using `app/sites/site_b/mcp/{models,internal,handlers}/
> performers.py` as the reference, implement the same three-layer
> setup for **shows**. Source tables: `shows`, `show_cast`. Router
> to mirror: `app/sites/site_b/routers/shows.py`. Return diffs for
> models, internal, handlers, and a `test_shows.py` skeleton.

Main session checks each diff, runs tests, commits. Ten domains
in an afternoon.

**Pattern C — BPMN signal catalog (M2).**

Spawn Haiku sub-agents grouped by 3–5 BPMN files each:

> Parse these .bpmn files. For each `<signalEventDefinition>`,
> read the adjacent service-task handler in `bpm_tasks/<process>/
> *.py` to infer the signal payload. Emit
> `bpm_tasks/<process>/signals.py` with one pydantic class per
> signal name.

Six sub-agents running concurrently cover all 27 processes in one
message round-trip.

**Pattern D — Exploratory research (up front per phase).**

Before each phase, the main session delegates a single
`Explore` sub-agent query:

> "Tell me everywhere in app/sites/site_a/routers/ that mutates
> state without going through BpmService. For each, report the
> file, function, what it writes, and whether there's a matching
> BPMN process."

The agent returns a punch list the main session uses to plan
that phase's service-extraction work. This keeps architectural
context in the main session while delegating the grep-heavy
discovery.

**Pattern E — Code review gate per phase.**

At the end of each phase branch, the main session invokes
`code-reviewer` before opening the PR:

> "Review the M2 branch against §6 in docs/mcp.md. Focus on: (a)
> are all 27 signal catalogs present, (b) does the reactive
> dispatcher correctly isolate queues per connection, (c) does
> the parity gate pass, (d) any inline logic that should have
> been extracted to services/."

### Parallelism budget per phase

Rough expectation — not a hard cap, but if you're burning more
concurrent agents than this, you've scoped a phase too large:

| Phase | Typical parallel sub-agents | Main-session model |
|---|---|---|
| M0 | 0 (all in main — small, load-bearing) | Opus |
| M1 | 3–5 (service extractions + model batches) | Opus → Sonnet |
| M2 | 6 (signal-catalog fan-out) + 1 (reactive in main) | Sonnet (reactive), Opus (architecture) |
| M3 | 8–10 (one per Site B domain) | Sonnet |
| M4 | 10–12 (one per Site A domain) | Sonnet |
| M5 | 2–3 (HTTP transport, resource bridge, systemd) | Opus |

### Cost-aware defaults

- **Default to Haiku** for anything that's "apply template X to
  context Y." That's most of this build.
- **Escalate to Sonnet** when a sub-agent asks a question or
  produces a non-trivial diff the main session can't review
  confidently.
- **Stay in Opus** in the main session when making architecture
  calls, reviewing sub-agent output, and handling cross-cutting
  concerns.
- **Never use a larger model than needed.** Writing a 5-line
  handler façade in Opus is a billing bug, not a quality win.

## 7. Open Questions

1. **Workflow signal schemas.** Should payload schemas for each
   `(process_key, signal_name)` be declared in the BPMN file as an
   extension element, or in a sibling `<process_key>.signals.py`?
   Former keeps it with the diagram; latter is easier to typecheck.
   Leaning sibling Python.

2. **Time-of-use vs time-of-approval for actor.** If an operator
   queues ten `signal_workflow` calls, should every `actor_user_id`
   be the MCP admin, or should the MCP capture "on behalf of" when
   the question comes from a shared support inbox? Start simple:
   one MCP process = one actor.

3. **Reader projection depth.** `get_order` returning the full
   related graph is convenient but costly. Cap at one join level;
   deeper drilldowns go via `get_payment(order_id)` etc. Add
   `fields` filter later if context gets tight.

4. **Cursor pagination format.** Opaque base64-encoded
   `(updated_at, id)` tuple, matching the existing admin list
   endpoints where pagination already exists. Consistency over
   cleverness.

5. **Rate limiting.** stdio is 1:1 with the admin's client, so
   limit on the client side (turn budget). HTTP transport needs
   server-side slowapi hookups identical to the web app.

6. **Error shape.** MCP's tool-result error contract is text + a
   flag; we want enough structure for Claude to recover. Wrap
   exceptions into `{error: {code, message, detail?}}` and return
   `isError: true`. Codes match the HTTP status families the
   routers already raise (404, 409, 422).

7. **Schema drift vs Pydantic.** Tool input/output schemas
   duplicate the pydantic DTOs from `backend/app/core/schemas/`.
   Generate MCP schemas from those DTOs at boot (FastMCP supports
   this via `pydantic.BaseModel` input types) to stay in sync.

## 8. Out of Scope

- Public-facing tools. MCP is admin-only.
- Consumer-facing agents (WebMCP tracked separately — see the
  cross-link once that doc lands).
- Anything requiring browser DOM state (selected row, open modal,
  form draft). That's WebMCP's niche; keep them orthogonal.
- Replacing the admin web UI. MCP is a parallel surface for
  operator questions that are awkward in a grid.

## 11. Implementation notes (what actually shipped)

Deltas between the design above and the M0–M4 code. Future phases
should read this alongside the design so assumptions don't bite.

### 11.1 Naming drift

- The BPM class is **`WorkflowService`**, not `BpmService` as written
  in several places above. Design doc kept the shorter name for
  readability; real imports are
  `from app.core.bpm.service import WorkflowService`.

### 11.2 Reactive backend is polling, not LISTEN/NOTIFY

§2.7 documents the polling fallback because MySQL has no native
`LISTEN/NOTIFY`. What shipped:

- `backend/app/core/mcp/reactive/dispatcher.py` — 1-second poll loop
  per `wait_for_channel` call. Opens short-lived `SessionLocal()`
  each tick so long polls don't hold connections.
- `_latest_id` walks the table iteratively to seed "start now"
  cursors for `cursor=None` calls — fixed a bug where taking the
  earliest-by-ASC sort was masquerading as "latest."
- Upgrade path to Redis pub/sub behind `asyncio.Queue` stays valid;
  channel contract doesn't change.

### 11.3 Events ledger trimmed

Design §4.4 referenced a `stripe_events` webhook ledger table and
`list_stripe_events` / `get_stripe_event` tools. **That table
doesn't exist in the codebase** — webhooks land directly on
`state_transitions` via the BPM audit trail, and the observability
surface uses that source. The ledger tools were dropped from M1;
`list_events` + `list_workflow_events` cover the same ground.

Add the ledger later if volume demands it; the reader is a one-file
add once the write-path exists.

### 11.4 Signal catalog is parsed, not hand-maintained

Design §3.2.1 envisioned per-process `signals.py` modules with
`SIGNAL_PAYLOADS: dict[str, type[BaseModel]]`. What shipped:

- `signal_catalog.list_signals` parses BPMN XML at call time to
  extract signal names from `<signalEventDefinition>` +
  `<signal name="…">` pairs. Source of truth is the .bpmn file.
- Per-process `signals.py` with payload DTOs is **optional** — the
  catalog probes `app/core/bpm_tasks/<process>/signals.py` and
  `app/sites/<current_site>/bpm_tasks/<process>/signals.py`, falls
  through to `payload_schema=None` if absent.
- Cross-site payload probing is **disabled** — importing another
  site's `bpm_tasks` triggers SQLAlchemy `Table already defined`
  collisions (each site defines its own `blog_posts` etc.). The
  loader tolerates the exception and skips.

None of the 27 processes have payload DTOs today. Add them
incrementally where operators need typed payloads; the catalog
picks them up without code changes elsewhere.

### 11.5 Test environment is single-site-per-process

Both sites' models declare identical table names (`blog_posts`,
`stripe_reviews`, etc.) against their own DBs. Loading both into
one Python process → `sqlalchemy.exc.InvalidRequestError: Table X
is already defined for this MetaData instance`.

Consequences for tests:

- `backend/tests/mcp/conftest.py` parameterises on `APP_SITE` but
  only imports `main` once — whichever site boots first wins the
  model registration. Subsequent parameter passes flip `APP_SITE` +
  DB URL; handlers are site-agnostic core so tests still run.
- The known Site A-leak in `app.core.routers.videos` (imports
  `site_a.dependencies` → `site_a.models.profile`) is what makes
  `User.consumer_profile` resolvable on both sites regardless of
  which site loads first.
- Parity-gate tests run per-site (Site B audit + Site A audit are
  separate tests, each skips on the wrong site).

Do not attempt to load both site registers in one process. If
future work needs cross-site testing, spawn separate pytest
workers via pytest-xdist.

### 11.6 Sub-agent fan-out lessons (M3 + M4)

**What worked:** Haiku sub-agents with a golden template + concrete
file layout + domain list produced ~2000 LOC per agent on first
pass. M3 used 1 agent (41 tools / ~15 min wall-clock), M4 used 3
parallel agents (127 tools / ~5 min wall-clock). Pattern B from
§6.1 validated at scale.

**What bit:** Agent 3 in M4 referenced 50+ input DTO classes from
`site_a/mcp/inputs.py` but didn't append them to the file —
imports resolved at boot time, so smoke test caught it immediately.
Lesson: **always smoke-test after a sub-agent batch**; don't trust
the agent's self-reported file list blindly.

**What to add to future prompts:** "After you finish writing, run a
quick ast-parse + import-resolution check across everything you
wrote; flag any unresolved imports before reporting complete."

### 11.7 Router refactor scope

Design §2.3 says "always extract, never duplicate." M3/M4 shipped
**partial** router refactors — only `performers.py` (Site B golden)
was refactored to call its service module. Every other router
still has inline logic identical to what the service also
contains. This **is** duplication.

Why it slipped: scope pressure; the sub-agents were told "do NOT
refactor the original routers." The services are additive, so MCP
is fine, but drift risk exists. Follow-up: a mechanical pass where
each router's admin endpoints become thin shims over
`services/<domain>`. One PR per site.

### 11.8 Direct-mutation tools that should be signals

The design's §4.3 Site A table says "publish/archive via signal"
for courses, assessments, and blog posts. M4 ships these as
**direct mutations** against the ORM state column — the service
layer does `post.state = "published"` without firing a workflow
signal. The existing router code had always done it this way.

Consequences:

- No `blog_post` / `course` / `assessment` workflow is started on
  publish, even though those processes exist in `bpmn/`.
- `state_transitions` has no row for the publish event.
- Admin audit trail shows the HTTP request but not the BPMN
  transition.

Follow-up: wire publish/archive through `signal_workflow`
(`blog_post.submit`, `course.publish`, `assessment.publish`). Drop
the direct state mutation.

### 11.9 `logs` surface gate

`tail_logs` and `search_logs` are registered unconditionally but
refuse with `VALIDATION` error unless `MCP_ALLOW_LOGS=1` is set in
the subprocess env. In production, leave it off by default — turn
on per-incident, turn off again after.

### 11.10 Service layer lives under the site packages

Design §2.3 maps "where services go" against originating-router
location:

| Originating router | Service module |
|---|---|
| `app/core/storefront/routers/admin_*.py` | `app/core/storefront/services/<domain>.py` |
| `app/core/routers/*.py` | `app/core/services/<domain>.py` |
| `app/sites/<site>/routers/*.py` | `app/sites/<site>/services/<domain>.py` |

What shipped matches for **Site B** and **Site A** domain extractions
(both `app/sites/<site>/services/*.py`). Core storefront services
were NOT extracted — storefront write tools are deferred, so MCP
reads the ORM directly and the routers still carry their inline
logic. When storefront writes land, the
`app/core/storefront/services/` tree is the right home.

### 11.11 Tool naming

M1 tools use bare names (`list_orders`, `signal_workflow`,
`get_review_mode`). M3+ site-specific tools use a site prefix:
`site_b_list_shows`, `site_a_list_instructors`. The prefix
distinguishes site-scoped work from core when an operator connects
to both servers at once (Claude sees two separate MCP sessions but
bare-named tools would collide in the operator's mental model).

### 11.12 Parity gate enforcement

`tests/mcp/test_parity.py` runs on every pytest invocation and
checks two things per site:

1. **Required tools are registered** — each page maps to a set of
   tool names that MUST exist. New tool added, page not updated →
   fine. Page adds a button, no matching tool → test fails.
2. **Matrix covers every file** — if someone adds
   `public/site_a/admin/new-feature.html` without updating
   `SITE_A_PARITY_MATRIX`, the audit test fails loudly.

Exemptions (file uploads, inline DOM drag-reorder, Stripe
credential mutations) live in the matrix with a reason string.
Adding an exemption without a reason → `test_every_exempt_has_a_
reason` fails.

### 11.13 HTTP transport is streamable-http, not SSE

M5 uses FastMCP's `streamable-http` transport (MCP spec
2025-11-25's new transport), not the older SSE variant. The server
exposes `streamable_http_app()` — a Starlette app we wrap with
`BearerAuthMiddleware` before handing to uvicorn. Endpoint is
`/mcp` on both sites (Claude Code / Desktop / Claude.ai configure
`type: "http"` + `url: "https://<site>/mcp"` + `headers:
{Authorization: Bearer ...}`).

### 11.14 Output schema dropped — error-path validation

The design (§3.4) advertised a per-tool `outputSchema` in
`list_tools` derived from each tool's pydantic return type.
**M-comprehensive-tests dropped this.** FastMCP's
`convert_result` validates `CallToolResult.structuredContent`
against the output model on every code path — including errors,
where `structuredContent` is legitimately `None` (we can't
synthesise a valid `SessionInfo` for an `AUTH_REQUIRED` error, the
types don't match).

What shipped instead:
- The decorator strips the return annotation on the wrapped
  function so FastMCP generates no output model.
- On success, `structuredContent` carries the pydantic dump.
  Clients can still read the typed payload — it's just not
  advertised as a schema in the tool registry.
- On error, `structuredContent` is omitted. The error code +
  message land on `CallToolResult._meta` (keys `error_code`,
  `error_message`) and in the human-readable `content[0].text`.

If FastMCP ever adds a per-path validation toggle or error-aware
schema handling, bring `outputSchema` back. The output model class
is still authored per tool — just not wired into FastMCP.

### 11.15 Handler return-shape contract

`@tool_result` accepts three return shapes from the wrapped
handler on the happy path:

| Return | structuredContent becomes |
|---|---|
| `BaseModel` subclass | `model.model_dump(mode="json")` |
| `dict` | used verbatim |
| `None` | omitted |
| anything else | `{"value": str(result)}` fallback |

The dict + None cases exist for delete handlers that have nothing
useful to return. The fallback exists for defensive programming —
not something to rely on.

### 11.16 Dual-path auth resolver

`mcp_auth.require_current_user` accepts both kinds of bearer:
- Bare UUID → `auth_service.resolve_session` (UserSession row)
- `mcp_` prefix → `token_service.resolve` (MCPToken row)

`internal_auth.whoami` does the same branch for the SessionInfo's
`expires_at` field (session has real expiry; MCP token synthesises
a 100-year sentinel since tokens don't expire server-side).

`internal_auth.logout` only deletes UserSession rows — MCP tokens
are revoked from the admin UI, not from logout.

### 11.17 HTTP test env needs DATABASE_URL override

`tests/mcp_http/` runs inside the site_b or site_a docker container.
The container env has `DATABASE_URL` pinned to that site's DB.
When the session-scoped fixture parametrises across both sites and
spawns a `APP_SITE=site_a` subprocess from the site_b container, the
child inherits `DATABASE_URL=...siteb` unless the fixture
overrides — pydantic-settings reads env vars with priority over
env files. The fixture explicitly resets `DATABASE_URL` per site
(`_child_env` in `tests/mcp_http/conftest.py`).

Same pattern applies to token minting + cleanup subprocesses —
both must carry the site-specific `DATABASE_URL` or they'd mutate
the wrong schema.

### 11.18 HTTP client tests as the authority

`tests/mcp_http/test_all_tools_over_http.py` is the authoritative
callability test. It spawns the HTTP server, connects via the
official `mcp.client.streamablehttp_client`, iterates every
registered tool, and asserts each one is either successful or
returns a documented error code. The in-process `tests/mcp/`
suite tests handler internals; the HTTP suite tests what Claude
actually sees.

Running just the HTTP sweep:
```bash
docker compose exec site_b python -m pytest tests/mcp_http/
```

Failing tools in the sweep land with per-tool failure annotations,
so the list of broken tools is easy to read in a CI run. The
first run of the sweep in 2026-04-20 caught three bugs that would
have shipped otherwise — see the
`tests(mcp): comprehensive HTTP-transport coverage` commit message.
