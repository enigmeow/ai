# ai

Reusable specs — distilled from shipped production systems, written to be used
as implementation specs or as prompts for agent-driven work on new projects.

Each document states up front which sections are the transferable spec and
which are worked examples from the reference implementation.

## Specs

| Doc | What it is |
|---|---|
| [`docs/bpm.md`](docs/bpm.md) | **Business Process Management.** A complete spec for putting a BPMN 2.0 workflow engine at the core of a product: the five-table schema, all six engine modules, the five canonical process shapes, integration with routers / webhooks / sweeps / frontend / agents, the testing strategy, and 22 numbered gotchas that were each a real incident. Written so the system can be rebuilt from an empty repo. |
| [`docs/mcp.md`](docs/mcp.md) | **Agent-operable admin plane** — "Admin Tooling over the BPMN Spine." Exposing a product's entire admin surface as MCP tools an AI agent can call. Architecture, the readers / signals / mutations tool taxonomy, the structured-output contract, auth + transport, and a reactive channel layer. |
| [`docs/mcp-why.md`](docs/mcp-why.md) | The case for the above, in concrete weekly-admin-work scenarios. |
| [`docs/mcp-setup.md`](docs/mcp-setup.md) | Connecting Claude Code, Claude Desktop, and remote clients over stdio and HTTP. |

## How they fit together

`bpm.md` is the foundation and `mcp.md` is what it unlocks. The connection is
one idea:

> Because every state transition is already a named signal against a business
> key, exposing the product to an agent becomes a typed façade over the workflow
> service rather than a new API.

Build BPM first; the agent surface is then mostly mechanical. Reading order for
a new project: `bpm.md` §0 → §1 (the laws) → §13 (phased rollout), then
`mcp-why.md` before deciding whether the agent plane is worth it.

## Conventions

The reference implementation is a monorepo serving several products from one
FastAPI / MySQL codebase. Where per-product detail matters, they are referred
to neutrally:

- **Site A** — education / training (instructors, courses, videos, assessments)
- **Site B** — events + merch storefront (performers, shows, orders, payouts)
- **Site C** — project-management SaaS (projects, tasks, contracts, invoices)

Source paths (`app/core/bpm/service.py`, `app/sites/<site>/bpmn/…`) describe
that layout and are worth mirroring, but nothing in the specs depends on it.
