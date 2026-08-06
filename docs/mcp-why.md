# Why MCP — The Pitch

> **Status:** design-supporting companion to `docs/mcp.md`
> (2026-04-20).
> **Read first:** this for the "why," then `docs/mcp.md` for the
> "how."
> **Covers:** what the MCP servers unlock, in ten concrete
> scenarios an admin hits weekly.

## What we're building

Two MCP (Model Context Protocol) servers — one per site —
expose every admin capability of Site A Collective and Site B
Fever as typed tools an AI agent can call. Readers for every
entity, BPMN signals for every state transition, a live channel
layer for "tell me when X happens."

**The contract:** anything a human admin can do or see on the
website, an agent can do or see via MCP. No private supersets.
No hidden surfaces.

**Why it's cheap to build:** the routers, services, BPM engine,
and event tables already exist. The MCP layer is a typed façade
over code that ships daily. The work is 95% new files in a
quarantined tree; the 5% that touches existing routers is
mechanical extraction to a service layer that should exist
anyway.

**Why it pays off:** admin work that today takes 30 minutes of
clicking, SQL, and dashboard-hopping collapses into a single
plain-English question. And the agent can *sit* on the system,
reacting to events in real time, while you do something else.

---

## Ten things it unlocks

### 1. "Which orders are stuck, and why?"

One prompt. The agent calls `list_workflows(process_key="shipment",
status="running", last_advanced_before="7d")`, gets each stuck
order, pulls `get_order` + `list_workflow_events` for each,
reads the waiting task name, and tells you:

> Three orders stuck. 3f2… is waiting on `wait_for_label_purchase`
> — EasyPost returned 500 twice, last retry at 14:02. 8a1… is
> waiting on `await_tracking` — carrier never scanned. 6b4… is
> a duplicate of a completed order from last week.

No SQL. No grepping logs. No Stripe dashboard tab.

### 2. Fix the stuck order while you're still in the conversation

Same session:

> Retry the label on 3f2…, mark 6b4… cancelled with reason
> "duplicate," and leave 8a1… alone — I'll email the carrier.

Agent calls `signal_workflow("shp_3f2…", "label.retry")`,
`signal_workflow("ord_6b4…", "order.cancel", {reason:
"duplicate"})`, and reports back. Every signal lands in
`state_transitions` with *your* user id as the actor — audit
trail is indistinguishable from if you'd clicked.

### 3. Batch work without writing a script

> Resend the Stripe onboarding link to every performer stuck at
> `details_submitted`.

Agent calls `list_connected_accounts(status="details_submitted")`,
loops `signal_workflow_by_correlation("connected_account",
<id>, "onboarding.resend")` for each, tells you who got the
email and when. Twenty minutes of CSV export + dashboard
clicking becomes one prompt.

### 4. Sit on a channel, react to a real event

You start a Claude session before your shift:

> Watch `stripe-reviews` for new items. When one lands, read the
> charge, score it against my heuristics, and either approve low-
> risk ones automatically or summarise the high-risk ones so I
> can decide.

Agent calls `wait_for_channel("stripe-reviews", timeout=1800)`.
Claude blocks server-side. A review lands, tool returns, Claude
pulls the charge, decides, either signals `review.approve` or
tells you what to look at. Loop repeats for hours. You handle
the judgment calls; Claude handles the queue.

### 5. "What happened to this order in the last hour?"

Four parallel reader calls from a single prompt —
`list_events(object_type="order", object_id=…)`,
`list_workflow_events(process_instance_id=…)`,
`list_stripe_events(object_id=…)`, `tail_logs(since="1h")` —
sort-merged into one timeline:

> 14:02 customer checked out. 14:02 payment_intent.created.
> 14:03 Stripe posted radar_early_fraud_warning. 14:03 workflow
> opened stripe_review, paused order. 14:17 you approved. 14:17
> workflow resumed, fired shipment.create. 14:17 EasyPost
> returned 500 — app log `easypost timeout 30s`. Workflow is
> currently waiting on `wait_for_label_purchase`, ready for
> retry.

