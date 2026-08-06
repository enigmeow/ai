# Multi-party project management — a domain spec

> **Status:** a spec distilled from a shipped, production project-management
> SaaS: a services business that runs client projects (renovations, launches,
> events, weddings, fundraisers) and gives clients and subcontractors their own
> windows into the work.
> **Stack of the reference implementation:** FastAPI + SQLAlchemy + MySQL,
> vanilla-JS frontend, BPMN workflow engine underneath every lifecycle.
>
> ### What this document is
>
> Two things are specified here, and they are worth separating.
>
> **The domain** — §3 through §11 — is a working project-management system:
> tasks that carry cost and payment state, derived urgency, an auto-narrated
> activity log, the board / Gantt / calendar / inbox views, a four-total money
> model, invoicing, contractors, and the completion→testimonial loop. §4 is the
> task system specifically; it is not assumed to be obvious.
>
> **The hard part** — §2 — is the authorization model, and it is what transfers
> furthest:
>
> **Three or more parties with genuinely different trust levels, collaborating
> on one shared object, where the expensive failure is a leak between them.**
>
> Staff see everything. Clients see their project — but only one of them may
> touch money. Contractors see *their slice and nothing else*: not the budget,
> not the client, not the other contractors. That asymmetry is the design
> problem the domain exists to make concrete, and §2.2 is the answer.
>
> Read §2 even if you are building something that is not project management —
> an agency portal, a legal case system, a healthcare provider network, a
> multi-vendor marketplace with shared orders. And read §4.7 for what it looks
> like when that model is applied to one party and forgotten for another: the
> reference implementation leaks per-task costs to client contacts who should
> not see them.
>
> **Companion:** `docs/bpm.md`. Every lifecycle referenced here (project, task,
> contract, expense, invoice, testimonial) is a BPMN workflow, and the
> parent/child orchestration is that document's §6.4.4. This spec does not
> re-explain the engine.

---

## 1. The shape of the problem

### 1.1 The four parties

| Party | Sees | Money it sees | Never sees |
|---|---|---|---|
| **Management** | Everything. Accepts intake, creates projects, approves expenses, edits budgets, assigns roles, approves public content, manages the contractor registry | All of it, including revenue | — |
| **Team member** | Projects they are assigned to: tasks, hours, expenses, client + contractor messaging | Project costs | Projects they are not on |
| **Client** | Their project: tasks, messages, contracts, invoices, budget summary | Budget, spend-on-their-behalf, the fee, what they've been invoiced and paid | Internal notes, team-only threads, other clients |
| **Contractor / vendor** | Their own contracts, a stripped project timeline, one team-only thread | **Their own contract value and invoices — nothing else** | The budget, the fee, the client's identity, other contractors, internal notes |

The money column is not decoration: it is the axis that most often gets designed
by accident. What each party may see depends on the revenue model, so settle
that before the schema — §5.0.

The client party splits again: exactly one **financial contact** per project may
see and act on money; every other client contact is **standard**. That
distinction is per-project, not global — the same human can be the financial
contact on one project and a standard contact on another.

### 1.2 The end-to-end journey

```
marketing visit → consultation request → staff accepts
   → project created + client invited
   → client dashboard  ┐
   → internal ops runs the work (board / Gantt / calendar)
   → expenses + labor logged, budget moves
   → contracts issued → contractors onboard → contractor portal
   → invoices issued → paid online
   → final invoice paid → project completes
   → testimonial unlocked → approved → published
   → project highlight blog post
```

Two things about that chain are worth copying:

1. **It is one continuous loop, not two products.** The marketing site's only
   job is to feed the funnel; the completed project feeds testimonials and
   highlights back to the marketing site. Most builds treat "the website" and
   "the app" as separate systems and lose the loop.
2. **Every arrow is a state transition owned by a workflow**, not by whichever
   endpoint happened to fire. See `docs/bpm.md` §1.

---

## 2. The authorization model

This is the part worth stealing.

### 2.1 Two axes: global role × per-project role

A user carries **global roles** — who they are on the platform:

```
management | team_member | client | vendor    (+ whatever your platform already has)
```

And, separately, **per-project roles** on a membership row — what they are on
*this* project:

```
lead | secondary_lead | team | client_financial | client_standard | vendor
```

The global role answers *"is this person a client somewhere?"*. The project role
answers *"is this person the financial contact on THIS project?"*. Collapsing
them into one enum is the mistake this design exists to avoid: you would need a
`client_financial_project_47` role, or a side table that is a membership row
wearing a disguise.

The resolver is one function, and it carries a deliberate wrinkle:

```python
def user_project_role(db, user, project_id) -> str | None:
    """The user's active project_role, or None. Staff implicitly have full
    access and return the sentinel role 'staff'."""
    if any(user.has_role(r) for r in ("admin", "management")):
        return "staff"                      # <- sentinel, not a membership
    m = (db.query(ProjectMembership)
           .filter(ProjectMembership.project_id == project_id,
                   ProjectMembership.user_id == user.id,
                   ProjectMembership.is_active.is_(True))
           .first())
    return m.project_role if m else None
```

**The `"staff"` sentinel is load-bearing.** Management does not get membership
rows on every project — that would be thousands of rows and a permanent
consistency problem. They short-circuit to a role name that no membership can
produce, so every downstream gate can write `role in ("staff", …)` and staff
access falls out for free.

### 2.2 The idea: default-deny by *non-membership*

Contractors are the most dangerous party — external, least trusted, and the one
whose leak (budget, client identity, competitor rates) is most expensive.

The tempting design is to give them a membership row with
`project_role = "vendor"` and then exclude that role at each sensitive endpoint.
**That design is one forgotten exclusion away from a breach**, and the forgotten
one will be an endpoint added six months later by someone who never read this
document.

Instead:

> **Contractors hold no membership row at all.**
> Their identity is `Vendor.user_id`.
> Their authority is `Contract.vendor_id`.

Because they hold no membership, `user_project_role()` returns `None` for them,
so **every endpoint gated on membership 403s a contractor by construction** —
including endpoints that do not exist yet. Security becomes a property of the
data model rather than a checklist applied per route.

