# Business Process Management (BPM) — a portable implementation spec

> **Status:** a self-contained spec for implementing a BPMN workflow engine at
> the core of a product, extracted from a system that has been running in
> production since 2026-04.
> **Engine:** SpiffWorkflow 3.1.2, in-process with FastAPI, persisted to MySQL.
> **Proven at:** 34 BPMN processes across 4 sites (15 / 10 / 7 / 2), 25 test
> modules, ~2,700 lines of service-task handlers.
>
> ### About this document
>
> This is a spec distilled from a production system, not a proposal. Every rule
> in it is grounded in shipped code, and most of §12 is traceable to a specific
> incident. Source paths (`app/core/bpm/service.py`,
> `app/sites/<site>/bpmn/…`) describe the reference implementation's layout and
> are worth mirroring, but nothing here depends on that layout.
>
> The reference implementation is a monorepo serving **four independent
> products** from one FastAPI/MySQL codebase. Where per-product detail matters
> they are referred to as:
>
> | Label | Domain |
> |---|---|
> | **Site A** | Education / training — instructors, courses, videos, assessments, bookings, gear |
> | **Site B** | Events + merch storefront — performers, shows, products, orders, payouts |
> | **Site C** | Project-management SaaS — projects, tasks, contracts, expenses, invoices |
> | **Site D** | A browser-extension product with no workflow surface |
>
> **What is portable vs. what is context.** §1 (the laws), §4 (data model),
> §5 (engine modules), §6 (process shapes), §7 (integration), §10 (testing),
> §12 (gotchas) and §13 (rollout) are the transferable spec — they assume
> nothing about the product. §2 (engine choice), §8 (multi-tenant layout) and
> §14 (inventory) are context from the reference implementation; read them for
> the reasoning, not as requirements.

---

## 0. How to use this document

Two audiences:

**Working in a codebase that already has this?** §3 (architecture), §6
(authoring conventions), §7 (web integration) and §12 (gotchas) are the working
set. §14 is the inventory of the reference implementation.

**Re-implementing BPM in a new product?** Read straight through: the five tables
(§4), the engine's load-bearing code (§5), the BPMN templates for each canonical
process shape (§6), the integration surfaces (§7), the test strategy (§10), and
— the part that costs the most to rediscover — the gotchas (§12). §13 is a
phased rollout order.

This is a **design spec with the hazards mapped, not a drop-in implementation.**
An unbriefed reviewer rebuilt the engine from it and shipped a working system
with 67 passing tests, but had to supply the HTTP/auth layer, the user and role
model, and one schema column of their own (§5). Expect to write real code from
these shapes; expect §12 to save you weeks. §14.4 lists what that exercise
found — five silent defects, all now corrected in the text.

The spec is stack-flavored (Python / FastAPI / SQLAlchemy / MySQL /
SpiffWorkflow) but the design is portable. The parts that generalize are §1
(the laws), §4 (the data model), §6 (process shapes) and §12 (gotchas) — a
Java/Camunda or Node/Zeebe implementation would keep all four and swap §5.

**The single most important section is §12.** Every gotcha in it was a
production incident or a multi-hour debugging session. A re-implementation
that reads only §1–§11 will rediscover all of them.

---

## 1. The thesis, and the laws that follow from it

### 1.1 The thesis

**Every business object with a meaningful lifecycle is a BPMN process.** Not a
`status` column mutated from wherever the code happens to be; not a scattering
of `if order.state == "paid"` branches. One executable diagram per object type,
authored in a modeler, committed to the repo, executed by the engine, and
audited on every token movement.

The owner's formulation: *"nothing is fire-and-forget that doesn't need to
be."* A state transition and its side effects must be **durable, sequenced,
audited, and single-fire**.

What that buys, concretely:

- **One place to look.** "How does an order get canceled?" is answered by
  opening `order.bpmn`, not by grepping for `.state =`.
- **A free audit trail.** `state_transitions` answers "what happened to this
  object, in what order, triggered by whom" with one indexed query — for every
  object type, without per-feature logging code.
- **Crash-safety by construction.** The workflow is serialized to the DB after
  every step. A process restart mid-lifecycle resumes exactly where it stopped.
- **Timers and races you'd otherwise never build.** "Auto-cancel an unpaid
  order after 30 minutes, unless payment lands first, unless an admin cancels
  first" is four lines of XML instead of a cron job plus three guard clauses.
- **The diagram is the spec.** Non-programmers can read the lifecycle.

### 1.2 The laws

These are the rules the codebase actually enforces. Break one and the system
degrades to "a state column with extra steps."

1. **Routers emit signals; they never mutate state.** A router's job is
   authorization, validation, and `wf.signal(...)` / `wf.start(...)`. The
   workflow owns the transition and its side effects. (§7.1)
2. **Every side effect of a transition lives in the service-task handler**,
   not in the router that triggered it. If publishing a post sends email and
   purges the CDN, both happen in `svc_publish_post` — so they also happen when
   the transition is triggered by a timer, a webhook, an admin, or an MCP agent.
3. **Service-task handlers flush, never commit.** The caller owns the
   transaction. A mid-handler `commit()` corrupts the serialized workflow
   state — the workflow is persisted *after* the handler returns, so a commit
   inside it publishes a half-stepped workflow. (§12, G1)
4. **The `status` column on the domain row is a denormalized cache, not the
   source of truth.** It exists so `SELECT ... WHERE state='published'` works
   without deserializing a workflow. The workflow is authoritative; the column
   is written only by handlers.
5. **Arbitrary / repeated / manual transitions are ledger rows, not graph
   edges.** The engine can't model an unbounded "any state → any state" mesh
   (§12, G7). Those transitions are `state_transitions` rows attached to the
   object's running instance — still audited, still attributed, just not
   token movements. (§6.4.2)
6. **Clock-driven *cadence* is an operational sweep; clock-driven *state* is a
   BPMN timer.** "Auto-cancel after 30 min" is a timer inside the process.
   "Email a status update every two weeks forever" is a sweep that *emits* BPM
   actions and never touches status. (§7.3)
7. **The only deliberately fire-and-forget things are best-effort
   notifications.** A failed email must never roll back the write that
   triggered it. Everything else is durable.
8. **A workflow that can't start must not break the entity.** The domain row is
   the source of truth for existence; the workflow is the lifecycle anchor.
   `start_lifecycle` swallows and logs. (§7.1.3)

### 1.3 The documented exceptions

Honesty about where BPM-everywhere does *not* apply, so nobody relitigates:

| Not a workflow | Why |
|---|---|
| Notification/email cadence (bi-weekly digests) | Operational machinery, not a state lifecycle. A perpetual timer loop needs its parallel branch cancelled on every terminal transition — exactly the shape the engine handles worst. Use a sweep. |
| Per-member archive/unarchive of a shared object | Unbounded, repeatable, per-actor. Ledger rows. (§6.4.2) |
| Idempotent GET-side reloads / cache warms | No state change. |
| Pure CRUD with no lifecycle (a tag, a saved address) | Nothing to model. Don't force it. |

---

## 2. Engine choice

### 2.1 Why SpiffWorkflow

| Criterion | SpiffWorkflow | Camunda Platform |
|---|---|---|
| Language | Pure Python | Java (separate JVM service) |
| Runs where | **In-process with FastAPI** | Separate service + its own DB |
| BPMN 2.0 / DMN | Yes / Yes | Yes / Yes |
| Ops burden (solo dev) | ~zero — `pip install` | Real — JVM, broker, second DB |
| Modeler | Camunda Modeler (standard `.bpmn`) | Same |
| Scale ceiling | Single-process Python throughput | Multi-node, clustered |
| License | LGPL (library) | CE Apache 2 / C8 source-available |

For a FastAPI + MySQL stack run by a small team, SpiffWorkflow wins on the one
axis that matters most: **it is a library, not a service**. There is no second
process to deploy, monitor, secure, or keep version-matched. The workflow
transaction is *the same database transaction* as the domain write, which
eliminates an entire class of distributed-consistency bugs (see §5.3).

The escape hatch is real: the `.bpmn` files are standard BPMN 2.0. Moving to
Camunda/Zeebe later means rewriting §5 and keeping §6.

### 2.2 What we deliberately do NOT use

- **`bpmn:serviceTask` + Spiff's `call_service` API.** That surface churned
  hard across Spiff 1.x → 3.x. We use `bpmn:scriptTask` whose entire body is
  `_dispatch("svc_name")`, which keeps us on `PythonScriptEngine` — the most
  stable code path in the library. Everything reads as a service task; only the
  XML tag differs. (§5.2)
- **Spiff Arena.** We render our own admin UI; Arena is a whole platform.
- **DMN decision tables.** Available, unused. Gateways have sufficed.
- **BPMN sub-process call activities.** Parent/child is modeled as two
  *independent top-level instances* that signal each other (§6.4.4), which is
  more robust to versioning and lets a child outlive/precede a parent.

---

## 3. Architecture

### 3.1 Runtime shape

```
┌─────────────────────────────────────────────────────────────────────┐
│ FastAPI (uvicorn)                                                   │
│                                                                     │
│  Routers ─────┐                                                     │
│  Webhooks ────┼──▶ WorkflowService ──▶ SpiffWorkflow (in-process)    │
│  Sweeps ──────┤      start / signal / complete_user_task / cancel    │
│  MCP tools ───┘      │        │            │                        │
│                      │        │            └─▶ RegistryScriptEngine  │
│                      │        │                  └─▶ @service_task   │
│                      │        │                        handlers      │
│                      │        └─▶ audit.record()                     │
│                      └─▶ serialize ↔ MySQL                           │
│                                                                     │
│  APScheduler (60s) ──▶ timer tick ──▶ refresh_waiting_tasks()        │
│  APScheduler (site) ─▶ operational sweeps ──▶ emit BPM actions       │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                   MySQL: process_definitions, process_instances,
                          workflow_tasks, state_transitions,
                          ai_invocations, + domain tables
```

No broker, no second service, no new language. One `pip install SpiffWorkflow
APScheduler`, five tables, six engine modules.

### 3.2 Repository layout

```
backend/
├── main.py                        # startup: sync_definitions + timer.start + site on_startup
└── app/
    ├── core/                      # site-agnostic; must never import a site package
    │   ├── bpm/
    │   │   ├── registry.py        # @service_task decorator + name→callable map
    │   │   ├── engine.py          # Spiff parser/serializer + RegistryScriptEngine
    │   │   ├── service.py         # WorkflowService — THE public API
    │   │   ├── loader.py          # scan bpmn/*.bpmn → process_definitions
    │   │   ├── audit.py           # state_transitions writer
    │   │   ├── retry.py           # transient-failure return-value protocol
    │   │   └── timer.py           # APScheduler 60s tick
    │   ├── bpmn/                  # cross-site process definitions
    │   ├── bpm_tasks/             # cross-site service-task handlers
    │   ├── models/
    │   │   ├── workflow.py        # ProcessDefinition, ProcessInstance, WorkflowTask, AIInvocation
    │   │   └── state_transition.py
    │   └── routers/
    │       ├── workflows.py       # /api/workflows — admin ops + per-object read
    │       └── tasks.py           # /api/tasks — user-task inbox
    └── sites/<site>/
        ├── bpmn/                  # site-specific .bpmn (shadows core by process_key)
        ├── bpm_tasks/             # site-specific handlers
        └── services/orchestration.py   # (optional) parent/child helpers
```

For a **single-product** implementation, collapse `core/` + `sites/<site>/`
into one tree — `app/bpm/`, `app/bpmn/`, `app/bpm_tasks/`. The core/site split
(§8) only earns its keep when several products share one codebase.

---

## 4. Data model

Five tables. Four are the engine; the fifth is for AI service tasks (§9).