No dashboards. No scrolling journald.

### 6. The BPMN signal name is on the tip of your tongue

> I need to mark the dispute evidence submitted on this chargeback
> — what's the signal?

Agent calls `describe_workflow("dispute")`, reads the signal
list, tells you it's `evidence.submitted`, asks if you want it
fired. You say yes; it signals and confirms. You never have to
open a `.bpmn` file or dig through `bpm_tasks/`.

### 7. A chargeback just landed and you're not at your desk

Before leaving: `wait_for_channel("webhooks:stripe",
filter={event_types: ["charge.dispute.created"]}, timeout=3600)`.
Phone buzzes when Claude comes back with the dispute details and
a suggested response. You decide from a coffee shop. (M5 with
HTTP transport.)

### 8. Revenue check across both sites without SQL

> Give me Site B ticket + merch revenue and Site A class-enrollment
> revenue for the last 7 days. Break out by product type.

Claude runs `list_orders` on the Site B server, `list_orders` +
`list_enrollments` on the Site A server, aggregates client-side,
hands you a table. Two MCP connections, one conversation. The
sites stay strictly separated; the *agent* is the bridge.

### 9. Emergency: flip the shop off and tell me what was in flight

> Turn the shop off, tell me every cart with items in it right now,
> and anyone mid-checkout.

Agent calls `set_feature("shop", false)`, then
`list_abandoned_carts(since="now-1h")`,
`list_workflows(process_key="order", status="running",
started_after="now-15m")`. Returns a list of affected users with
emails + totals. You've got a triage list in 10 seconds.

### 10. Onboard a new admin without teaching them the codebase

A new person joins. Instead of a two-hour walkthrough of "here's
the shop orders page, here's the stripe review queue, here's
workflows, here's how to refund…" you give them access to
Claude + the MCP servers and say "ask it." They type questions.
Claude translates them to the right tools. They learn by doing.
The `list_signals` + `describe_workflow` self-discovery is the
manual; they never have to read one.

---

## What this replaces

| Today | With MCP |
|---|---|
| SQL queries to answer "which orders are stuck" | `list_workflows(status="running", last_advanced_before=…)` |
| Tab-hopping between admin pages to reconstruct a timeline | `list_events(object_type, object_id)` — one call |
| Shelling into the VPS + `journalctl` | `tail_logs(service, level)` (gated) |
| Opening the Stripe dashboard for webhook history | `list_stripe_events(object_id=payment_id)` |
| Writing a one-off Python script for a batch operation | A loop Claude runs inline |
| Remembering signal names from `.bpmn` files | `list_signals(process_key)` at the prompt |
| Polling a page to see if a review came in | `wait_for_channel("stripe-reviews")` |

## What this does *not* replace

- The admin web UI. MCP is a parallel surface, not a replacement.
  Some tasks (reordering a gallery, inspecting an image) are
  still better with a mouse.
- Judgment calls. Claude surfaces information and executes
  confirmed actions; it doesn't decide "is this dispute worth
  fighting."
- Cold-wake. MCP only works while a session is open. Email /
  Slack alerts still own the "2am chargeback" path.
- Consumer-facing AI. WebMCP is a separate plan (and a separate
  can of worms).

## The bet

We're betting that once the infrastructure exists, the habits
flip: admin work starts in Claude, only falls back to clicking
when something is genuinely visual. Same way engineering work
already starts in Claude Code and only falls back to the IDE
when it's a big refactor.

If the bet is right, we get faster ops + a better audit trail
(every MCP call is logged with actor, args, result) + a hiring
tool (new admins get productive in a day).

If the bet is wrong, we've still built a strict service layer
that the web routers need anyway, and a parity gate that tells
us when our surfaces drift. The "worst case" of MCP is a better
codebase.