Contractors then get a **separate API surface** scoped by a different edge:

```python
def vendor_can_access_project(db, vendor, project_id) -> bool:
    """Authority = the vendor holds at least one contract on this project."""
    return db.query(Contract.id).filter(
        Contract.project_id == project_id,
        Contract.vendor_id == vendor.id).first() is not None
```

`/vendor/*` endpoints resolve the caller's `Vendor` row, check contract
authority, and return **purpose-built narrow schemas**. The project timeline a
contractor receives carries task name, status and due date — nothing else. No
costs, no assignees, no client notes.

**The generalizable rule:**

> When a party must not see something, do not add an exclusion. Remove them from
> the relation that grants it, and give them a different relation that grants
> only what they should have.

Two supporting disciplines:

- **The team-only field never enters a contractor schema.** `internal_notes` on
  the vendor registry is not "excluded from the vendor serializer" — it is
  absent from every `/vendor/*` schema, so it cannot leak through a serializer
  refactor.
- **Prove it with negative tests.** The reference implementation has a
  ten-test isolation gate that asserts contractors receive 403 from the
  membership-gated endpoints. Those tests are the specification of the boundary;
  see §12.

### 2.3 The gate ladder

Six dependencies, ordered loosest to tightest. Every endpoint picks exactly one.

| Gate | Passes | Use for |
|---|---|---|
| `require_management` | global `management` \| `admin` | Budgets, approvals, role assignment, public content |
| `require_team_member` | staff \| global `team_member` | Internal surfaces not scoped to one project |
| `require_team_member_on_project` | staff \| `lead` \| `secondary_lead` \| `team` **on this project** | Per-project internal actions |
| `require_project_member` | anyone with an active membership | Shared surfaces: view project, message thread |
| `require_financial_contact` | staff \| `client_financial` | Invoices, payment, anything money |
| `require_vendor` | global `vendor` **and** a registry profile | The `/vendor/*` surface |

Two of these encode bugs that were found and closed:

- **`require_team_member_on_project` exists because `require_team_member` is not
  enough.** A global `team_member` who is not on project X could otherwise read
  project X. Global role means "is internal staff", not "is on this project" —
  the per-project check is a separate question and needs a separate gate.
- **`require_vendor` checks the role *and* the profile.** Holding the global
  role with no registry row is a half-provisioned account, and it should fail
  closed.

### 2.4 Audience-scoped communication

One thread model serves three trust boundaries:

```
ProjectThread.audience ∈ ("client_visible", "team_only", "vendor")
ProjectThread.vendor_id            # set ONLY when audience = "vendor"
```

- `client_visible` — client ↔ team. The default.
- `team_only` — internal. Invisible to clients and contractors.
- `vendor` + `vendor_id` — team ↔ *one specific contractor*. Invisible to the
  client and to every other contractor.

The `vendor_id` scope is what makes this safe: without it, one `vendor`-audience
thread per project would be a shared room where contractors read each other's
messages. **Audience alone is not a boundary when a party has multiple
mutually-untrusting members.** Scope to the individual.

---

## 3. The domain model

Table names below are unprefixed for readability. The reference implementation
prefixes every table (`pm_projects`, `pm_tasks`, …) so a shared database can host
several products — worth doing from the start if that is remotely likely.

All ids are UUID strings. All money is **integer cents**, never float, never
`Decimal` at the boundary. Hours are `Numeric(6,2)` — the one place a decimal is
right, because humans log "1.25 hours".

### 3.1 Project

```
projects
  id, name, location, project_type, status, deadline
  client_overview          TEXT   -- team-written, shown to the client
  budget_total_cents       BIGINT   -- money that flows THROUGH to vendors
  management_fee_cents     BIGINT   -- revenue, charged ON TOP (§5.0)
  committed_cents          BIGINT
  spent_cents              BIGINT
  pm_user_id               -> users
  secondary_lead_user_id   -> users
  consultation_id          -> consultations   (nullable: projects can be created directly)
  testimonial_unlocked     BOOL
  testimonial_token        VARCHAR(36)        -- minted at completion, §10
  created_at
  INDEX (status, deadline)                    -- the sweep's query, §9.3

status ∈ (intake, active, wrapping, complete, archived, cancelled)
```

`(status, deadline)` is indexed as a pair because the automation sweep's hot
query is exactly *"wrapping projects whose deadline has passed."* Index the
sweep's predicate, not just the columns.

### 3.2 Membership

```
project_memberships
  id, project_id, user_id (NULLABLE), project_role, is_active
  invited_at, revoked_at
  INDEX (project_id, is_active), INDEX (user_id, is_active)
```

Two deliberate choices:

**`user_id` is nullable.** A membership can exist before a human does — the
placeholder seat, §7.1.

**Revocation is `is_active = False`, never a delete.** Membership is history:
who could see this project in March matters after they lose access in April.
Every query filters `is_active.is_(True)`; every index leads with it.

### 3.3 Task

```
tasks
  id, project_id, name, assignee_user_id, status, due_date, sort_order
  expected_cost_cents      -- budgeted
  actual_cost_cents        -- real
  deposit_cents            -- partial payment made
  paid_in_full             BOOL
  vendor_id                -> vendors        (nullable)
  client_note              TEXT              -- client-readable
  completed_at                               -- set on →done, CLEARED on reopen
  INDEX (project_id, status), INDEX (project_id, due_date)

status ∈ (todo, in_progress, blocked, done)
```

`blocked` is a first-class status, not a flag. In services work "waiting on
someone else" is the single most common state and the one clients most want to
see.

`completed_at` is **cleared when a task reopens**. A completion timestamp that
survives reopening silently corrupts every duration metric downstream.

### 3.4 Two kinds of writing on a task

Distinguishing these is a small idea that pays continuously:

```
task_activity        -- APPEND-ONLY log. System + human events.
  task_id, actor_user_id, body, created_at
  e.g. "Florist contracted — Acme Florals, deposit paid"

task_notes           -- CONVERSATION. Threaded, human, replyable.
  task_id, author_user_id, body, time_sensitive BOOL, created_at
```