```sql
-- 1. Deployed process definitions. Source of truth is the .bpmn file in the
--    repo; this table is synced on every startup. A content-hash change
--    appends a new version. Running instances stay pinned to their version.
CREATE TABLE process_definitions (
  id            INT PRIMARY KEY AUTO_INCREMENT,
  process_key   VARCHAR(100) NOT NULL,     -- 'blog_post', 'order'
  version       INT          NOT NULL,
  bpmn_xml      MEDIUMTEXT   NOT NULL,
  bpmn_hash     CHAR(64)     NOT NULL,     -- sha256 of the xml
  deployed_at   DATETIME     NOT NULL,
  UNIQUE KEY uk_key_version (process_key, version),
  INDEX idx_key (process_key)
);

-- 2. One row per object lifecycle. Holds the serialized SpiffWorkflow.
CREATE TABLE process_instances (
  id                     BIGINT PRIMARY KEY AUTO_INCREMENT,
  process_definition_id  INT          NOT NULL,
  -- MUST exceed process_key + 1 + object_id, or you re-commit the exact
  -- DataError 1406 -> poisoned-session cascade that §4.1/G16 exist to prevent.
  business_key           VARCHAR(255) NOT NULL,   -- '<process_key>:<object_id>'
  object_type            VARCHAR(50)  NOT NULL,
  object_id              VARCHAR(100) NOT NULL,   -- see the width warning below
  status                 VARCHAR(20)  NOT NULL,   -- running|completed|error|canceled
  serialized_state       LONGTEXT     NOT NULL,   -- SpiffWorkflow JSON
  current_states         JSON         NULL,       -- denormalized active task names
  started_at             DATETIME     NOT NULL,
  updated_at             DATETIME     NOT NULL,
  completed_at           DATETIME     NULL,
  FOREIGN KEY (process_definition_id) REFERENCES process_definitions(id),
  INDEX idx_business_key (business_key),
  INDEX idx_object (object_type, object_id),
  INDEX idx_status (status, updated_at)
);

-- 3. Denormalized projection of ready user tasks, for fast inbox queries.
--    Rebuilt from the serialized workflow after every step.
CREATE TABLE workflow_tasks (
  id                   BIGINT PRIMARY KEY AUTO_INCREMENT,
  process_instance_id  BIGINT       NOT NULL,
  task_spec_name       VARCHAR(200) NOT NULL,     -- the BPMN task id
  task_type            VARCHAR(20)  NOT NULL,     -- user|service|timer|script
  status               VARCHAR(20)  NOT NULL,     -- ready|completed|canceled|error
  assignee_user_id     VARCHAR(36)  NULL,
  assignee_role        VARCHAR(50)  NULL,
  due_at               DATETIME     NULL,
  created_at           DATETIME     NOT NULL,
  completed_at         DATETIME     NULL,
  completed_by_user_id VARCHAR(36)  NULL,
  form_data            JSON         NULL,
  error_message        TEXT         NULL,
  FOREIGN KEY (process_instance_id) REFERENCES process_instances(id),
  INDEX idx_instance (process_instance_id, status),
  INDEX idx_user_inbox (assignee_user_id, status),
  INDEX idx_role_inbox (assignee_role, status)
);

-- 4. Global audit. Every token movement, every signal, every manual override.
CREATE TABLE state_transitions (
  id                  BIGINT PRIMARY KEY AUTO_INCREMENT,
  process_instance_id BIGINT       NOT NULL,
  object_type         VARCHAR(50)  NOT NULL,
  object_id           VARCHAR(100) NOT NULL,
  task_spec_name      VARCHAR(200) NULL,
  event               VARCHAR(64)  NOT NULL,   -- started|task_started|task_completed|
                                               -- signal|error|canceled|ended|<custom>
  from_state          VARCHAR(100) NULL,
  to_state            VARCHAR(100) NULL,
  actor_user_id       VARCHAR(36)  NULL,
  reason              TEXT         NULL,
  metadata            JSON         NULL,
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (process_instance_id) REFERENCES process_instances(id),
  INDEX idx_object (object_type, object_id, created_at),
  INDEX idx_instance (process_instance_id, created_at),
  INDEX idx_actor (actor_user_id, created_at)
);
```

### 4.1 Design notes that are load-bearing

**`object_id` is `VARCHAR(100)`, and all three tables must agree.** It is
tempting to make it `VARCHAR(36)` for a UUID. Don't. Real systems key
lifecycles on **composite** ids — Site A's CRM keys relationship events on
`"{instructor_id}:{student_id}"` (73 chars), and the stripe-review workflow
packs an entity prefix plus a content hash into it. At 36, every such write
raised MySQL `DataError 1406`, which then poisoned the SQLAlchemy session with
`PendingRollbackError` — and because the writer was the 60-second timer sweep,
it produced a full traceback every minute forever (§12, G16 and G14). `process_instances`,
`state_transitions` and `ai_invocations` all carry the pair and must all be
widened together.

**`business_key` must be wider than its two components combined.** It is built
as `f"{process_key}:{object_id}"` from a `VARCHAR(100)` and a `VARCHAR(100)`, so
`VARCHAR(100)` cannot hold it — up to 201 characters are possible. Shipping it
at 100 re-creates, in the very schema presented as the fix, the `DataError 1406`
→ `PendingRollbackError` cascade described two paragraphs above. Latent only
while keys stay short. Size it at 255.

**`business_key` is NOT unique.** The
original design had a unique constraint. It was dropped deliberately: an object
can legitimately have a *sequence* of lifecycles over time (a gear item that is
acquired, sold, re-acquired). Uniqueness is enforced at the *behavior* level —
`WorkflowService.start` refuses when a **running** instance already exists for
`(object_type, object_id)` — while history is preserved. Readers use
`get_instance()`, which prefers the running instance and falls back to the most
recent terminal one.

**`current_states` is a denormalized JSON array of active task-spec names.** It
answers "show me every order waiting on payment" without N deserializations —
but note it is a **JSON column and therefore not directly indexable** in MySQL.
If that query becomes hot, add a generated column over the JSON path and index
that; don't assume the array is doing index work it can't do.

**Index names must be unique per *schema*, not per table.** `idx_object` on both
`process_instances` and `state_transitions` is legal MySQL and fatal on
PostgreSQL and SQLite. Prefix them (`idx_pi_object`, `idx_st_object`) from the
start — retrofitting means a migration per site.

**`metadata` is a reserved attribute name in SQLAlchemy's declarative API.** The
column is named `metadata` in SQL but mapped as
`transition_metadata: Mapped[...] = mapped_column("metadata", JSON)`.

**`state_transitions` has no FK to `workflow_tasks`,** by design — transitions
outlive task rows and include events (signals, manual overrides) that never had
a task.

---

## 5. The engine — six modules

**Scope, honestly.** What follows is the *load-bearing* code — the parts that are
non-obvious, version-fragile, or were got wrong in production. Where a snippet
is complete it can be lifted near-verbatim. It is **not** a full drop-in
implementation: `WorkflowService`'s method bodies are given as the five-beat
pattern plus the tricky fragments rather than end to end, the ORM mapping over
§4 is left to you, and the HTTP/auth substrate in §7 is described by contract
rather than by code.

An independent reviewer who rebuilt the engine from this document alone reported
that the architecture transfers but the implementation does not: they had to
invent the HTTP layer, the user/role model, and one schema column
(`workflow_tasks` carries no identifier for *which* Spiff task a row projects,
which `task_spec_name` cannot supply once a user task appears inside a loop or a
parallel branch). Budget for that. The value here is the design and §12.

### 5.1 `registry.py` — binding BPMN ids to Python

```python
_REGISTRY: dict[str, Callable] = {}

def service_task(task_id: str) -> Callable[[Callable], Callable]:
    """Decorator: bind a Python callable to a BPMN service-task id."""
    def wrap(fn: Callable) -> Callable:
        if task_id in _REGISTRY:
            raise ValueError(f"service task {task_id!r} already registered")
        _REGISTRY[task_id] = fn
        return fn
    return wrap

def get_handler(task_id: str) -> Callable | None:
    return _REGISTRY.get(task_id)

def registered_tasks() -> list[str]:
    return sorted(_REGISTRY.keys())
```

The duplicate-registration `ValueError` is deliberate: two handlers claiming
`svc_publish` is always a bug, and it should surface at import time, not as a
silent last-writer-wins at 3am.

**Registration is an import side effect** — so the app must import every
handler module at startup, and so must any migration or script that advances a
workflow. (§12, G3)

### 5.2 `engine.py` — Spiff setup and the dispatch trick

The central design decision: **every service task in our BPMN is a
`bpmn:scriptTask` whose entire body is `_dispatch("svc_name")`.**

```xml
<bpmn:scriptTask id="svc_publish_post" name="Publish post">
  <bpmn:script>_dispatch("svc_publish_post")</bpmn:script>
</bpmn:scriptTask>
```

`_dispatch` is injected as a script global bound to the currently-executing
task, so a handler's return value lands in that task's data:

```python
class RegistryScriptEngine(PythonScriptEngine):
    def __init__(self, ctx_factory: CtxFactory) -> None:
        super().__init__()
        self._ctx_factory = ctx_factory

    def execute(self, task, script, external_context=None):
        def _dispatch(task_name: str):
            handler = get_handler(task_name)
            if handler is None:
                raise RuntimeError(
                    f"no Python handler registered for service task {task_name!r}")
            ctx = self._ctx_factory(task, task_name)
            result = handler(ctx)
            if isinstance(result, dict):
                task.data.update(result)      # merged into workflow data
            return result

        ext = dict(external_context or {})
        ext["_dispatch"] = _dispatch
        return super().execute(task, script, ext)
```

Why not `bpmn:serviceTask`? Spiff's service-task/`call_service` surface changed
shape across 1.x → 3.x; `PythonScriptEngine` did not. This costs one XML tag of
fidelity and buys version stability. In the modeler it still *reads* as a
service step because of the `svc_` id convention.

**The handler context.** Every handler receives one object:

```python
@dataclass
class ServiceTaskContext:
    db: Session                  # the caller's session — flush, never commit
    data: dict[str, Any]         # merged workflow variables (see below)
    actor: User | None           # who triggered this step, if anyone
    instance: ProcessInstance | None
    task_name: str
    def get(self, key, default=None): return self.data.get(key, default)
```

`data` is a **three-way merge**, in precedence order: `workflow.data` →
`workflow.data_objects` → `task.data`. This matters because after a
parallel-gateway branch or a loop-back, the executing child task may not have
inherited the start seed, and a handler reading `ctx.get("order_id")` would
otherwise see `None`. (§12, G4)

**Parsing.** Two non-obvious steps:

```python
def parse_spec(bpmn_xml: str, process_key: str):
    parser = BpmnParser()
    # lxml rejects a unicode str carrying an XML encoding declaration.
    payload = bpmn_xml.encode("utf-8") if isinstance(bpmn_xml, str) else bpmn_xml
    parser.add_bpmn_str(payload)
    spec = parser.get_spec(process_key)
    subprocess_specs = parser.find_all_specs()
    _inject_candidate_groups(payload, spec, subprocess_specs)   # see below
    return spec, subprocess_specs
```

**Spiff does not parse the `camunda:` namespace.** `camunda:candidateGroups`
and `camunda:assignee` arrive as an empty `extensions = {}`. We walk the XML
once with lxml and graft the values onto each user-task spec's `extensions`
dict, where the projection layer reads them. (§12, G5)

**Serialization** is `BpmnWorkflowSerializer().serialize_json(workflow)` /
`.deserialize_json(state)`. The serializer is `@lru_cache`d. On deserialize you
must **re-attach a fresh script engine** — it is not part of the serialized
state:

```python
def deserialize_workflow(state_json: str, ctx_factory):
    workflow = _serializer().deserialize_json(state_json)
    workflow.script_engine = RegistryScriptEngine(ctx_factory)
    return workflow
```

### 5.3 `service.py` — `WorkflowService`, the only public API

Domain code never touches SpiffWorkflow. Everything goes through this class.

```python
class WorkflowService:
    db: Session

    # mutating
    def start(self, process_key, *, object_type, object_id, actor=None, data=None) -> ProcessInstance
    def signal(self, business_key, signal_name, payload=None, actor=None) -> ProcessInstance
    def signal_by_correlation(self, object_type, object_id, signal_name,
                              payload=None, actor=None) -> ProcessInstance | None
    def complete_user_task(self, task_id, actor, form_data) -> ProcessInstance
    def cancel(self, business_key, actor, reason) -> ProcessInstance
    def retry_failed_task(self, task_id, actor) -> ProcessInstance

    # read-only
    def get_inbox(self, user) -> list[WorkflowTask]
    def get_history(self, object_type, object_id) -> list[StateTransition]
    def get_instance(self, business_key) -> ProcessInstance | None
    def list_instances(self, *, status=None, process_key=None, limit=100) -> list[ProcessInstance]
    def available_actions(self, business_key, user) -> list[dict]
```

**Every mutating method follows the same five-beat pattern:**

