# ai

Reusable specs — distilled from shipped production systems, written to be used
as implementation specs or as prompts for agent-driven work on new projects.

Each document states up front which sections are the transferable spec and
which are worked examples from the reference implementation.

## Specs

| Doc | What it is |
|---|---|
| [`docs/bpm.md`](docs/bpm.md) | **Business Process Management.** A complete spec for putting a BPMN 2.0 workflow engine at the core of a product: the five-table schema, all six engine modules, the five canonical process shapes, integration with routers / webhooks / sweeps / frontend / agents, the testing strategy, and 26 numbered gotchas, each a real incident. A design spec with the hazards mapped — see §0 on what you still have to supply. Has a working reference implementation (below). |
| [`docs/mcp.md`](docs/mcp.md) | **Agent-operable admin plane** — "Admin Tooling over the BPMN Spine." Exposing a product's entire admin surface as MCP tools an AI agent can call. Architecture, the readers / signals / mutations tool taxonomy, the structured-output contract, auth + transport, and a reactive channel layer. |
| [`docs/mcp-why.md`](docs/mcp-why.md) | The case for the above, in concrete weekly-admin-work scenarios. |
| [`docs/mcp-setup.md`](docs/mcp-setup.md) | Connecting Claude Code, Claude Desktop, and remote clients over stdio and HTTP. |
| [`docs/storefront.md`](docs/storefront.md) | **Storefront / third-party integration.** Not "how to build a shop" — how to stay correct when your correctness depends on an API you cannot run or pause. The three-mode adapter layer where the fake is production code, why you store the processor's vocabulary instead of abstracting it, the payment integration in detail (§4.5 — idempotency, hosted checkout vs intents, webhook verification), inventory that cannot oversell, and 16 gotchas. |
| [`docs/stripe.md`](docs/stripe.md) | **Stripe integration.** The parts that are not in the quick-start: the object model, call sequences, **the webhook event catalog with what to do for each**, **which errors are worth retrying**, idempotency done properly, and the retry-after-failure problem that makes a declined attempt look like a dead order. 16 gotchas. §5 and §6 are the two that cost real money. |
| [`docs/project-management.md`](docs/project-management.md) | **Multi-party project management.** A domain spec for a platform where staff, clients and external contractors share one workspace with asymmetric visibility. §2 is the transferable core — an authorization model where the untrusted party is denied *by construction* rather than by an exclusion list. §4 is the task system (derived urgency, the auto-narrated activity log, per-audience projections, the board / Gantt / calendar / inbox views). §5 is the money model, and it opens by insisting you settle the revenue model first — cost and price are different vocabularies, and who may see which depends entirely on whether you mark up or charge a fee. Plus invites-as-credentials and their security rules, 17 gotchas, and what to deliberately leave out. |

## Reference code

| Path | What it is |
|---|---|
| [`reference/bpm/`](reference/bpm/) | A working implementation of `docs/bpm.md`, built **from the spec alone** by an agent forbidden from reading any existing implementation. 122 tests on SQLite, 132 on real MySQL, including executable mutants and a conformance suite that checks §12's hazard claims against the engine version in use. Not production code — see its README. |

## How they fit together

`bpm.md` is the foundation, `mcp.md` is what it unlocks, and
`project-management.md` is a full domain built on top of both.

The bpm ↔ mcp connection is one idea:

> Because every state transition is already a named signal against a business
> key, exposing the product to an agent becomes a typed façade over the workflow
> service rather than a new API.

Build BPM first; the agent surface is then mostly mechanical.

**Reading order for a new project:** `bpm.md` §0 → §1 (the laws) → §13 (phased
rollout). Then `mcp-why.md` before deciding whether the agent plane is worth it.
If the product has multiple parties with different trust levels, read
`project-management.md` §2 *before* designing the schema — the role model is the
hardest thing to change later.

## Conventions

The reference implementation is a monorepo serving several products from one
FastAPI / MySQL codebase. Where per-product detail matters, they are referred
to neutrally:

- **Site A** — education / training (instructors, courses, videos, assessments)
- **Site B** — events + merch storefront (performers, shows, orders, payouts)
- **Site C** — project-management SaaS (projects, tasks, contracts, invoices)

Source paths (`app/core/bpm/service.py`, `app/sites/<site>/bpmn/…`) describe
that layout and are worth mirroring, but nothing in the specs depends on it.