An activity log is a record; a note is a conversation. Merging them produces a
feed that is simultaneously too noisy to read and too lossy to audit.

`time_sensitive` is a **one-bit escalation channel** — see §9.2.

### 3.5 Money entities

```
expenses
  project_id, task_id (nullable), submitter_user_id, amount_cents, description,
  status ∈ (pending, approved, rejected), decided_by_user_id, decision_note

invoices
  project_id, vendor_id?, contract_id?, task_id?, file_url?, number,
  status ∈ (draft, issued, paid, void), is_final BOOL, due_date,
  subtotal_cents, total_cents, amount_paid_cents,
  stripe_session_id, stripe_payment_intent_id, stripe_hosted_url,
  paid_at
invoice_line_items
  invoice_id, description, quantity, unit_cents

labor_logs
  task_id, member_user_id, hours NUMERIC(6,2), logged_on DATE, note
```

`is_final` is the flag the whole completion path hangs off (§10). An invoice
optionally references a vendor, a contract *and* a task so the money is
searchable from every direction it might be asked about.

**Invoices own their payment-processor columns directly** rather than reusing a
generic storefront payment table. The reference implementation shares a codebase
with an e-commerce product and still chose separate `stripe_*` columns on the
invoice, plus its own webhook endpoint. Invoice payment and cart checkout have
different lifecycles, different reconciliation, and different failure modes;
coupling them means every e-commerce change risks the invoicing path. Reuse the
*adapter*, not the *schema*.

### 3.6 Contracts, contractors, communication, notifications

```
contracts
  project_id, title, file_url?, docusign_url?, vendor_id?,
  value_cents?             -- NULLABLE: many contracts have no headline number
                           -- (NDAs, scopes priced per invoice) and 0 would lie.
                           -- The one money field a vendor may see, own only.
  status ∈ (pending, signed)
contract_comments
  contract_id, author_user_id, body

vendors                    -- the registry; a row can exist with no user
  business_name, contact_name, email, phone, service_category, service_area,
  internal_notes           TEXT   -- TEAM-ONLY. Never in a /vendor/* schema.
  user_id                  -> users, NULLABLE, UNIQUE   (see §12 G4)

vendor_invites
  token, vendor_id, contract_id, email, state, accepted_user_id, expires_at

project_threads            -- §2.4
project_messages
  thread_id, sender_id, body, time_sensitive, sent_at

notifications              -- §9.1
  user_id, kind, message, link, is_read, created_at
  INDEX (user_id, is_read, created_at)
```

Contracts support **a file upload, an e-signature link-out, or both**. Native
e-signature integration is a large project; a link-out plus a `signed` status
someone flips captures 90% of the value on day one. Do not let it block launch.

---

## 4. The task system

§3.3 gives the table. This is the system: how urgency is computed, who may
change what, how one task becomes three different objects depending on who is
looking, and which views the work is read through.

### 4.1 A task here is not a ticket

Issue trackers model *work to be triaged*. This models *work that costs money
and that a client is watching*. Concretely, a task carries four things a ticket
does not:

- **Two costs** — `expected_cost_cents` (budgeted) and `actual_cost_cents`
  (real). The gap between the sums of these across a project is the overrun
  signal (§5).
- **Payment state** — `deposit_cents` and `paid_in_full`, because in services
  work "done" and "paid for" are different events, often weeks apart.
- **A contractor link** — `vendor_id`, so the task is the join between the work,
  the money, and the person doing it.
- **A client-facing note** — `client_note`, distinct from internal discussion.

And `blocked` is a first-class status rather than a flag, because "waiting on
someone else" is the most common state in services work and the one clients most
want to see.

### 4.2 Derived urgency, never stored priority

There is no priority column. Urgency is computed at read time from the due date:

```python
RED_DAYS, YELLOW_DAYS = 14, 30

def urgency_for_deadline(due_date, today, *, done=False) -> str:
    """'red' ≤14 days (including overdue), 'yellow' ≤30 days, else 'none'.
    A done item or a missing date is always 'none'."""
    if done or due_date is None:
        return "none"
    days_left = (due_date - today).days
    if days_left <= RED_DAYS:    return "red"
    if days_left <= YELLOW_DAYS: return "yellow"
    return "none"
```

Three properties worth copying:

- **Derived, so it cannot go stale.** A stored priority is wrong the moment
  someone moves a date, and keeping it correct means a recalculation job that
  will itself drift. A derived value is always right and needs no machinery.
- **Overdue collapses into `red` rather than getting its own level.** Overdue and
  due-in-three-days demand the same response, so they get the same colour. Extra
  levels buy nothing an inbox count does not already say.
- **One function, every view.** The same helper colours tasks, projects and
  calendar entries. One urgency vocabulary across the whole product means a
  client and a project manager describe the same thing the same way.

Note the `done=` parameter rather than a separate branch at each call site: a
completed task is never urgent, and centralising that means no view can forget
it.

### 4.3 Who may change what

Three distinct permissions on one entity, and the split is the point:

| Action | Gate |
|---|---|
| Create / update / delete a task | `require_team_member_on_project` — staff or team **on this project** |
| Append an **activity** entry | Same — staff/team |
| Append a **note** | **Any active project member**, clients included |
| Read | Any active project member (but see §4.7) |

**Clients can participate without being able to change state.** They add notes,
they flag urgency, they cannot move a task to `done` or edit a cost. Most tools
force a choice between read-only clients (who then email you instead, defeating
the tool) and editing clients (who reorganise your board). A write-scoped note
channel is the middle path.

### 4.4 The auto-narrated activity log

This is the highest value-per-line feature in the domain. Every meaningful field
change on a task appends a **human-readable sentence** to the activity log:

