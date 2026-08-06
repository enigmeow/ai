# Reference implementation — BPM engine

A working implementation of [`docs/bpm.md`](../../docs/bpm.md), built **from the
spec alone** by an agent that was forbidden from reading any existing
implementation. Clean-room by construction: it cannot carry anything the
document does not say, because it never saw the system the document describes.

```
122 passed, 13 skipped        SQLite (the 13 need MySQL)
132 passed,  3 skipped        real strict-mode MySQL
~4,700 lines
```

## What it is for

**Reading the spec tells you the design. Running this tells you the design
works.** Three specific jobs:

1. **A conformance suite for the engine.** `tests/test_gotchas_engine.py`
   verifies or falsifies §12's checkable claims directly against SpiffWorkflow
   3.1.2. Point it at a new Spiff release and it will tell you which hazards
   still reproduce — the mechanism that would have caught G6 and G7 being
   wrong for months.
2. **A worked answer to the parts the spec only describes.** §5.3's error
   durability and §7.6's signal recovery are stated as problems with options;
   here they are solved, and you can read the choice.
3. **A propagation check.** A spec change that contradicts working code shows up
   as a failing test rather than a stale paragraph — the failure mode §14.6
   exists to warn about.

## What it is NOT

- **Not production code.** It has never run a real transaction or survived a bad
  deploy. It is correct against the spec, not hardened against the world.
- **Not a drop-in library.** No packaging, no migrations, no auth beyond a stub.
- **Not the system the spec was distilled from.** It makes choices that system
  did not: an invented `workflow_tasks.spiff_task_id`, a separate error session.
  Where the two differ, this one follows the document — which is the point, and
  why differences between them are worth investigating rather than "syncing".

## Running it

```bash
python -m venv .venv && .venv/bin/pip install SpiffWorkflow sqlalchemy pytest
.venv/bin/python -m pytest tests -q                 # SQLite; 13 skip
BPM_TEST_MYSQL_URL=mysql+pymysql://root:pw@127.0.0.1:3306 \
  .venv/bin/python -m pytest tests -q               # all of it
```

The MySQL-only tests are not optional decoration — §10.2 exists because SQLite
silently ignores `VARCHAR(n)` limits, which hides the entire class of overflow
bug that G16 documents.

## Layout

| Path | Spec section |
|---|---|
| `app/models/` | §4 — the five tables |
| `app/bpm/{registry,engine,service,loader,audit,timer,retry}.py` | §5 — the engine |
| `app/bpmn/` | §6 — Shapes A, B, C and E, hand-authored |
| `app/bpm_tasks/` | §5.2 — handlers |
| `app/{api,webhooks,startup}.py` | §7 — integration, plus the HTTP/auth substrate §5 says you must supply |
| `tests/test_process_*.py` | §10.1 — BPMN contract tests, no DB |
| `tests/test_schema_mysql.py` | §10.2 — handler contracts on real MySQL |
| `tests/test_integration_*.py` | §10.3 — through the real service |
| `tests/test_mutation_discipline.py` | §10.4 — executable mutants |
| `tests/test_gotchas_engine.py` | §12 — the conformance suite |

## Provenance

Built 2026-08-06 in a second unbriefed pass. The first pass built from an earlier
draft and found five defects live in the system the spec describes; the
corrections were then re-tested by building again from scratch. Findings from
both rounds are recorded in `docs/bpm.md` §14.4–§14.6 — including the two
corrections that had reached one section and not another, which this build
caught by following the document literally.

Comparing this implementation against the production one afterwards found a
further bug in *production*: a column whose meaning had been widened without its
width being changed. That comparison is the third review technique the exercise
produced, after "review the doc" and "build from the doc": **where two
independent implementations of one spec disagree, one of them is wrong.**