1. Load (or create) the `ProcessInstance` row.
2. Deserialize (or build) the workflow with a ctx factory bound to this
   instance + actor.
3. Apply the input (seed data / payload / form data), then step the engine.
4. Write `state_transitions` audit rows.
5. Persist: serialize, recompute `current_states`, re-project `workflow_tasks`,
   `flush()`.

**`_persist` never commits.** The caller's request handler commits. This is
what makes the workflow step and the domain write **one atomic transaction** —
the single biggest structural advantage of an in-process engine, and the reason
G1 (handlers must not commit) exists.

**Seeding data is a three-way write, not one.** Spiff evaluates gateway
conditions as `evaluate(expr, task.data, external_context=workflow.data_objects)` —
`workflow.data` alone is *not* consulted. So:

```python
def _apply_initial_data(workflow, seed: dict) -> None:
    workflow.data.update(seed)            # Spiff-internal lookups + our ctx merge
    for t in workflow.get_tasks(state=TaskState.READY):
        if t.task_spec.__class__.__name__ == "BpmnStartTask":
            t.data.update(seed)           # <- the write that actually matters
            break
    else:
        for t in workflow.get_tasks(state=TaskState.READY):
            t.data.update(seed)           # fallback: seed all ready tasks
```

> **Do not add `workflow.data_objects.update(seed)`.** It looks like the way to
> populate the expression evaluator's external context. It does nothing:
> `data_objects` is a **read-only property** returning
> `self.data.get('data_objects', {})`, so with no such key it hands back a fresh
> dict on every access and the update is discarded. Real BPMN data objects
> require a `<bpmn:dataObject>` declaration. This line sat in production for
> months with a comment asserting the opposite; the start-task write is what
> makes gateway conditions resolve.

`signal()` does the same three-way write for its payload, and additionally
updates **every waiting/ready task's local data** — a condition expression
evaluates against the task that is about to fire, not the workflow root.
(§12, G4)

**Signals use `BpmnEvent`, not a bare event definition:**

```python
workflow.send_event(BpmnEvent(SignalEventDefinition(signal_name)))
```

The 1.x/2.x `workflow.signal(name)` and `workflow.catch(SignalEventDefinition(...))`
forms do not exist in 3.x. (§12, G6)

**`signal_by_correlation` is the webhook entry point.** Webhooks know a domain
object (`order`, `payment_intent`), not a process key. It looks up the running
instance by `(object_type, object_id)` and returns `None` — not an error — when
nothing is running. A webhook arriving for a workflow that already ended is a
normal, expected no-op, and treating it as an error is how you page yourself at
2am for nothing.

**But `None` has two meanings and you must separate them (G25).** *No instance
at all* is routine. *An instance exists and is not running* means a workflow has
gone permanently deaf — every later signal for that object is discarded, in
silence, forever. Log the first at DEBUG and the second at WARNING with the
statuses you skipped:

```python
if instance is None:
    others = (self.db.query(ProcessInstance.id, ProcessInstance.status)
                .filter(ProcessInstance.object_type == object_type,
                        ProcessInstance.object_id == object_id).limit(5).all())
    if not others:
        log.debug("signal %r dropped: no instance for %s %s", ...)   # routine
    else:
        log.warning("signal %r DROPPED for %s %s — instance(s) exist but none "
                    "are running: %s", ...)                          # diverged
    return None
```

**`available_actions(business_key, user)`** is what makes generic UI possible.
It returns the transitions *this user* can trigger right now: ready user tasks
they may claim/complete (matched on `assignee_user_id` or role), plus the
signal names any currently-waiting catch event would consume. The frontend
renders buttons from this list without knowing anything about the process.

```python
def _catchable_signals(workflow) -> list[str]:
    signals: list[str] = []

    def _collect(ed):
        if ed is None:
            return
        if type(ed).__name__ == "SignalEventDefinition":
            if getattr(ed, "name", None):
                signals.append(ed.name)
            return
        for child in getattr(ed, "event_definitions", None) or []:   # composite
            _collect(child)

    for t in workflow.get_tasks(state=TaskState.WAITING):
        _collect(getattr(t.task_spec, "event_definition", None))     # SINGULAR
        for ed in getattr(t.task_spec, "event_definitions", None) or []:
            _collect(ed)
    return sorted(set(signals))
```

> **`event_definition` is singular.** A catch-event spec in 3.1.2 exposes
> `event_definition`; there is no `event_definitions` attribute on it. Reading
> the plural returns `[]` for *every* workflow, so `available_actions()` never
> offers a signal and the buttons below never render — silently, with no error.
> This shipped and survived undetected precisely because the failure is an empty
> list rather than an exception. The plural is kept only as a fallback for
> composite definitions (`MultipleEventDefinition` exposes children that way).

**Task projection is a reconciliation, not an append.** After every step,
`_reproject_tasks` diffs Spiff's ready user tasks against the `ready` rows in
`workflow_tasks`: rows whose task is no longer ready are closed, genuinely-new
ready tasks get rows plus a `task_started` audit entry, and rows for tasks still
ready are left alone (preserving a claim). Assignment is resolved at projection
time: `camunda:candidateGroups` → `assignee_role` as a **comma-separated list,
preserving every group** — take only the first and every other candidate role
silently loses the task (G24); match with a split-and-any helper, never `==` or
a SQL `IN`. `camunda:assignee` holds the **name of a workflow variable**
(e.g. `owner_user_id`) which is resolved against task/workflow data → 
`assignee_user_id`. Keep it a bare variable name — no Jinja/`${}` interpolation.

**Error handling.** If `do_engine_steps()` raises, the instance is flipped to
`status='error'`, an `error` audit row records the exception text, and the
exception re-raises so the request fails loudly. The instance is left where it
died for inspection.

### 5.4 `loader.py` — deploying `.bpmn` files

On startup, scan the BPMN directories, hash each file, and append a new
`process_definitions` version when the hash differs from the latest.

```python
def sync_definitions(db, bpmn_dir=None) -> list[ProcessDefinition]:
    dirs = [bpmn_dir] if bpmn_dir else [d for d in (_core_bpmn_dir(), _site_bpmn_dir()) if d.exists()]

    keyed: dict[str, Path] = {}
    for d in dirs:                                # [core, site] order matters:
        for path in sorted(d.glob("*.bpmn")):     # a site file shadows a core
            keyed[_extract_process_key(path.read_text(), path.name)] = path

    rows = []
    for key, path in keyed.items():
        xml = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(xml.encode()).hexdigest()
        latest = (db.query(ProcessDefinition)
                    .filter(ProcessDefinition.process_key == key)
                    .order_by(ProcessDefinition.version.desc()).first())
        if latest and latest.bpmn_hash == digest:
            rows.append(latest); continue
        new = ProcessDefinition(process_key=key, version=(latest.version + 1) if latest else 1,
                                bpmn_xml=xml, bpmn_hash=digest, deployed_at=_now())
        db.add(new); db.flush(); rows.append(new)
    return rows
```

`process_key` comes from `bpmn:process/@id`, falling back to the filename stem.
**Keep them identical** — the file name is what a human greps for; the id is
what the engine uses.

`missing_handlers(db)` cross-references every `serviceTask`/`scriptTask` id in
the deployed XML against the registry and returns the unregistered ones. **In
the source system it is written but never called at startup** (and it only scans the
site dir, not core) — wiring it into startup as a hard failure is a
recommended improvement for a fresh implementation. A missing handler is
otherwise discovered at runtime, mid-transition, in production.

### 5.5 `audit.py` — one writer, one place

```python
def record(db, *, process_instance_id, object_type, object_id, event,
           task_spec_name=None, from_state=None, to_state=None,
           actor_user_id=None, reason=None, metadata=None) -> StateTransition:
    row = StateTransition(..., transition_metadata=metadata, created_at=_now())
    db.add(row); db.flush()
    return row
```

Trivial on purpose. It is a separate module so that later needs (analytics
fan-out, Sentry breadcrumbs, an outbox) get one edit site rather than a
grep-and-hope across `service.py`.

Canonical `event` values: `started`, `task_started`, `task_completed`,
`signal`, `error`, `canceled`, `ended`. Ledger writes (§6.4.2) use
domain-specific verbs (`archive`, `unarchive`, `status_override`).

### 5.6 `timer.py` — the 60-second tick

BPMN timer events need something to wake them. An APScheduler `BackgroundScheduler`
job runs every 60s, and for each **running** instance: deserialize →
`refresh_waiting_tasks()` (fires due timers) → `do_engine_steps()` → persist
**only if something actually moved**.

```python
def _completed_count(workflow) -> int:
    # NOT get_tasks(state=COMPLETED) — see G8. TaskIterator stops descending at
    # the first ancestor below min_state, so a WAITING gateway hides everything
    # completed beneath it and the sweep discards real progress.
    return sum(1 for t in workflow.get_tasks() if t.state == TaskState.COMPLETED)

before = _completed_count(workflow)
workflow.refresh_waiting_tasks()
workflow.do_engine_steps()
after = _completed_count(workflow)
if after > before or _workflow_is_complete(workflow):
    service._persist(workflow, instance)
```

**Progress is measured by counting COMPLETED tasks, not by comparing task-spec
names.** A polling loop ends each iteration back at the same
`timer_wait_30s` spec name — a name-keyed before/after snapshot sees no change
and never persists, so the loop silently never advances. Completed-count is
monotonic. (§12, G8)

**And the count must scan, not filter.** Getting this wrong is not a missed
optimisation: on an event-based gateway the persist gate evaluates `False` while
the handler's domain write has already committed, so the sweep re-runs the
handler every 60 seconds forever (§12, G8 and G23).

**Every instance is processed in its own try/except with a `db.rollback()` on
failure.** One poisoned workflow must not stop the sweep for everything else —
and without the rollback, a `DataError` poisons the session and every
subsequent instance in the same tick fails too.

Three operational requirements this module taught us:

- **Route `bpm.*` loggers to uvicorn's handlers at startup.** uvicorn configures
  its own logger and leaves the root alone, so `logging.getLogger("bpm.timer").info(...)`
  vanishes silently. (§12, G12)
- **A permanently-failing instance retries forever.** Two shipped bugs produced
  ~11.5k and ~17.3k tracebacks/day for weeks, with no crash and all health
  checks green (§12, G14). Alert on tick error *rate*, and give the sweep a
  give-up path.
- **Multi-worker uvicorn starts the scheduler in every worker.** Each tick runs
  N times. Currently idempotent-but-wasteful; the fix when it matters is a
  `SELECT ... FOR UPDATE SKIP LOCKED` claim per instance. (§12, G13)

### 5.7 `retry.py` — the transient-failure protocol

A service task calling Stripe/Cloudflare/EasyPost **must not raise** on a 5xx.
Raising flips the instance to `error` and demands a human. Instead, handlers
return flags that a gateway reads to loop back through a timer.

```python
retry_pending(op, attempt, max_attempts=10)
# → {f"{op}_attempt": N, f"{op}_retry_pending": True, f"{op}_max_attempts": M,
#    f"{op}_ok": False, f"{op}_failed": False}

terminal_failure(op, reason, code=None)
# → {f"{op}_failed": True, f"{op}_ok": False, f"{op}_retry_pending": False,
#    f"{op}_failure_reason": ..., f"{op}_failure_code": ...}

clear_retry(op)
# → {f"{op}_attempt": 0, f"{op}_retry_pending": False,
#    f"{op}_ok": True, f"{op}_failed": False}
```

The corresponding BPMN:

```
svc_do_thing → gw_outcome ├── thing_retry_pending and thing_attempt < thing_max_attempts
                          │      → timer(PT30S) → back to svc_do_thing
                          ├── thing_retry_pending  → svc_flag_needs_intervention
                          ├── thing_failed         → failure branch
                          └── default              → next step
```

**Every helper sets *all* of `_ok` / `_failed` / `_retry_pending` explicitly,
even the irrelevant ones.** A gateway condition referencing a flag that the
taken branch never set raises `NameError` inside Spiff's evaluator and takes
down the transition. Exhaustive flags are the cheap fix. (§12, G9)

**They are not quite exhaustive enough, and the gateway above hides it.**
`terminal_failure` sets neither `<op>_attempt` nor `<op>_max_attempts`, which
the retry condition references. It survives only because Python's `and`
short-circuits on `<op>_retry_pending == False` before reaching them — so
reordering that condition, or adding a branch that reads `_attempt` first,
turns a permanent failure into a `NameError`. Either have `terminal_failure`
set the counters too, or treat the short-circuit as load-bearing and comment it
as such. Do not leave it accidental.