```python
if body.name is not None and body.name.strip() != t.name:
    changes.append(f'Renamed to "{body.name.strip()}"')
if "due_date" in body.model_fields_set and body.due_date != t.due_date:
    changes.append("Due date cleared" if body.due_date is None
                   else f"Due date set to {body.due_date.isoformat()}")
if body.expected_cost_cents is not None and body.expected_cost_cents != t.expected_cost_cents:
    changes.append(f"Expected cost set to {_fmt_cents(body.expected_cost_cents)}")
if body.status is not None and body.status != old_status:
    changes.append(f"Status changed from {old_status} to {body.status}")
...
for line in changes:
    add_activity(db, task=t, actor_user_id=actor.id, body=line)
```

The client opens a task and sees a dated narrative — *"Due date set to
2026-08-14"*, *"Expected cost set to $2,400.00"*, *"Status changed from
in_progress to done"* — instead of a status field that silently changed at some
point. **An audit log becomes a client-facing progress story for free**, and the
single most common client question ("what's happened lately?") is answered
without anyone writing an update.

Three implementation details that make it work:

- **Compare before assigning.** Only actual changes are logged; a PATCH that
  re-sends identical values produces no noise.
- **Format money at write time.** The log line stores `"$2,400.00"`, not a cents
  integer, so rendering never has to know the line's shape.
- **The log is append-only and separate from notes** (§3.4). A record and a
  conversation are different things.

### 4.5 PATCH semantics: `fields_set` vs `None`

A partial-update trap worth stating explicitly, because it silently destroys
data:

```python
# WRONG — a PATCH that omits due_date is indistinguishable from one clearing it
if body.due_date is not None:
    t.due_date = body.due_date

# RIGHT — presence in the payload is the signal, not the value
if "due_date" in body.model_fields_set and body.due_date != t.due_date:
    t.due_date = body.due_date          # explicit null clears; omission leaves alone
```

The concrete failure: the "Done" checkbox PATCHes `{"status": "done"}` only.
Under the wrong version, `due_date` is `None` in the parsed model, and if you
treat `None` as "clear it" you wipe the date every time someone ticks a box.
**Any nullable, clearable field on a PATCH endpoint needs the `fields_set`
treatment** — otherwise "omitted" and "explicitly null" are the same thing.

### 4.6 Coupled invariants on transition

State changes must reset everything their old state justified:

```python
if body.status is not None and body.status != old_status:
    t.status = body.status
    t.completed_at = _now() if body.status == "done" else None   # stamp AND clear
    if body.status != "done":
        t.paid_in_full = False        # only meaningful for a completed task
    if body.status == "done":
        signal(f"task_lifecycle:{t.id}", "task_resolved")        # -> parent join
```

Both resets matter. A `completed_at` that survives a reopen corrupts every
duration metric downstream (§12 G7). A `paid_in_full` that survives a reopen
corrupts the distributed-money total (§5) — the task is no longer complete, so
"paid in full for the completed work" is not a claim the system can still make.

**The general rule: when a field is only meaningful in a given state, clear it on
the way out of that state, in the same transaction that changes the state.**

The workflow signal is best-effort — tasks created before the lifecycle existed
have no instance to signal, and that must not fail the update.

### 4.7 Three projections of one task

The same row is three different objects depending on who reads it:

| Reader | Sees | Via |
|---|---|---|
| **Team / staff** | Everything | Full summary schema |
| **Client** | Name, status, due date, urgency, client note, completion time, activity, notes | *(see the leak below)* |
| **Contractor** | **Name, status, due date. Nothing else.** | A dedicated three-field schema |

The contractor projection is a purpose-built schema:

```python
class VendorTimelineTask(BaseModel):
    """A timeline entry for the vendor — name/status/due_date ONLY."""
    name: str
    status: str
    due_date: date | None
```

It cannot leak, because there is nothing in it to leak. No assignee, no cost, no
client note, no vendor linkage to other contractors.

> **⚠ The client projection in the reference implementation does not do this.**
> The task-list endpoint is gated on *any* active membership and returns the
> full team schema — `expected_cost_cents`, `actual_cost_cents`,
> `deposit_cents`, `paid_in_full`, `vendor_id`. A `client_standard` contact
> therefore reads per-task costs and contractor links.
>
> Whether that is a **bug** depends on the revenue model (§5.0), and that is the
> lesson. Under markup it is a margin leak. Under pass-through plus a flat fee —
> which is what that business actually runs — it is the receipt, and defensible.
> Either way the code was not *deciding*; it reused the schema that already
> existed and answered the question by accident.
>
> The contractor path shows the contrast: a purpose-built narrow schema, safe by
> construction, because someone sat down and chose the three fields. **Same
> codebase, two approaches — one deliberate, one inherited.**
>
> The fix is a per-audience schema chosen by `user_project_role()` at
> serialization, not conditional field-blanking on a shared one — blanking is
> the exclusion-list pattern again and fails the same way. What goes *in* the
> client projection is a business decision, and §5.0 is where you make it.

**The rule this yields: a trust boundary needs its own schema, not a shared
schema with fields omitted at runtime.** If two audiences can read an endpoint,
two response models should exist.

### 4.8 The four views

All four read the same tasks and differ only in projection and grouping.

**Per-project board.** Tasks ordered by `sort_order`, then creation. Manual
ordering always — chronological or alphabetical ordering of a project board is
never what anyone wants. This is the client's and the team's default view.

**Cross-project Gantt.** Every live project as a bar: `start_date` derived from
`created_at`, end from `deadline`, coloured by project-level urgency, ordered by
deadline with undated projects last:

```python
.order_by(Project.deadline.is_(None), Project.deadline.asc())
```

That first clause is the whole trick for "undated items sort last" — without it,
NULL deadlines sort first in most databases and the view opens on the projects
that matter least.

**Calendar.** Project deadlines and open task due dates flattened into **one
shape** and sorted chronologically:

```
CalendarItem: date, label, kind ∈ (task | project), project_id, project_name, urgency
```

Two different entities become one list because the question — *"what is coming
up?"* — does not care which is which. Done tasks and undated tasks are excluded
at the query, not the renderer.

**Ops inbox.** The cross-project attention surface, and the only view that is not
task-shaped. It merges two feeds — project messages and task notes — into one
searchable, paginated stream with a `kind` discriminator and an inline reply
that routes back to the right place. Alongside it, one row per project with
`open_task_count`, `urgent_task_count`, `next_deadline` and project urgency.

That per-project row is what a manager actually opens the tool for: not "what
are all my tasks", but **"which of my projects needs me today?"** Build it early;
it is the difference between a task database and an operations tool.

### 4.9 Deliberately absent

| Not built | Why |
|---|---|
| Task dependencies / critical path | Services deadlines come from external commitments (a venue, a permit, a launch date), not from computed slack. A dependency graph is a large feature that would mostly restate what the due dates already say. |
| Subtasks | One flat list plus `sort_order` and the activity log covers it. Hierarchy doubles every query and every view. |
| Recurring tasks | Projects are finite; templates (§11) cover repetition across projects. |
| Per-task permissions | The trust boundary is the project, not the task. Per-task ACLs would be a second authorization model competing with §2. |
| Sprints, points, WIP limits, velocity | Deadline-driven work, not throughput-driven. Importing agile ceremony here measures nothing anyone acts on. |

The through-line: **this is a deadline-and-money system, not a throughput
system**, and every omission follows from that. If your domain is throughput —
support queues, manufacturing — invert most of these.

---

## 5. The money model

### 5.0 Decide the revenue model before you design any of this

This section originally opened with the four totals below, as though one money
vocabulary were enough. It isn't, and getting that wrong is expensive to undo,
so decide this first:

**Every figure in §5.1 is a *cost* — money flowing out to the people doing the
work. None of them is a *price*.** A services business also has revenue, and
where that revenue comes from decides who may see what:

| Revenue model | Consequence for the money views |
|---|---|
| **Markup on vendor costs** | Cost is commercially sensitive. A client who can see both budget and distributed can compute your margin. Client and vendor need genuinely different numbers per line of work, which means a **price axis** on the task alongside the cost axis. |
| **Flat fee on top** (pass-through costs) | Cost is *not* sensitive — showing the client what was spent on their behalf is the receipt, and exactly what a pass-through client should see. You need one fee field, not a price axis. |
| **Fee taken out of the budget** | The budget stops meaning one thing, and "remaining" silently mixes vendor money with your margin. Avoid unless a client contract forces it. |

The reference implementation chose **flat fee on top**:

```
Project.budget_total_cents      money that flows THROUGH us to vendors
Project.management_fee_cents    our revenue — charged ON TOP, never a slice
client_total = budget + fee     derived, never stored
```

Keeping the fee *out* of the budget is what lets `remaining` keep answering a
single honest question — *"how much of the build budget is left?"* — and makes
the fee a visible line rather than an invisible deduction.

**The failure this avoids.** Before the fee existed, that codebase had no price
concept at all: every field was `*_cost_cents`, and the client dashboard
rendered *Distributed Total* — money already paid out to vendors — to any
project member. Under a markup model that would have put the margin on the
client's own screen. It was survivable only because the intended model turned
out to be pass-through. **The visibility rules cannot be designed until the
revenue model is settled**, which is why this now comes first.

### 5.1 Four totals, four questions

The most-copied mistake in project software is a single `spent` number. Four
different people ask four different questions and each needs a different answer:

| Total | Formula | Answers |
|---|---|---|
| **Initial budget** | `budget_total_cents` | "What did we agree to?" |
| **Estimated** | `Σ task.expected_cost + Σ non-rejected expenses` | "What do we think it will cost?" |
| **Current** | `Σ task.actual_cost + Σ non-rejected expenses` | "What has it cost so far?" |
| **Distributed** | `Σ invoice.amount_paid + Σ (task.paid_in_full ? actual_cost : deposit)` | "How much money has actually left the building?" |

Estimated-vs-current is the overrun signal. Current-vs-distributed is the cash
position — work performed but not yet paid for. A project can be on budget and
out of cash, and one number cannot say so.

Note the deliberate asymmetries:

- **Rejected expenses are excluded; pending ones are included.** A submitted
  expense is a real commitment until someone says otherwise. Counting only
  approved expenses makes the estimate optimistic in exactly the situation where
  optimism is most expensive.
- **Distributed reads `paid_in_full ? actual_cost : deposit`** — a task either
  paid out fully or paid out a deposit, never both.
- **`spent_cents` is a denormalized cache** on the project, written by exactly
  one place: the expense-approval workflow handler. The full breakdown is
  computed on read. One writer, many readers.

`committed_cents` exists in the schema for contracted-but-unbilled work and is
not yet wired — an honest gap, noted so nobody assumes it is populated. Giving
`Contract` a `value_cents` is what makes it computable; without a contract
value there is nothing to commit.

### 5.2 The three money views

Same data, three audiences. **Derived per audience, not one view with fields
blanked at runtime** — blanking is the exclusion-list pattern from §2.2 and
fails the same way (§12 G13).

| Party | Sees |
|---|---|
| **Staff** | Everything: all four totals, the fee, and fee-accrued vs fee-invoiced |
| **Client** | Budget · spent on your behalf · remaining · **fee** · **client total** · invoiced · paid |
| **Vendor** | **Their own contract value and their own invoices.** Nothing about the project budget, the client, the fee, or any other vendor |

The vendor row is easy to get wrong in *both* directions. The reference
implementation initially showed vendors **no money at all** — not even their own
contracted amount, because `Contract` carried no value field. That is as wrong as
over-sharing: a contractor cannot see the number they signed up to. The fix was
one nullable `value_cents` on `Contract`, surfaced through the vendor's own
narrow schema.

**Nullable, not zero-defaulted.** Plenty of contracts are documents with no
headline number — NDAs, scopes priced per invoice — and `0` would be a lie about
those. Absent must stay distinguishable from zero.

---

## 6. Lifecycles

Every stateful entity is a BPMN workflow. Full engine spec in `docs/bpm.md`;
what matters here is the shape:

```
project_lifecycle  (PARENT)
  active ──[all_tasks_done]──▶ wrapping ──[final_payment_received]──▶ complete

  children, each an independent instance that signals up:
    task_lifecycle       (one per task)
    contract_signing     (one per contract)
    expense_approval     (one per expense)
    invoice_payment      (one per invoice)
    testimonial_review   (one per testimonial)
```