Distinguish transient (retry) from permanent (route to failure) at the
**handler**, not the gateway. A declined card is not a network blip.

---

## 6. Authoring BPMN — conventions and canonical shapes

### 6.1 Toolchain

Author in **Camunda Modeler** (free desktop, emits standard BPMN 2.0). Save to
`bpmn/<process_key>.bpmn`, commit, deploy — the loader picks it up on the next
startup. Hand-editing the XML is fine and common here; these files are small and
`git diff`-able, and hand-editing is how you get useful `<!-- comments -->`
explaining *why* a branch exists. Round-tripping through the modeler preserves
them.

### 6.2 Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Process key + file name | snake_case object name, identical | `blog_post` / `blog_post.bpmn` |
| Business key | `<process_key>:<object_id>` | `order:9f3c…` |
| Service task id | `svc_<verb>_<noun>` | `svc_publish_post` |
| User task id | `user_<actor>_<verb>_<noun>` | `user_admin_review_post` |
| Signal name | past-tense domain event | `payment_captured`, `all_tasks_done` |
| Catch state | `state_<name>` | `state_wrapping` |
| Gateway | `gw_<question>` | `gw_verdict` |
| Timer | `timer_<duration or purpose>` | `timer_24h`, `timer_wait_30s` |
| End event | `end_<terminal state>` | `end_published`, `end_canceled` |
| Sequence flow | `sf_<meaning>` | `sf_approve` |

These prefixes are not decoration: the frontend's `labelFor()` strips
`svc_|user_|state_|end_|timer_|gw_|sf_|sig_` to derive a human label for any
unmapped name (§7.4), and the loader's handler check keys off `svc_`.

**Every element gets an explicit, meaningful `id`.** Modeler-generated
`Activity_0x7f2k` ids end up in your audit table, your admin UI, and your
`workflow_tasks` rows forever.

### 6.3 The XML boilerplate

```xml
<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  id="Definitions_<key>"
                  targetNamespace="https://<product>/bpmn">
  <!-- Signals are declared ONCE at definitions level, referenced by signalRef. -->
  <bpmn:signal id="sig_<name>" name="<name>" />

  <bpmn:process id="<process_key>" name="<Human Name>" isExecutable="true">
    ...
  </bpmn:process>
</bpmn:definitions>
```

Note: **no `bpmndi` diagram section is required.** The engine ignores it. Files
authored by hand here omit it; files round-tripped through the modeler carry it.

### 6.4 The five canonical process shapes

Nearly every lifecycle in this codebase is one of these five, or a composition.
Start by picking the shape.

#### 6.4.1 Shape A — review-and-approve (the simplest useful one)

Start → user task → gateway → service task → end. `blog_post`, `expense_approval`,
`testimonial_review`, `instructor_onboarding`.

```xml
<bpmn:startEvent id="start"><bpmn:outgoing>sf_start</bpmn:outgoing></bpmn:startEvent>
<bpmn:sequenceFlow id="sf_start" sourceRef="start" targetRef="user_admin_review_post" />

<bpmn:userTask id="user_admin_review_post" name="Review blog post"
               camunda:candidateGroups="instructor,admin">
  <bpmn:incoming>sf_start</bpmn:incoming><bpmn:outgoing>sf_decided</bpmn:outgoing>
</bpmn:userTask>
<bpmn:sequenceFlow id="sf_decided" sourceRef="user_admin_review_post" targetRef="gw_decision" />

<!-- ALWAYS set default= on an exclusive gateway. -->
<bpmn:exclusiveGateway id="gw_decision" name="Approve?" default="sf_reject">
  <bpmn:incoming>sf_decided</bpmn:incoming>
  <bpmn:outgoing>sf_approve</bpmn:outgoing><bpmn:outgoing>sf_reject</bpmn:outgoing>
</bpmn:exclusiveGateway>

<bpmn:sequenceFlow id="sf_approve" sourceRef="gw_decision" targetRef="svc_publish_post">
  <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">decision == 'approve'</bpmn:conditionExpression>
</bpmn:sequenceFlow>
<bpmn:sequenceFlow id="sf_reject" sourceRef="gw_decision" targetRef="end_rejected" />

<bpmn:scriptTask id="svc_publish_post" name="Publish post">
  <bpmn:incoming>sf_approve</bpmn:incoming><bpmn:outgoing>sf_published</bpmn:outgoing>
  <bpmn:script>_dispatch("svc_publish_post")</bpmn:script>
</bpmn:scriptTask>
```

`decision` arrives as user-task form data from `POST /api/tasks/{id}/complete`.
The `default=` attribute is not optional in practice — an exclusive gateway
where no condition matches and no default exists is a runtime exception.

#### 6.4.2 Shape B — durable anchor + ledger (for unbounded/repeatable transitions)

The workflow parks at a single catch state and only advances on a **forward or
terminal** signal. Every arbitrary, repeatable, per-actor transition is written
as a `state_transitions` row attached to the instance instead of a token move.

This is the answer to G7 (§12) and it is used more than any other shape.

```xml
<!-- message_thread.bpmn — the whole process -->
<bpmn:signal id="sig_delete" name="delete_thread" />
<bpmn:process id="message_thread" isExecutable="true">
  <bpmn:startEvent id="start"><bpmn:outgoing>sf_start</bpmn:outgoing></bpmn:startEvent>
  <bpmn:sequenceFlow id="sf_start" sourceRef="start" targetRef="state_alive" />

  <bpmn:intermediateCatchEvent id="state_alive" name="Alive — awaiting delete">
    <bpmn:incoming>sf_start</bpmn:incoming><bpmn:outgoing>sf_delete</bpmn:outgoing>
    <bpmn:signalEventDefinition signalRef="sig_delete" />
  </bpmn:intermediateCatchEvent>

  <bpmn:sequenceFlow id="sf_delete" sourceRef="state_alive" targetRef="svc_delete_thread" />
  <bpmn:scriptTask id="svc_delete_thread"><bpmn:script>_dispatch("svc_delete_thread")</bpmn:script></bpmn:scriptTask>
  <bpmn:endEvent id="end_deleted" />
</bpmn:process>
```

The per-member archive/unarchive actions — unbounded, repeatable, per-actor —
are `state_transitions` rows with `event in ('archive','unarchive')` and
`actor_user_id` identifying the member. Fully audited, no graph complexity.

The helper that writes them:

```python
def ledger_transition(db, *, object_type, object_id, event,
                      from_state=None, to_state=None, actor_user_id=None, metadata=None) -> bool:
    """Record an arbitrary/manual transition against the entity's running
    lifecycle instance. Best-effort, no commit; the caller owns the txn and
    the denormalized status column."""
    inst = (db.query(ProcessInstance)
              .filter(ProcessInstance.object_type == object_type,
                      ProcessInstance.object_id == object_id,
                      ProcessInstance.status == "running")
              .order_by(ProcessInstance.started_at.desc()).first())
    if inst is None:
        logger.info("ledger_transition: no running lifecycle for %s %s", object_type, object_id)
        return False
    try:
        audit.record(db, process_instance_id=inst.id, object_type=object_type,
                     object_id=object_id, event=event, from_state=from_state,
                     to_state=to_state, actor_user_id=actor_user_id, metadata=metadata)
        return True
    except Exception:
        logger.exception("ledger_transition failed")   # audit must never break the write
        return False
```

**This shape is what "BPM everywhere" actually means in practice.** It is not
"draw every possible transition." It is "every entity has exactly one durable,
auditable lifecycle anchor, and every transition is attributed — whether it
moved a token or not."

#### 6.4.3 Shape C — event-based race (the one that prevents zombies)

An `eventBasedGateway` waits on several catch events at once; the first to fire
wins and Spiff eliminates the rest. Use it wherever an object waits on an
external system that might never respond.

```xml
<!-- order.bpmn: payment captures, fails, admin cancels, or 30 min elapse -->
<bpmn:eventBasedGateway id="gw_await_payment" name="Await payment">
  <bpmn:incoming>sf_placed</bpmn:incoming>
  <bpmn:outgoing>sf_to_captured</bpmn:outgoing>
  <bpmn:outgoing>sf_to_failed</bpmn:outgoing>
  <bpmn:outgoing>sf_to_canceled</bpmn:outgoing>
  <bpmn:outgoing>sf_to_timeout</bpmn:outgoing>
</bpmn:eventBasedGateway>

<bpmn:intermediateCatchEvent id="evt_payment_captured">
  <bpmn:signalEventDefinition signalRef="sig_payment_captured" />
</bpmn:intermediateCatchEvent>
<bpmn:intermediateCatchEvent id="evt_payment_failed">
  <bpmn:signalEventDefinition signalRef="sig_payment_failed" />
</bpmn:intermediateCatchEvent>
<bpmn:intermediateCatchEvent id="evt_canceled">
  <bpmn:signalEventDefinition signalRef="sig_canceled" />
</bpmn:intermediateCatchEvent>
<bpmn:intermediateCatchEvent id="timer_payment_timeout" name="Wait 30 min">
  <bpmn:timerEventDefinition>
    <!-- Spiff EVALUATES this body as Python. The quotes are mandatory. -->
    <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">"PT30M"</bpmn:timeDuration>
  </bpmn:timerEventDefinition>
</bpmn:intermediateCatchEvent>
```

**Every end event in this process must terminate.** This is not optional
decoration — omit it and the shape produces the exact zombie it exists to
prevent:

```xml
<bpmn:endEvent id="end_canceled" name="Order canceled">
  <bpmn:incoming>sf_released</bpmn:incoming>
  <bpmn:terminateEventDefinition/>      <!-- REQUIRED. See §12 G23. -->
</bpmn:endEvent>
```

Without it the losing arms stay `MAYBE`, the gateway stays `WAITING`, `EndJoin`
never resolves, and the instance never leaves `running` — while the timeout
handler re-runs every 60 seconds forever. Full mechanism and measurements in
G23; it is the most expensive gotcha in this document.

The real-world stake, from the comment in the file: without the timeout branch,
an abandoned Stripe Checkout leaves the workflow zombied at `await_payment`
**and inventory committed indefinitely** — phantom oversells. A single-catch
"wait for payment" is almost always a bug. (And a timeout branch *without*
terminate strands it just as thoroughly, one layer deeper.)

#### 6.4.4 Shape D — parent/child orchestration with a join

Two **independent top-level instances** that signal each other. The parent parks
at catch states; children report up when they reach terminal states; a plain
Python query computes the join condition.

```
project_lifecycle (parent)
  start → state_active   ──catch: all_tasks_done──▶ svc_enter_wrapping
        → state_wrapping ──catch: final_payment_received──▶ svc_complete_project → end

task_lifecycle (child, one per task)      ─┐
contract_signing (child, one per contract) ├─▶ orchestration.signal_parent_project(...)
expense_approval (child, one per expense)  │
invoice_payment  (child, one per invoice) ─┘
```

The child→parent signal is one call, made from a service-task handler:

```python
def signal_parent_project(db, project_id: str, signal_name: str, payload=None) -> None:
    """Best-effort: a parent that isn't running, or isn't listening for this
    signal, is a no-op — the child's own row stays authoritative."""
    try:
        WorkflowService(db).signal(f"project_lifecycle:{project_id}", signal_name, payload=payload or {})
    except LookupError:
        logger.info("no running project_lifecycle:%s", project_id)
    except Exception:      # Spiff raises WorkflowException when nothing catches the signal
        logger.exception("signal_parent_project failed")
```

**The join is computed in Python, not in the graph.** When a task is marked
done, the router signals the task's own child workflow; that handler calls
`check_all_tasks_done(db, project_id)` — an ordinary SQL count — and only if it
returns true does it signal the parent `all_tasks_done`. Trying to express
"wait for N children where N is unknown at design time" as a BPMN parallel join
is the wrong tool; a `COUNT(*)` is the right one.

Two properties worth copying:

- **Children are independent instances, not call activities.** A child can be
  created before the parent, outlive it, or be versioned separately. The
  coupling is one signal name.
- **Signalling up is always best-effort.** A parent that already advanced past
  its catch event is a normal no-op. Both `LookupError` (no parent) and Spiff's
  `WorkflowException` (parent running but not listening) are swallowed and
  logged.