The two joins that define the product:

- **Every task done → project enters `wrapping`.** Computed by a plain
  `COUNT(*)` in a service function, which then signals the parent. Not a BPMN
  parallel join — the child count is unknown at design time (`docs/bpm.md` G20).
- **The final invoice paid → project `complete`**, which triggers the
  testimonial unlock (§10).

Manual status overrides by staff are **audit-ledger rows attached to the
lifecycle instance**, not silent column writes (`docs/bpm.md` §6.4.2). A
project-management tool without an answer to "who changed this and when" is a
liability; getting it for free from the workflow layer is most of why the
workflow layer is there.

---

## 7. Onboarding: invites as account-claim credentials

### 7.1 The placeholder seat

When a project is created from an accepted consultation, three rows are written
together:

1. The **project**.
2. An **inactive `client_financial` membership with `user_id = NULL`** — the
   seat exists, nobody is in it.
3. A **pending invite** to the client's email, carrying `project_role` and a
   14-day expiry.

On redemption, the placeholder is **bound and activated** rather than a new row
created. The seat was always there; a person moved into it.

This is worth the small complexity: the project is complete and describable from
the moment it exists ("the financial contact seat is unfilled"), rather than
having a hole that code must everywhere treat as a special case.

### 7.2 The invite is a credential — treat it like one

An invite link is the *only* thing standing between a stranger and an account.
Four rules, each of which was a real finding:

**1. Real entropy.** `secrets.token_urlsafe(32)` — 256 bits. Not a UUID, not a
counter, not a hash of the email.

**2. A NULL expiry is INVALID, not "never expires."**

```python
if invite.expires_at is None:
    invite.state = "expired"; db.flush()
    raise LookupError("invite expired")
```

The column is nullable, so some future call site *will* forget to set it. Fail
closed: an immortal account-claim token is precisely the thing you cannot allow
a forgotten line to mint.

**3. Redeeming to an existing password account must VERIFY the password, not
overwrite it.**

This was a live account-takeover bug. Redeeming an invite for an email that
already had an account logged the redeemer straight in — the password field was
ignored and a session minted. Anyone holding an invite link could take over the
matching account.

```python
user = db.query(User).filter(User.email == email).first()
if user is None:
    user = create_user(email, hash(password))            # new account
elif user.password_hash is not None:
    if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        raise InvitePasswordMismatch()                   # -> 403, NO session
    # correct password: link the project to the existing account
else:
    ...                                                  # see rule 4
```

The invite proves *the project should be linked*. It does not prove *who is
holding the link*. Those are different claims and only the password settles the
second.

**4. A password-less account may still be a real, owned account.** If it has an
OAuth credential, it belongs to someone — refuse and tell them to use the
matching sign-in method:

```python
if db.query(OAuthAccount).filter(OAuthAccount.user_id == user.id).first():
    raise InvitePasswordMismatch("sign in with Google to accept the invite")
user.password_hash = hash(password)     # genuinely unclaimed: safe to set
```

The mirrored guard exists on the OAuth path: redeeming via Google against an
email that has a password account returns 409, not a session.

**5. The UI must know before it asks.** `GET /invites/{token}` returns
`has_account: true` so the page can render "link to your existing account" with
a password prompt, instead of a signup form that confusingly rejects the email.

### 7.3 Three invite types

| Type | Grants | Tied to |
|---|---|---|
| **Project invite** | Global `client` + an active project membership in a named project role | A project |
| **Vendor invite** | Global `vendor` + links `Vendor.user_id` | **A contract** — not a project |
| **Staff invite** | A global staff role (`management` \| `team_member`) | Nothing; project-less |

The contractor invite being **contract-tied** is the §2.2 principle showing up
in onboarding: a contractor's existence in the system is justified by a specific
contract, so that is what mints their account.

---

## 8. The intake funnel

The marketing site exists to convert visitors into consultation requests. Three
details are worth copying:

**Conditional fields by project type.** Event-type projects reveal event-specific
questions (date, guest count, theme); build-type projects do not. They land in a
JSON `extra` column rather than a dozen mostly-null columns — a form whose shape
varies by a single enum should not shape the schema.

**Type-specific preparation notes in the confirmation email.** The confirmation
tells the requester *what to bring to the call*, keyed by project type:

> *Home renovation:* "Helpful to have ready: the scope of work (which rooms or
> areas), any permits already obtained, photos of the current state if possible,
> and your must-have vs. nice-to-have outcomes."

A static dict, one entry per type, with a sensible fallback. Costs an afternoon,
makes the first call materially better, and reads as competence — which is the
entire pitch of a project-management service.

**A calendar invite attached to the confirmation.** An `.ics` attachment for the
requested time. The meeting is on their calendar before a human has touched the
request.

Intake itself is a workflow: `consultation_intake` parks at a staff review task;
accepting runs the create-project-and-invite handler, declining marks it closed.
The consultation keeps its own status (`submitted → scheduled → accepted →
converted | declined`), and the resulting project back-references it — a project
can also be created directly, so the reference is nullable and points from
project to consultation, not the reverse.

---

## 9. Attention: notifications, escalation, cadence

Three distinct mechanisms, deliberately not unified.

### 9.1 In-platform notifications — polling-first

One row per recipient; a bell polls an unread count. **No websockets**, chosen
deliberately: the delivery guarantee needed here is "the user sees it within a
minute or two of opening the app", which polling satisfies at a fraction of the
operational cost.

Insertion is **best-effort at every call site**:

```python
def create_notification(db, *, user_id, kind, message, link=None) -> None:
    """Flushes, does NOT commit — the caller owns the transaction.
    A failure here must never break the triggering write."""
    try:
        db.add(Notification(...)); db.flush()
    except Exception:
        logger.exception("create_notification failed for user %s", user_id)
```

The rule from `docs/bpm.md` §1.2 law 7: **a failed notification must never roll
back the write that caused it.** Nobody's expense submission should fail because
a bell did not ring. `notify_management(...)` fans out to every management-role
holder, deduped, under the same guarantee.

Two details the reference implementation got wrong first and then fixed: the
badge must refresh its count immediately on mark-read (not on the next poll),
and it must **hide at zero** rather than render a `0`.

### 9.2 The `time_sensitive` bit

Both task notes and project messages carry one boolean. When set, the write also
emails the project manager.

This is a **one-bit escalation channel**, and its virtue is that it needs no
configuration: no priority taxonomy, no routing rules, no per-user preference
matrix. The author knows whether this is urgent, and the checkbox is the entire
interface. Priority schemes with five levels end up with everything at level one.

Escalation is best-effort and no-ops loudly (logged) when the project has no
assigned manager or the manager has no email.

### 9.3 Cadence is a sweep, not a timer

Recurring, clock-driven work runs in an hourly job that **emits workflow actions
and never mutates status directly**:

- **Bi-weekly status email** — every active project whose last update is >14 days
  old.
- **Auto-final-invoice** — a `wrapping` project past its deadline with no final
  invoice yet: bill `budget − Σ(non-void invoice totals)`. If that is ≤ 0 *and*
  everything billed is paid, signal completion instead.

The full reasoning for sweep-vs-BPMN-timer is `docs/bpm.md` §7.3. The short
version: a perpetual timer loop must have its parallel branch cancelled on every
terminal transition, which is the shape workflow engines handle worst. **A
notification cadence is operational machinery, not a state lifecycle.**

Two implementation requirements:

- **Sweep functions take an injectable `now`**, so they are unit-testable
  without freezing the clock globally. This single choice is why the automation
  has direct tests at all.
- **Each project is swept in its own try/except with a rollback**, so one bad
  project does not poison the batch.

The final-invoice sweep's three-way branch is the subtle part, and it is easy to
get wrong:

```
uninvoiced > 0                       -> issue the final invoice
uninvoiced <= 0 and fully PAID       -> signal completion
uninvoiced <= 0 and NOT fully paid   -> do nothing
```

That third branch matters: fully billed is not fully paid, and completing a
project on issued-but-unpaid invoices means writing off real money. Distinguish
`total_cents` (billed) from `amount_paid_cents` (received) everywhere they
appear.

---

## 10. The social-proof loop

Completion feeds marketing, automatically:

1. Final invoice paid → the completion handler sets `testimonial_unlocked` and
   mints a one-time `testimonial_token`.
2. A thank-you email goes out with a dashboard link and a
   `/testimonial?token=…` link.
3. Submission is **doubly gated**: the token resolves the project *and* the
   submitter must be a client member *and* the project must be in `wrapping` or
   `complete`. A token alone is not authorization.
4. Submission starts a review workflow → staff approve/reject.
5. Approved testimonials render publicly and on the homepage.
6. A blog post about the project can **link an approved testimonial** and carry
   before/after images.

The token is a *convenience credential* for the email link, never the sole
authority — the membership check runs regardless. Denormalize `author_name` and
`project_type` onto the testimonial at submission so public rendering never
joins back to user or project rows.

**Markdown rendering note:** the reference implementation renders post bodies
client-side as escaped paragraphs with no Markdown library — XSS-safe by
construction, at the cost of supporting only paragraphs. For staff-authored
content that trade is usually right; adopt a sanitizing renderer only when
someone actually needs tables.

---

## 11. Small ideas that punch above their weight

**Starter task templates per project type.** On creation, seed a curated
checklist for that project type — a wedding gets venue / catering / photographer
/ florist / officiant / seating chart / marriage license; a renovation gets
scope / permits / demolition / framing / plumbing / electrical / punch list. A
project manager starts from a populated board instead of a blank one.

It is a `dict[str, list[str]]` and an afternoon of domain research, and it is
probably the highest ratio of perceived-competence to effort in the entire
product. Tasks stay freely renameable and deletable — it is a starting point,
not a workflow.

**A client-readable note field, distinct from internal discussion.**
`Task.client_note` is the one sentence the client should read. Without it, teams
either write everything client-safe (losing internal candor) or nothing (losing
the client).

**One project-type enum, used everywhere.** The same tuple drives the intake
form, conditional fields, task templates, prep notes, the color system, blog
tags and testimonial tags. One tuple; everything keys off it.

**A resume/singleton-document pattern.** A single-row table holding a JSON blob,
edited in place. When you need exactly one editable document, a singleton row
beats both a config file (not editable in the UI) and a full CMS (a project).

---

## 12. Gotchas

**G1. A global "team member" role is not a per-project permission.** Gating a
project endpoint on the global role lets any internal user read any project.
Global role = "is internal"; membership = "is on this project". Two questions,
two gates (§2.3).

**G2. An invite proves the link, not the holder.** Redeeming to an existing
account must verify the password. This shipped as a live account-takeover.
(§7.2 rule 3.)

**G3. A nullable expiry column will eventually be NULL.** Treat NULL as expired,
not eternal. (§7.2 rule 2.)

**G4. MySQL allows multiple NULLs in a UNIQUE index — use it.** A unique index
on `vendors.user_id` enforces *one claimed profile per user* while leaving any
number of unclaimed registry rows (`user_id IS NULL`) unconstrained. Exactly the
semantics wanted, at zero cost.

Related: **resolve one-to-many defensively while duplicates may exist.** Before
that constraint, `current_vendor()` pinned resolution to the earliest profile by
`created_at` rather than letting `.first()` pick arbitrarily. An unordered
`.first()` over a set that *should* be unique is a nondeterminism bug waiting for
the second row.

**G5. MySQL returns naive datetimes even from `DateTime(timezone=True)`
columns.** Every comparison against `datetime.now(timezone.utc)` raises
`offset-naive vs offset-aware`. Normalize at the boundary:

```python
def _as_utc(dt):
    if dt is None: return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
```

This bites hardest in sweeps, where every predicate is a datetime comparison.

**G6. Billed is not paid.** `total_cents` and `amount_paid_cents` answer
different questions. Completing a project on issued-but-unpaid invoices writes
off real money. (§9.3.)

**G7. `completed_at` must be cleared when a task reopens**, or every duration
metric silently lies.