#### 6.4.5 Shape E — timer poll loop (for async external work)

For polling an external job (video transcoding, a batch API):

```
svc_poll_stream_status → gw_poll_outcome ├── ready  → user_publish_decision
                                         ├── error  → end_error
                                         └── default → timer_wait_30s → back to svc_poll
```

```xml
<bpmn:intermediateCatchEvent id="timer_wait_30s" name="Wait 30s">
  <bpmn:incoming>sf_still_waiting</bpmn:incoming><bpmn:outgoing>sf_poll_again</bpmn:outgoing>
  <bpmn:timerEventDefinition><bpmn:timeDuration xsi:type="bpmn:tFormalExpression">"PT30S"</bpmn:timeDuration></bpmn:timerEventDefinition>
</bpmn:intermediateCatchEvent>
<bpmn:sequenceFlow id="sf_poll_again" sourceRef="timer_wait_30s" targetRef="svc_poll_stream_status" />
```

**A timer loop-back is fine** (this one runs in production); it is *signal*
catch-event loop-backs that terminate the workflow (§12, G7).

Two requirements for the loop body handler:

- **Short-circuit on every terminal and post-loop state**, not just the one you
  expect. `svc_poll_stream_status` originally short-circuited only on
  `state == 'ready'`; a backfill ran it against already-`published` rows, so it
  fell through, called Cloudflare, and **overwrote `published` back to `ready`**.
  (§12, G10)
- **Bound the loop.** An attempt counter plus a gateway to
  `svc_flag_needs_intervention` (the §5.7 protocol) — otherwise a permanently
  stuck external job polls every 30s forever.

#### 6.4.6 Composition: AI triage (Shape A + a machine first-pass)

`stripe_review.bpmn` — the shipped AI-in-BPM process (§9):

```
start → svc_review_content (Claude or the deterministic fake; writes a row,
                            sets workflow data `verdict` + `review_id`)
      → gw_verdict (default = sf_needs_human)
          ├── verdict == 'pass' → svc_notify_parent_pass → end_pass
          └── default           → user_admin_stripe_review (candidateGroups="admin")
                                    → gw_decision (default = sf_reject)
                                        ├── decision == 'approve' → svc_record_override → svc_notify_parent_pass
                                        └── default               → svc_notify_parent_block → end_blocked
```

Note both gateways **default to the safe branch**: unknown verdict → human;
unknown decision → reject. And both ends carry `<bpmn:terminateEventDefinition/>`
so no stray token survives.

### 6.5 Gateway condition rules

- Conditions are **plain Python expressions** evaluated against task data:
  `verdict == 'pass'`, `attempt < max_attempts`. Not `${...}`, not JUEL.
- **Always set `default=`** on an exclusive gateway, pointing at the *safe*
  branch.
- **Only reference variables that are guaranteed set on every path into the
  gateway.** An unset name is a `NameError` at transition time, not a falsy
  value. This is why §5.7's helpers set every flag explicitly.
- Keep conditions to simple comparisons on flat scalars. Anything requiring
  `.get()` chains or DB access belongs in the preceding service task, which
  returns a scalar flag.

---

## 7. Integrating BPM into a web application

The engine is half the system. This section is the other half — and it is what
most BPM adoptions get wrong, by building a beautiful engine that the
application then routes around.

### 7.1 Routers: emit, don't mutate

#### 7.1.1 Starting a lifecycle

At entity creation:

```python
@router.post("/blog/{post_id}/submit")
def submit_for_review(post_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    post = _load_or_404(db, post_id)
    _authorize(user, post)
    WorkflowService(db).start("blog_post", object_type="blog_post", object_id=post.id,
                              actor=user, data={"post_id": post.id, "author_id": post.author_id})
    db.commit()
    return {"status": "submitted"}
```

The router does auth + validation + `start`. It does **not** set
`post.state = "pending_review"` — `svc_*` handlers own that.

#### 7.1.2 Advancing a lifecycle

```python
wf.signal(f"booking:{booking.id}", "cancel", actor=current_user)
wf.signal(f"gear_share:{share.id}", "accept", actor=current_user)
wf.signal(f"course_enrollment:{e.id}", "video_completed")
```

That is the entire transition surface for those objects. Grep the codebase for
`wf.signal(` and you have the complete list of things that can happen.

#### 7.1.3 The best-effort start wrapper

Starting a lifecycle must not be able to break entity creation:

```python
def start_lifecycle(db, *, process_key, object_type, object_id, actor=None, data=None) -> None:
    try:
        WorkflowService(db).start(process_key, object_type=object_type,
                                  object_id=object_id, actor=actor, data=data or {})
    except ValueError:
        pass        # already running for this object — idempotent, fine
    except Exception:
        # LookupError (no process definition) lands here too. That one does NOT
        # self-heal: it silently disables the lifecycle for EVERY entity of this
        # type until the .bpmn is loaded. If lifecycles never start, grep for this.
        logger.exception("start_lifecycle failed: %s for %s %s", process_key, object_type, object_id)
```

The comment is the important part. Swallowing `LookupError` trades a loud
failure for a silent feature-off. Worth it (entity creation keeps working), but
only if the log line is greppable and someone knows to grep it. A fresh
implementation should additionally fail startup when a referenced
`process_key` has no definition.

### 7.2 Webhooks: correlate, signal, tolerate

External systems know their own ids, and they retry. The pattern:

```python
_STRIPE_TO_ORDER_SIGNAL = {
    "payment_intent.succeeded":      "payment_captured",
    "payment_intent.payment_failed": "order_payment_failed",
    "charge.dispute.created":        "dispute_opened",
}

def _route_signals_to_workflows(db, event_type, order, payload) -> list[str]:
    fired, wf = [], WorkflowService(db)
    if (name := _STRIPE_TO_ORDER_SIGNAL.get(event_type)):
        try:
            inst = wf.signal_by_correlation(
                object_type="order", object_id=order.id, signal_name=name,
                payload={"stripe_event_type": event_type, "stripe_payload": payload})
            if inst is not None:
                fired.append(f"order:{name}")
        except Exception as exc:      # a webhook must NEVER fail on a workflow error
            log.warning("order signal routing failed event=%s err=%s", event_type, exc)
    return fired
```

Four rules, each learned the expensive way:

1. **A static dict maps provider event → domain signal.** One table to read
   when asking "what does Stripe tell us and what do we do about it."
2. **Correlate by `(object_type, object_id)`, not by process key.** The webhook
   doesn't know or care which process is running.
3. **No running instance is a no-op, not an error — but say which kind.** Late
   webhooks for finished workflows are normal. An instance that exists in
   `error` is not: it will swallow every future signal silently (G25). Log the
   two cases differently or you will not find out for months.
4. **Never fail the webhook on a workflow error.** Return 200 and log. A
   provider that gets a 500 retries with backoff and eventually disables your
   endpoint. The domain row update and the signal are separate concerns; do the
   row update inline and treat the signal as best-effort.

The response body reports `{"signaled": [...]}` — invaluable when debugging
"did that event actually do anything?"

### 7.3 Sweeps: clock-driven cadence that emits BPM actions