**G8. Rejected expenses out, pending expenses in.** A pending expense is a real
commitment; excluding it makes the estimate optimistic exactly when optimism is
most expensive. (§5.)

**G9. Audience is not a boundary when a party has mutually-untrusting members.**
A single `vendor`-audience thread per project is a shared room. Scope to the
individual with `vendor_id`. (§2.4.)

**G10. Services flush; routers commit.** Uniformly. A service that commits
breaks every caller that wanted to compose it into a larger transaction — and
under a workflow engine it corrupts serialized workflow state (`docs/bpm.md` G1).

**G11. Prove isolation with negative tests.** "The contractor cannot see the
budget" is only true if a test asserts a 403. The reference implementation's
ten-test isolation gate is the executable specification of §2.2; a security
review found no leak path *because the boundary was testable*.

**G12. Escape by context, not by habit.** Element text and HTML *attribute*
values need different escapes. Interpolating into `title="…"` with an
element-text escaper is a stored-XSS hole. Use distinct helpers (`esc` /
`escAttr` / `safeUrl`) and lint for attribute interpolation.

**G13. A shared response schema decides a policy question by accident.** Two
audiences reading one endpoint need two response models. The reference
implementation gave contractors a purpose-built three-field schema (safe by
construction) but let clients read the team's full task schema — per-task costs,
deposits, contractor links — because it was the schema that already existed.

The instructive part is what happened next. That looked like a straightforward
leak, and under a **markup** revenue model it would have been a serious one: the
client could compute the margin. But the business turned out to run on
**pass-through costs plus a flat fee**, under which showing a client what was
spent on their behalf is not a leak at all — it is the receipt, and arguably
required.

So the defect was never "clients can see costs". It was that **nobody had
decided whether they should**, and a schema reused for convenience made the
decision silently. Reusing a response model across a trust boundary does not
just risk over-sharing; it quietly answers a policy question that the business
has not answered yet. Two response models force the question into the open.
(§4.7, §5.0.)

**G14. Omitted and explicitly-null are not the same PATCH.** Use the parsed
model's set-fields, not `is not None`, for any nullable clearable field —
otherwise a partial update that omits the field wipes it. The "mark done"
checkbox erasing due dates is the canonical instance. (§4.5.)

**G15. Clear the fields a state justified when leaving that state**, in the same
transaction. `completed_at` and `paid_in_full` both survive a reopen if you
forget, corrupting duration metrics and the distributed-money total
respectively. (§4.6.)

**G16. Settle the revenue model before designing any money view.** Whether a
client may see costs is not a security question with a right answer — it depends
entirely on whether you mark costs up or charge a fee on top (§5.0). Build the
schema first and you will encode an answer by accident, then find months later
that the client dashboard has been computing your margin for you. Ask "where
does our revenue come from?" before the first money field exists.

**G17. A party can be under-shared as easily as over-shared.** The same
implementation that showed clients everything showed vendors **no money at all**
— not even the value of the contract they signed, because `Contract` had no
amount field. Isolation reviews reliably hunt for leaks and reliably miss the
inverse. For each party ask both "what must they not see?" *and* "what do they
need that they currently cannot get?" (§5.2.)

---

## 13. Build order

Each phase is independently shippable and de-risks the next.

| Phase | Ships | Gate |
|---|---|---|
| **0** | Marketing site + intake form + confirmation email | A stranger can request a consultation and gets a useful reply |
| **1** | Auth, the two-axis role model, gates, project shell | Accept intake → project + invite → client logs in and sees a dashboard |
| **2** | Client dashboard: tasks, notes, messages, contracts | A client can follow their project without emailing anyone |
| **3** | Internal ops: board/Gantt, members, labor, task templates | Staff run a project entirely in-app |
| **4** | Expenses + the four-total money model | Approving an expense moves the budget |
| **5** | Invoicing + online payment | Issue → pay → paid, end to end |
| **6** | Lifecycle automation (the sweep) | A wrapping project bills and completes itself |
| **7** | Contractor portal | The isolation gate is green (§12 G11) |
| **8** | Testimonials, notifications, highlights blog | Completion produces published social proof |

**Phase 1 is the one to get right.** The role model is the hardest thing to
change later — every endpoint written after it encodes its assumptions. If any
part of this spec deserves extra design time up front, it is §2.

Two ordering notes from the reference build:

- **The contractor portal came late and that was correct.** It is the strictest
  boundary, and it is far easier to add a party that holds no memberships than
  to retrofit isolation onto one you gave memberships to.
- **A "drift cleanup" phase is normal.** Earlier phases wrote status columns
  directly; a later retrofit moved them all onto workflows and closed the last
  silent mutation. Budget for it rather than being surprised by it.

---

## 14. What to leave out

Cut deliberately in the reference implementation, and right to cut:

| Not built | Instead | Why |
|---|---|---|
| Native e-signature integration | Upload + link-out + a `signed` status | Weeks of work for a status flip someone can do by hand |
| Real-time websocket notifications | Polling bell | Same perceived latency, a fraction of the ops burden |
| A true unread model for the ops inbox | "Recent 20 messages" | Per-user read state on every thread is a real feature; recency answers the actual question |
| A Markdown library | Escaped paragraphs, client-side | XSS-safe by construction; staff-authored content rarely needs tables |
| Per-user notification preferences | One `time_sensitive` bit | A preference matrix nobody configures is worse than a checkbox everyone understands |
| Time tracking with start/stop timers | Logged hours + a date | People reconstruct their day anyway |

The pattern: **ship the 90% version of the feature and the 100% version of the
boundary.** Contract signing can be a manual flip; contractor isolation cannot.

---

## 15. Companion specs

- `docs/bpm.md` — the workflow engine every lifecycle here runs on. §6.4.4 is
  the parent/child orchestration, §7.3 is sweeps-vs-timers, §1.2 is the laws
  this spec assumes throughout.
- `docs/mcp.md` — exposing the admin plane to an AI agent. A
  project-management platform is an unusually good fit: "which projects are
  wrapping with unpaid invoices?" is a question staff ask weekly and a query
  nobody wants to write twice.