Recurring business rhythm (bi-weekly digests; "if wrapping and past deadline,
issue the final invoice") is an **operational sweep**, not a BPMN timer:

- Hourly APScheduler job, started from a site-agnostic `on_startup()` hook.
- The sweep **emits BPM actions**: issues an invoice (which starts a child
  lifecycle), signals a parent, sends an email.
- **The sweep never mutates `status`.** Every state change still goes through
  the workflow.
- Sweep functions take an **injectable `now`** so they are unit-testable
  without freezing the clock globally.

Why not a BPMN timer loop for the cadence? Because a perpetual bi-weekly timer
lives on a parallel branch that must be cancelled on every terminal transition —
the exact shape the engine handles worst (§12, G7). A cadence is machinery, not
a lifecycle. The distinction to hold onto: **if the clock changes the object's
state, it's a BPMN timer; if the clock just makes something happen, it's a
sweep.**

### 7.4 Frontend: one generic helper, no per-page workflow logic

A single JS module (`js/workflow.js`) talks to the workflow API, and every page
showing a stateful object uses it:

```js
Workflow.fetchInstance(businessKey)   // → {instance, history} | null
Workflow.fetchActions(businessKey)    // → [{kind:'user_task', task_id, task_spec_name} |
                                      //    {kind:'signal', signal_name}]
Workflow.completeTask(taskId, formData)
Workflow.labelFor(name)               // task_spec_name → human label
Workflow.stateBadgeHtml(state)        // → <span class="wf-state wf-state-published">
```

The pattern that makes this work: **the page renders buttons from
`fetchActions()`** rather than hardcoding which transitions exist. Add a signal
to the BPMN, and the button appears — no frontend change.

`labelFor()` holds an explicit map for known names and falls back to stripping
the `svc_|user_|state_|end_|timer_|gw_|sf_|sig_` prefix and un-underscoring. So
a brand-new task spec renders as "publish post" rather than
`svc_publish_post`, and improving it later is a one-line map entry.

State badges get a CSS class derived from the state name
(`wf-state-<state>`), so styling is per-site CSS with no JS involvement.

### 7.5 The user-task inbox

Four endpoints, all authenticated, all scoped to the caller:

| Endpoint | Purpose |
|---|---|
| `GET /api/tasks/inbox` | Tasks assigned to me **or** to a role I hold |
| `GET /api/tasks/{id}` | Detail (403 unless visible to me) |
| `POST /api/tasks/{id}/claim` | Lock a role-assigned task to me |
| `POST /api/tasks/{id}/complete` | Submit form data; advances the workflow |

Visibility is a single predicate used by every endpoint:

```python
def _visible_to(task, user) -> bool:
    return (task.assignee_user_id == user.id
            or (task.assignee_role and user.has_role(task.assignee_role)))
```

Claim rules: must be `ready` (else 409), role must match (403), and a task
already assigned to a *different* user is refused (403).

The inbox query is why `workflow_tasks` is a denormalized projection — it is
two indexed predicates (`idx_user_inbox`, `idx_role_inbox`), not N workflow
deserializations.

### 7.6 Admin operations

| Endpoint | Purpose |
|---|---|
| `GET /api/workflows?status=&process_key=&limit=` | Instance list (admin) |
| `GET /api/workflows/{id}` | Instance + tasks + full history (admin) |
| `POST /api/workflows/{id}/cancel?reason=` | Cancel; closes ready tasks, audits the reason |
| `POST /api/workflows/{id}/retry/{task_id}` | Retry a failed service task |
| `GET /api/workflows/by-key/{business_key}` | Instance + history — **any authenticated user** |
| `GET /api/workflows/by-key/{business_key}/actions` | What I can trigger right now |

The `by-key` pair is the important design choice: **object pages need workflow
state, and their viewers are not admins.** Splitting the read surface by
business key (rather than instance id) keeps the ops UI admin-only while
letting any product page render a state badge, a history timeline, and
transition buttons.

> **⚠ Authorize `by-key` against the underlying object.** As shipped in the
> reference implementation it requires only *authentication*: any logged-in user
> who can guess a business key — `blog_post:<id>`, `order:<id>` — reads that
> object's full transition history, including `actor_user_id`, `reason` and
> `metadata`. Business keys are enumerable by construction, so this is a
> horizontal-access hole, not a theoretical one. The fix is a per-object-type
> ownership check before returning history; `available_actions` already scopes
> by role, but the read path does not.

`cancel` sets `status='canceled'`, marks every `ready` task `canceled`, and
writes a `canceled` audit row **with a mandatory reason**. It does not step the
workflow — a canceled instance is frozen for inspection.

**`retry_failed_task` is currently unimplemented** (`NotImplementedError`; the
endpoint returns 501). Honest gap, called out here so nobody assumes coverage.
Today recovery is: fix the handler, deploy, and let the 60s tick re-drive the
instance — which works because handlers are written idempotent, and which is
*why* they must be.

### 7.7 Agent / MCP surface

The workflow API is exposed as MCP tools, which turns the engine into something
an AI agent can operate:

`start_workflow`, `signal_workflow`, `signal_workflow_by_correlation`,
`cancel_workflow`, `get_workflow`, `get_workflow_by_business_key`,
`list_workflows`, `list_workflow_events`, `describe_workflow`,
`list_pending_tasks`, `claim_task`, `complete_task`, `retry_task`,
`list_signals`, `wait_for_event`.

This is nearly free once §7.5–§7.6 exist — the MCP layer is a thin typed
wrapper over `WorkflowService`, sharing the same permission checks. It is worth
noting as a design *benefit* of BPM-everywhere: because every transition is a
named signal against a business key, an agent can drive the business without
any bespoke agent API. The alternative (scattered `PATCH /orders/{id}` with a
`status` field) gives an agent no vocabulary and no guardrails.

### 7.8 Where a transition can come from

The payoff of putting side effects in handlers (Law 2). Every one of these
paths produces identical, audited behavior:

```
router (user action) ─┐
webhook (external)  ──┤
timer tick (BPMN)   ──┼──▶ WorkflowService ──▶ handler ──▶ side effects + audit
sweep (cadence)     ──┤
MCP tool (agent)    ──┤
admin override      ──┘
```

---

## 8. Multi-site / multi-tenant layout

Only relevant if one codebase serves several products. Here, four sites share
`app/core/` and each owns `app/sites/<site>/`.

- **`app/core/bpm/`** — the engine. Never imports a site package.
- **`app/core/bpmn/` + `app/core/bpm_tasks/`** — processes genuinely shared
  across products (order, payment_intent, refund, shipment, dispute,
  cart_recovery, connected_account, entity_transfer, refund_reversal,
  performer_payout_reconciliation).
- **`app/sites/<site>/bpmn/` + `bpm_tasks/`** — everything with product flavor.
- **The loader scans core first, then the active site; a site file with the
  same `process_key` shadows the core one.** That is the override mechanism —
  Sites A and B each ship their own `stripe_review.bpmn` this way.
- The active site comes from an env var (`APP_SITE`) read at startup; each site
  has its own database, so `process_instances` never mixes tenants.

For a **single-product** implementation, drop the split. Adding it later is
mechanical; carrying it prematurely is pure overhead.

---

## 9. AI service tasks

AI calls are service tasks. They have inputs, outputs, latency, and failure
modes — exactly what the pattern models. What they add is: structured output,
confidence, human review, cost, and reproducibility.

### 9.1 The canonical shape: AI proposes, human disposes

```
svc_ai_<task> → gw_confidence ├── confidence >= threshold → svc_apply_result → end
                              └── default (INCLUDING null) → user_review_ai_output
                                        → svc_apply_result → end
```

The gateway defaults to human review, so an unparseable response, a null
confidence, or an unexpected verdict all route to a person rather than
auto-applying. Human overrides are recorded and become evaluation data.

### 9.2 The fake-first fast path (the pattern that actually shipped)

The shipped implementation (`stripe_review`) does **not** call the model first.
It runs a **deterministic reviewer first** as a cheap pre-filter, and only
escalates genuinely ambiguous cases to Claude:

- Obvious cases are decided by rules — free, instant, reproducible, testable.
- The paid API adjudicates only ambiguity.
- Mode is resolved in layers, highest wins: **DB override**
  (`platform_settings['stripe_review.mode']`, flippable from an admin page
  without redeploy) → **env var** (`STRIPE_REVIEW_MODE`) → **default `auto`**
  (real if `ANTHROPIC_API_KEY` is set, else fake).
- `fake | real | auto` means the whole local dev stack and the entire test
  suite run with zero credentials and zero cost, deterministically.

This is the single most valuable AI-integration lesson here: **the fake is not
a test double, it is the first stage of the production pipeline.**

### 9.3 Dedup by content hash

`content_hash(entity_type, fields)` is a stable hash of the reviewable text.
Before starting a review workflow, look for an existing row with the same
`(entity_type, entity_id, content_hash)` and return it. Re-saving a product
without touching its description costs nothing. The hash is also packed into
the workflow's `object_id` so an entity's review history is one row per
*content version*, not one per save.

### 9.4 The `ai_invocations` table

Schema: `process_instance_id`, `state_transition_id`, `task_name`, `model`,
`(object_type, object_id)`, `prompt_hash`, token counts (`input`, `output`,
`cache_read`, `cache_creation`), `cost_usd`, `latency_ms`, `status`,
`confidence`, `output_json`, `raw_response`, plus the human-review columns
(`review_status`, `reviewed_by_user_id`, `reviewed_at`, `human_override_json`).

Kept separate from `state_transitions` because the questions are different:
cost per task, model comparison, cache hit rate, review queue depth,
disagreement rate.

> **Honest status:** the model exists in `app/core/models/workflow.py` but
> **nothing writes to it yet** — the shipped AI path writes domain-specific
> `stripe_reviews` rows instead. A fresh implementation should wire it from the
> start; retrofitting cost telemetry after the fact is how you end up unable to
> answer "what did that feature cost us."

### 9.5 Guardrails for consequential decisions

1. **Some categories are always human.** Set the threshold to unreachable
   (1.0) rather than adding a special case, so the routing stays in the graph.
2. **Version-pin the prompt.** `prompt_hash` lets you trace any decision back
   to the exact policy text the model saw.
3. **Shadow-eval before promoting a model.** Replay recent production items on
   the new model via the Batch API, diff the outputs, review disagreements. No
   auto-promote. Model choice lives in config, **not in the BPMN** — the
   diagram stays model-agnostic and rollback is a one-line change.
4. **Cost circuit breaker.** Above an hourly ceiling, `svc_ai_*` short-circuits
   straight to `user_review` without calling the API. Graceful degradation:
   humans pick up the slack.
5. **Structured output via tool use**, with a schema per task, so the response
   is validated rather than regex-scraped.

---

## 10. Testing

Three layers, each catching what the others cannot.

### 10.1 BPMN contract tests — no database

Drive SpiffWorkflow directly against the `.bpmn` file with a recording engine
that captures `_dispatch` calls. Fast, hermetic, and they test the thing most
likely to be wrong: the graph.

```python
class _RecordingEngine(PythonScriptEngine):
    def __init__(self, results=None):
        super().__init__(); self.calls = []; self._results = results or {}
    def execute(self, task, script, external_context=None):
        calls, results = self.calls, self._results
        def _dispatch(name):
            calls.append(name)
            out = results.get(name, {"dispatched": name})
            if isinstance(out, dict):
                task.data.update(out)      # mirror the real engine — see below
            return out
        ext = dict(external_context or {}); ext["_dispatch"] = _dispatch
        return super().execute(task, script, ext)
```

**The harness must merge handler results into `task.data`, exactly as the real
`_dispatch` does (§5.2).** Omit that and no gateway downstream of a service task
can see the flag it branches on, so the test can't reach — let alone assert —
any branch that depends on a handler's return value. Passing a `results` map
also lets one test drive each branch by stubbing what a handler returns.

Assert: which handlers fired, in what order; that each gateway branch is
reachable; that every path terminates. **One test module per process** — 26 of
them here.

### 10.2 Handler contract tests — against real MySQL

These exist because of a production incident, and the reasoning is worth
copying verbatim:

> **Why MySQL and not SQLite:** SQLite silently ignores `VARCHAR(n)` limits, so
> the `state_transitions.object_id` overflow that wedged the timer sweep is
> invisible there.

Two shipped bugs were caught by nothing:

- A handler wrote `Order.canceled_at`, a column that existed on neither the
  model nor the table → `AttributeError` every tick, forever.
- A composite `object_id` (73 chars) into `varchar(36)` → `DataError 1406`
  every tick, poisoning the session.

**Neither handler had any test that touched a session.** That is the whole
lesson: a handler test with a mocked `db` proves nothing about the two failure
modes that actually take down a sweep. Run them against strict-mode MySQL,
skip cleanly when unreachable.

### 10.3 Integration tests — the real service, the real routers

`WorkflowService` round-tripping through the DB: start → signal → complete task
→ assert domain side effects, `current_states`, and the `state_transitions`
sequence. Plus end-to-end webhook and checkout paths, and a parent/child
orchestration test that drives real routers (create project → two task children
→ mark both done → assert the parent advanced to `wrapping`).

### 10.4 The rule that catches what green suites miss

**Mutation-test the guard, not just the fix.** Break the code deliberately and
confirm the test fails. In this project, ten tests matched their brief while
being structurally incapable of failing; every one was found by breaking the
code, none by a passing run. For BPM specifically: delete a handler's DB write
and confirm the test goes red; if it stays green, the test is asserting on
workflow data rather than on the effect.

---

## 11. Operations

### 11.1 Startup sequence

```python
@app.on_event("startup")
def _bpm_startup():
    # 1. Route bpm.* loggers into uvicorn's handlers (else logs vanish — G12)
    bpm_log = logging.getLogger("bpm"); bpm_log.setLevel(logging.INFO)
    if not bpm_log.handlers:
        for h in logging.getLogger("uvicorn").handlers: bpm_log.addHandler(h)

    # 2. Sync .bpmn files → process_definitions
    db = SessionLocal()
    try: bpm_loader.sync_definitions(db); db.commit()
    finally: db.close()

    # 3. Start the 60s timer tick
    bpm_timer.start()

    # 4. Site-specific hook (operational sweeps)
    if hasattr(_site_register, "on_startup"):
        try: _site_register.on_startup()
        except Exception: log.exception("site on_startup() failed")   # never abort startup
```

Handler modules must be **imported before this runs** (top-of-`main.py`
imports of `app.core.bpm_tasks` and the site's `bpm_tasks`), or the registry is
empty and the first transition through any service task fails.

### 11.2 Deploy

Deploying a `.bpmn` change is: commit → deploy → restart. The loader appends a
new version on startup. **Restart is required** — the definitions are read at
startup only, and a live-reload dev server does not reliably pick up a new
`.bpmn`.

Also: **a deploy script that restarts the service does not `pip install`.** A
new engine dependency, or a migration that imports SpiffWorkflow, crashes
*before* the service boots — and migrations run first, so the usual
startup-failure signature doesn't apply.

### 11.3 Versioning and in-flight instances

- New versions apply to **new instances only**. Running instances stay pinned
  via `process_instances.process_definition_id`.
- **Prefer additive changes.** Adding a branch or a new terminal state is safe.
- Changing the shape *around* a task that instances are currently parked on is
  not. If a change must reach in-flight instances, write a one-shot migration
  that loads each running instance, upgrades its serialized state, and re-saves.
  Spiff has utilities; the migration is still manual.
- The cheap alternative that usually wins: let the old instances drain on the
  old version.

### 11.4 Migration hazards (MySQL)

**MySQL commits DDL immediately.** A migration that adds a column and then
fails partway through a backfill leaves the column committed and
`alembic_version` un-stamped, so a re-run dies on "duplicate column."
Make migrations idempotent, or validate the backfill against a fresh copy of
production data before shipping.

**Validate before destroying, in both directions.** A `downgrade()` here
dropped a column *before* narrowing `object_id` back to 36 chars. Against real
data (a 73-char composite id) the narrowing failed — but the drop had already
auto-committed and alembic had not stamped, so `upgrade head` became a no-op
and every ORM `SELECT` on the table 500'd until someone hand-wrote DDL. The fix:
**check every value that would be truncated and raise before touching the
schema**, then narrow, then drop last. Reachable by following the documented
rollback command, which is what made it worth a real fix rather than a note.

### 11.5 What to monitor

| Signal | Why |
|---|---|
| `process_instances` where `status='error'` | Something needs a human |
| Instances `running` with `updated_at` older than N days | Zombie — a catch event nothing will ever signal |
| Timer-tick **error rate** (not just crashes) | The 11.5k-tracebacks/day failure mode had zero restarts and green health checks |
| Tick duration vs the 60s interval | Overlap means the sweep is falling behind |
| `workflow_tasks` ready count by role, aged | An inbox nobody is working |
| Definition version churn | A `.bpmn` reformatted on every deploy appends a version each time |

---

## 12. Gotchas

Every one of these was a production incident or a multi-hour debugging session.
This is the highest-value section in the document.

> **Cross-reference note:** the loop-back rule below is referred to elsewhere in
> the reference implementation's own docs as **"Gotcha #17"** from an earlier
> numbering. It is **G7** here. Both labels refer to the same rule.

### Engine semantics

**G1. Service-task handlers must flush, never commit.** The workflow is
serialized *after* the handler returns. A `commit()` inside a handler persists a
half-stepped workflow, and if a later step in the same transition fails you
cannot roll back to a consistent state. Handlers flush; the router commits.

**G2. Feed BPMN XML to the parser as bytes.** lxml refuses a unicode string
carrying `<?xml ... encoding="UTF-8"?>`:

```python
parser.add_bpmn_str(path.read_text())    # ValueError: Unicode strings with encoding declaration…
parser.add_bpmn_str(path.read_bytes())   # correct
```

**G3. Handler registration is an import side effect.** `@service_task(...)`
registers on import. `main.py` must import every handler package at startup, and
**so must any migration or standalone script that advances a workflow** —
otherwise `RuntimeError: no Python handler registered for service task '…'` the
first time that task runs.

**G4. Workflow data must be seeded in three places.** Spiff evaluates
expressions as `evaluate(expr, task.data, external_context=workflow.data_objects)`.
`workflow.data` alone is *not* consulted. Seed `workflow.data`,
`workflow.data_objects`, **and** the root start task's `data` (which propagates
by inheritance). For signal payloads, also update every waiting/ready task's
local data — the condition evaluates against the task about to fire. Symptom:
a gateway condition that "can't see" a variable you know you set.

**G5. Spiff does not parse the `camunda:` namespace.** `camunda:candidateGroups`
and `camunda:assignee` arrive as `extensions = {}`. Walk the XML with lxml after
parsing and graft the values onto each user-task spec. Also: `camunda:assignee`
holds the **name of a workflow variable**, not a value and not a template —
keep it a bare name, no `${...}`.

**G6. Signals need a `BpmnEvent` wrapper in 3.x.** `workflow.signal(name)` is
1.x/2.x and is gone. `workflow.catch(...)` **does still exist** in 3.1.2 and is
not deprecated — only its argument type changed, from a bare event definition to
a `BpmnEvent`. The two are not interchangeable: `catch` **queues** an event that
nothing is currently waiting on, while `send_event` **raises** if no task
consumes it. Prefer `send_event`, so that signalling a workflow that isn't
listening surfaces instead of vanishing — which is why every `signal_parent_*`
helper wraps it in a `try/except` (§6.4.4).

```python
workflow.send_event(BpmnEvent(SignalEventDefinition(name)))   # raises if unconsumed
workflow.catch(BpmnEvent(SignalEventDefinition(name)))        # queues silently
```

**G7. Do not loop a token back through a *signal* catch event.** (Historically
"Gotcha #17".) The rule stands; the mechanism is not what it was long recorded
as. **It does not "terminate the workflow."** Measured against 3.1.2:

| Loop shape | What actually happens |
|---|---|
| Bare cycle, no gateway | `RecursionError` inside `BpmnWorkflow.__init__` — at *construction*, before any signal is sent |
| Cycle through an exclusive gateway | Runs. Never terminates. But each iteration leaks **~6 permanent `Task` objects and ~2.2 KB of `serialized_state`** — 7.0 KB → 28.6 KB at 10 iterations → 50.3 KB at 20, linear and forever |
| Re-entered catch event's task data | Predicted loop copies are created with **empty data**, so the gateway condition that gates the loop `NameError`s (G9) and the task lands in `ERROR` |

Unbounded growth of the column deserialized on *every* 60-second tick is the
real argument, and it is a much stronger one than "it terminates." Model
repeated/arbitrary transitions as Shape B (§6.4.2): a durable anchor parked at a
catch state, advancing only on **forward/terminal** signals, with everything
else as `state_transitions` ledger rows.

**Nuance:** *timer* loop-backs are fine and run in production (§6.4.5) — the
poll loop is bounded by its own exit gateway and each iteration's data is
re-established by the service task. It is signal catch events and cancellable
parallel-branch loops that break.

**G8. A name-keyed progress snapshot misses a loop — and the obvious fix has its
own blind spot.** A poll loop ends each iteration at the same `task_spec.name`,
so a before/after diff keyed on names sees no change and never persists
(verified: names identical every iteration while completed-count went
4→7→10→13→18). Use a monotonic count of `COMPLETED` tasks.

**But do not compute that count with `get_tasks(state=TaskState.COMPLETED)`.**
Spiff's `TaskIterator` sets `min_state` and stops descending at the first
ancestor that doesn't meet it, so **any WAITING node hides every completed task
beneath it**. On an event-based gateway the gateway itself stays WAITING, so the
filtered count reports no progress at all (measured: filtered 3→3 while the true
count went 3→6). Scan every task and filter in Python:

```python
# WRONG — a WAITING ancestor hides everything below it
sum(1 for _ in workflow.get_tasks(state=TaskState.COMPLETED))

# RIGHT
sum(1 for t in workflow.get_tasks() if t.state == TaskState.COMPLETED)
```

Getting this wrong costs more than a missed persist: see G23.

**G9. Gateway conditions `NameError` on unset variables.** An unset name is not
falsy — it is an exception that kills the transition. Every branch of a service
task must return **all** the flags any downstream gateway might reference
(§5.7's helpers set `_ok`, `_failed`, and `_retry_pending` on every path).

**G10. Loop-body service tasks must short-circuit on every terminal and
post-loop state.** `svc_poll_stream_status` short-circuited only on
`state == 'ready'`; run against already-`published` rows during a backfill it
fell through, called the external API, and **regressed `published` back to
`ready`**. Enumerate every state in which the task should do nothing:

```python
if video.state in ("ready", "published", "unlisted", "archived"):
    return {"stream_ready": True, ...}
if video.state == "error":
    return {"stream_errored": True, ...}
```

**G11. Timer durations inside `<bpmn:timeDuration>` are Python expressions.**
Spiff evaluates the body and expects the *result* to be an ISO-8601 string. So
`PT30S` is a `NameError`; `"PT30S"` (with quotes, inside the XML) is correct.

### Runtime / operations

**G12. uvicorn does not configure the root logger.** `logging.getLogger("bpm.timer").info(...)`
vanishes silently. Attach uvicorn's handlers to the `bpm` logger at startup or
you will debug the timer sweep blind.

**G13. Multi-worker APScheduler races.** `--workers N` runs the startup event in
each child, so the scheduler starts N times and every tick fires N times.
Currently idempotent-but-wasteful (each tick re-advances freshly-deserialized
state); fix with `SELECT … FOR UPDATE SKIP LOCKED` per instance when it matters.

**G14. A permanently-failing instance retries forever, silently.** Two bugs
produced ~11.5k and ~17.3k tracebacks per day for weeks. No process ever
restarted; every health check returned 200. It presented as a crashloop in the
logs without being one — and meanwhile three order workflows could never reach a
terminal state, so **their committed inventory stayed reserved indefinitely**.
Alert on tick error rate; give the sweep a give-up path; treat "stuck instance"
as a first-class monitored state.

**G15. A single-catch wait is usually a zombie waiting to happen.** Any state
that waits on an external system needs an event-based gateway with at least a
timeout branch (§6.4.3). Without it, an abandoned checkout parks the workflow
forever *and* holds whatever it reserved.

### Schema

**G16. `object_id` must be `VARCHAR(100)`, consistently across all three
tables.** Composite lifecycle keys are real (`"{a_id}:{b_id}"` = 73 chars). At
36 you get `DataError 1406`, which poisons the SQLAlchemy session with
`PendingRollbackError` — and if the writer is the timer sweep, forever.

**G17. MySQL auto-commits DDL, so partial migrations leave committed damage.**
Alembic stamps `alembic_version` only after the function returns. Make
migrations idempotent; in a `downgrade()`, **validate that every value survives
a narrowing before touching the schema**, and put destructive steps last.

**G18. `metadata` is reserved by SQLAlchemy's declarative API.** Map it as
`transition_metadata: Mapped[...] = mapped_column("metadata", JSON)`.

**G19. `business_key` is deliberately NOT unique.** Objects can have sequential
lifecycles. Uniqueness is enforced behaviorally (one *running* instance per
`(object_type, object_id)`), and readers must prefer the running instance —
`get_instance()` does.

### Process design

**G20. Compute joins in Python, not in the graph.** "All N children done" where
N is unknown at design time is a `COUNT(*)`, not a BPMN parallel join. The
handler runs the query and signals the parent only when it's true.

**G21. Signalling a parent is always best-effort.** A parent that already
advanced past its catch event, or that never existed, is a normal no-op. Catch
both `LookupError` (no instance) and Spiff's `WorkflowException` (running but
nothing listening).

**G22. Always set `default=` on exclusive gateways, pointing at the safe
branch.** No matching condition and no default is a runtime exception. And the
safe branch is "escalate to a human", not "proceed".

**G23. Every end event of a process containing an `eventBasedGateway` MUST carry
`<bpmn:terminateEventDefinition/>`.** This is the most expensive gotcha in this
document, because two defects compound into the exact incident G14 describes,
and the shape it strands is the one that exists to prevent stranding (§6.4.3).

Without terminate, the losing race arms stay `MAYBE` and the gateway stays
`WAITING`, so `EndJoin` never resolves and the instance never leaves `running`.
On the **timer** arm — the abandonment path — it gets worse: an event-based
gateway's `has_fired` is driven by `seen_events`, which only signals populate, so
a timer never resolves the gateway at all.

Measured on a real order workflow, timer shortened to 1s:

```
BEFORE   handler fired: svc_release_and_cancel     ← domain write COMMITS
         is_completed(): False                      persist gate: False
         stuck: gw_await_payment, EndJoin, evt_payment_captured, … (8 tasks)

AFTER    handler fired: svc_release_and_cancel
         is_completed(): True                       persist gate: True
         stuck: []
```

The persist gate is `False`, so the workflow state is **discarded** while the
handler's domain write **commits**. Next tick the instance deserializes to its
pre-timeout state, the timer is still due, and the handler runs again. Every 60
seconds. Forever. That is a split-brain against Law 4 *and* G14's runaway sweep,
from one missing XML element.

Two further consequences worth knowing:

- **Terminate also un-blinds the progress metric.** Cancelling the WAITING
  gateway lets the task iterator descend, so even the naive filtered count in G8
  starts reporting correctly. Fix both anyway — they fail independently.
- **Existing instances do not get the fix.** Running instances stay pinned to
  their deployed definition version (§11.3), so anything already stranded stays
  stranded and keeps re-running its handler. After deploying a terminate fix,
  audit `process_instances WHERE status='running'` with an old `updated_at` and
  cancel the strays by hand.

Guard it statically — the rule is mechanical, so a test can enforce it across
every definition rather than trusting review (see also G24–G26, which were found
by *implementing* these fixes rather than by reviewing the document):

```python
for path in all_bpmn():
    root = ET.fromstring(path.read_text())
    if root.find(f".//{{{BPMN_NS}}}eventBasedGateway") is None:
        continue
    for end in root.iter(f"{{{BPMN_NS}}}endEvent"):
        assert end.find(f"{{{BPMN_NS}}}terminateEventDefinition") is not None
```

> **On the XML parser.** Stdlib `ElementTree` is fine for definitions that live
> in your own repo — version-controlled, authored by you, never user-supplied.
> **If your product ever lets users upload `.bpmn` files**, that stops being
> true: switch every parse path (the loader in §5.4 included) to
> `defusedxml.ElementTree`, or you have handed them XXE and entity-expansion
> against the process that runs your workflows.

**G24. `camunda:candidateGroups` is a LIST — keep all of it.** Projecting only
`split(",")[0]` into `assignee_role` means a task declaring
`candidateGroups="admin,senior_instructor"` is stored as `admin`, so senior
instructors never see it in their inbox and get 403 trying to claim it. The
second and subsequent groups vanish with no error anywhere.

Store the whole comma-separated list and match with a helper, in **every** place
that reads it — the inbox query, the task-visibility check, claim,
`available_actions`, and any agent/MCP filter:

```python
def role_names_of(assignee_role):            # "admin,senior_instructor"
    return [r.strip() for r in (assignee_role or "").split(",") if r.strip()]

def user_matches_role(assignee_role, user):
    return any(user.has_role(r) for r in role_names_of(assignee_role))
```

Note this breaks SQL `IN` matching on the column, so the inbox query must narrow
in SQL and refine in Python (inboxes are small; correctness wins). Single-role
rows written before the change split to a one-element list, so the migration is
free.

**G25. An errored instance is permanently, silently deaf.** This is the most
dangerous emergent behaviour in the design, because three separate "correct"
decisions compose into it:

```
_advance()               a handler raises  →  instance.status = "error"
signal_by_correlation()  .filter(status == "running")  →  no match  →  None
webhook                  if instance is not None: ...  ← no else, no log
```

**One handler exception disconnects a workflow from every future event, for
ever, without a single log line.** Traced in production: an order's payment
declined at T+7min, the cancel handler raised on a missing column, the instance
went to `error` — and the `payment_intent.succeeded` that arrived 3.5 minutes
later was discarded. The order was paid and shipped; its workflow sat frozen at
the payment gateway for three months. Nothing anywhere recorded that a signal had
been thrown away.

Mitigations, in order of value:

1. **Log the discard** and distinguish "no instance" from "instance not running"
   (§5.3). Cheapest, and would have surfaced this the same day.
2. **Make `error` recoverable.** If `retry_failed_task` is unimplemented (as it
   is here — §14.3), an errored instance is *both* invisible and unfixable.
3. **Alert on `status='error'`.** Nothing surfaces it; three instances sat
   there for months.
4. Consider whether signals should reach errored instances at all, or whether
   an error should park the instance in a state that can still catch events.

**G26. A handler must re-derive safety from the domain object, never trust its
trigger.** A signal describes what was true when it was *sent*; a handler runs
when it *runs*, which may be much later — after a retry, a re-driven instance,
or a sweep. In between, the object moves on.

Concretely: `payment_failed` routed straight to "cancel the order and release
its inventory". But a failed payment attempt is retryable — the buyer tried
another card and succeeded — so by the time a re-run reached the handler the
order was paid and shipped. Cancelling on the strength of the stale trigger
would have released the stock of goods already dispatched.

So a destructive handler needs a guard derived from the object's *current*
state, not from the reason it was invoked:

```python
def _cancel_block_reason(order):
    if order.payment_status in PAID_STATUSES:      return f"payment_{order.payment_status}"
    if order.fulfillment_status in SHIPPED_STATUSES: return f"fulfillment_{order.fulfillment_status}"
    if order.state == "canceled":                  return "already_canceled"
    return None
```

This is G10 (short-circuit on every terminal state) generalised from loop bodies
to **any handler reachable more than once or late**. Log the refusal, so a guard
that fires is visible rather than silent.

---

## 13. Rollout plan for a new product

Ordered so each phase is independently shippable and each one de-risks the
next.

**Phase 0 — infrastructure.** `pip install SpiffWorkflow APScheduler`. Create
the four core tables (§4). Scaffold the six engine modules (§5). Add
`/api/workflows` and `/api/tasks` skeletons. Wire startup (§11.1). **Gate:** a
trivial two-step `.bpmn` starts, persists, and completes via
`WorkflowService`.

**Phase 1 — first process, Shape A.** Pick the simplest real
review-and-approve lifecycle. Author the BPMN, implement handlers, replace the
existing publish endpoint with `wf.start(...)` + a user task. **Backfill
`process_instances` for existing rows** so history is complete from day one.
Write the contract test (§10.1). **Gate:** end-to-end through the real UI.

**Phase 2 — timers, Shape E.** A process with a polling loop. This is where
the timer tick, G8, G10 and G11 all get exercised. **Gate:** an instance
advances with no HTTP request involved.

**Phase 3 — the inbox.** `/tasks` UI plus the generic frontend helper (§7.4,
§7.5). **Gate:** a user completes a task from the inbox and the object
advances.

**Phase 4 — the remaining lifecycles.** One process per PR. By now the shapes
are known; this is throughput. Enforce the laws (§1.2) as you convert each one:
delete the direct `status =` mutations.

**Phase 5 — external systems.** Webhooks (§7.2), the retry protocol (§5.7),
event-based gateways (§6.4.3). **Gate:** a provider webhook advances a workflow,
and a duplicate/late webhook is a clean no-op.

**Phase 6 — admin ops.** Instance list, history, cancel, retry; optionally a
`bpmn-js` diagram with the current token highlighted. **Gate:** an operator can
diagnose a stuck instance without a SQL client.

**Phase 7 — orchestration.** Parent/child with a computed join (§6.4.4), if the
domain has containment. **Gate:** a parent advances from an aggregate condition
across children.

**Phase 8 — sweeps and AI.** Operational cadence (§7.3); AI service tasks with
the fake-first fast path (§9).

**Retrofitting an existing app?** The order that worked: convert the *newest*
lifecycle first (least legacy behavior to preserve), then the one with the most
support burden. Expect a "drift" cleanup pass — the parent/child retrofit here
needed a final task purely to close the last silent `status` mutations, which
is normal and worth budgeting for.

---

## 14. The reference implementation — scope and honest gaps

Reference material, not requirements. Two uses: calibrating how much surface a
mature BPM layer actually has, and — via §14.2 — knowing which shape to reach
for.

### 14.1 Scope

**34 process definitions**, which is a useful sense of scale for "every
lifecycle is a process" in a mature product:

| Where | Count | Processes |
|---|---|---|
| Shared core — commerce | 10 | `order`, `payment_intent`, `refund`, `refund_reversal`, `shipment`, `dispute`, `cart_recovery`, `connected_account`, `entity_transfer`, `payout_reconciliation` |
| Site A — education / training | 15 | `blog_post`, `video`, `course`, `course_enrollment`, `assessment`, `assessment_attempt`, `booking`, `availability_slot`, `instructor_onboarding`, `instructor_certification`, `gear_item`, `gear_share`, `message_thread`, `user_account`, `stripe_review` |
| Site C — project management | 7 | `project_lifecycle` (parent), `task_lifecycle`, `contract_signing`, `expense_approval`, `invoice_payment`, `consultation_intake`, `testimonial_review` |
| Site B — events / storefront | 2 | `video`, `stripe_review` |

**Tests:** 25 modules — 20 `*_process.py` BPMN contract tests, plus
service-task contract tests against real MySQL, webhook integration, checkout
integration, admin integration, and timer compatibility.

### 14.2 Which shape to reach for

| Want to model… | Shape | Canonical example |
|---|---|---|
| Review + approve | A (§6.4.1) | `blog_post` |
| Unbounded / repeatable / per-actor transitions | B (§6.4.2) | `message_thread` |
| Waiting on an external system that may never answer | C (§6.4.3) | `order` |
| Containment with an aggregate join | D (§6.4.4) | `project_lifecycle` + its children |
| Polling an async external job | E (§6.4.5) | `video` |
| Machine first-pass with human fallback | A + §9 | `stripe_review` |
| Transient-failure retry inside a handler | §5.7 | `payment_intent` |
| External events driving transitions | §7.2 | the storefront webhook router |
| Frontend integration | §7.4 | the shared workflow JS helper |

### 14.3 Known gaps

Stated so nobody assumes coverage. Each is a decision a fresh implementation
should make deliberately rather than inherit:

- **`retry_failed_task` is unimplemented** (`NotImplementedError`; the admin
  endpoint returns 501). Recovery is fix-and-redeploy plus the 60s tick — which
  works only because handlers are idempotent, and is the practical reason they
  must be.
- **`missing_handlers()` is never called at startup**, and scans only the site
  directory, not core. A missing handler therefore surfaces at runtime,
  mid-transition. Wiring it in as a hard startup failure is cheap and worth
  doing on day one.
- **`AIInvocation` is modeled but never written.** No AI cost or latency
  telemetry is captured. Retrofitting this is how you end up unable to answer
  "what did that feature cost us."
- **Multi-worker timer duplication (G13) is unfixed** — wasteful, not incorrect.
- **One core module imports from a site package**, violating the
  core-never-imports-a-site rule (§8). Latent, because no other site mounts it —
  which is exactly how this class of leak survives.
- **`by-key` history is authenticated but not authorized** — see the warning in
  §7.6. Business keys are enumerable.
- **`business_key` shipped as `VARCHAR(100)`** (§4.1 now says 255). Widening it
  needs a migration in **every** tenant's chain, not just one.
- **Index names collide across tables** (`idx_object`, `idx_instance`). Fine on
  MySQL, blocks a PostgreSQL or SQLite port.
- **`workflow_tasks` cannot identify *which* Spiff task a row projects.**
  `task_spec_name` is the only link, which stops being unique the moment a user
  task sits inside a loop or a parallel branch.

### 14.4 Found by an unbriefed review, 2026-08-06

Two independent reviewers — neither briefed by the author, both forbidden from
reading the implementation — worked from this document alone: one rebuilt the
engine from it, one audited it cold. Between them they found five defects that
were live in the reference implementation, **every one of which fails silently**:

| Defect | Impact |
|---|---|
| No `terminateEventDefinition` on any racing process | Every timed-out instance stranded in `running`, handler re-run every 60s forever (G23) |
| Progress metric used the filtered iterator | Sweep discarded real progress behind any WAITING node (G8) |
| `_catchable_signals` read `event_definitions` | `available_actions()` never returned a signal, for any workflow, ever |
| `candidateGroups` kept only the first entry | Multi-group tasks invisible to every role but the first-listed |
| `data_objects.update(seed)` | Dead line with a comment asserting the opposite (§5.3) |

### 14.5 Found by *implementing* those fixes

The review found what was wrong with the document. Applying the fixes to the
running system found more — which is the argument for making an unbriefed
reviewer **build** rather than read:

| Found | Recorded as |
|---|---|
| An errored instance silently swallows every later signal, permanently. One handler exception froze a paid order's workflow for three months | **G25** |
| A handler must re-derive safety from the domain object; a stale trigger nearly cancelled a shipped order | **G26** |
| The `candidateGroups` fix has to reach five call sites, and breaks SQL `IN` matching on the column | **G24** |
| `signal_by_correlation` returning `None` conflates "no workflow" with "diverged workflow" | §5.3, §7.2 |

None of these is visible from the document alone. They only appear when the
fixes meet real data.

All five are corrected in the text above. The lesson generalises past BPM:
**a briefed reviewer inherits the briefer's blind spots.** Every one of these
had survived normal review, a passing test suite, and months in production —
because none of them raises. Run one unbriefed pass on anything load-bearing,
and make it build the thing rather than read about it.

---

## 15. References

**Engine**
- [SpiffWorkflow docs](https://spiff-workflow.readthedocs.io/) · [GitHub](https://github.com/sartography/SpiffWorkflow)
- [SpiffWorkflow — timer events](https://spiff-workflow.readthedocs.io/en/stable/bpmn/events.html#timer-events)
- [spiff-arena](https://github.com/sartography/spiff-arena) — the full platform; useful for UI ideas
- [Camunda Platform](https://camunda.com/) — the fallback if the engine ever hits a ceiling

**BPMN**
- [Camunda Modeler](https://camunda.com/download/modeler/) — the editor
- [Camunda BPMN primer](https://docs.camunda.io/docs/components/modeler/bpmn/bpmn-primer/) — clearest element reference
- [BPMN 2.0 specification (OMG)](https://www.omg.org/spec/BPMN/2.0/)
- [bpmn-js](https://github.com/bpmn-io/bpmn-js) — browser renderer for a token-highlighted admin view
- [Workflow Patterns](http://www.workflowpatterns.com/) — the academic catalog underlying every BPM engine

**AI service tasks**
- [Tool use / structured outputs](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
- [Prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) — required for cost control on repeated system prompts
- [Message Batches API](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing) — shadow eval, bulk re-classification
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)

**Lineage** — the idea is not new. Vitria BusinessWare (1994) pioneered
modeling business processes as state machines with executable action code on
each transition; BPML → BPEL → BPMN 2.0 followed, and every modern engine
(Camunda, Zeebe, Activiti, jBPM, Flowable, SpiffWorkflow) descends from it.
- [Vitria BusinessWare](https://vitria.com/businessware/) · [US Patent 7,120,896](https://patents.google.com/patent/US7120896)
- [BPML](https://en.wikipedia.org/wiki/Business_Process_Modeling_Language) · [BPMN](https://en.wikipedia.org/wiki/Business_Process_Model_and_Notation)
- [State Machines and Business Process and Workflow](https://dobbse.net/thinair/2004/10/business-process.html)

**Companion specs in this repo**
- `docs/mcp.md` — **the agent-facing tool surface over `WorkflowService`.** Subtitled
  "Admin Tooling over the BPMN Spine"; §7.7 here is its one-paragraph summary. Read it
  next if the product should be operable by an AI agent, not just by humans.
- `docs/mcp-why.md` — the case for that surface, in concrete scenarios.
- `docs/mcp-setup.md` — connecting Claude Code / Desktop / remote clients to it.
