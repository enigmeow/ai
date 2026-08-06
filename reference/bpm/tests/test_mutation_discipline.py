"""§10.4 — "Mutation-test the guard, not just the fix."

Each test here BREAKS the implementation deliberately and asserts that the
guard it is supposed to be protected by actually goes red. A test that stays
green under mutation is asserting on workflow data rather than on the effect.

These are executable mutants rather than a report, so they stay honest as the
code changes.
"""
from __future__ import annotations

import re

import pytest

from app.bpm.registry import _REGISTRY
from app.bpm.service import WorkflowService
from app.models.domain import BlogPost, InventoryItem, Order
from app.models.workflow import WorkflowTask


# --------------------------------------------------------------- mutant 1
def test_mutant_delete_the_handlers_db_write(db, post, author, admin):
    """§10.4's own example: "delete a handler's DB write and confirm the test
    goes red; if it stays green, the test is asserting on workflow data rather
    than on the effect"."""
    def publish_without_writing(ctx):
        return {"published": True, "post_state": "published"}   # data only

    _REGISTRY["svc_publish_post"], original = publish_without_writing, _REGISTRY["svc_publish_post"]
    try:
        wf = WorkflowService(db)
        inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                        actor=author, data={"post_id": post.id})
        db.commit()
        task = db.query(WorkflowTask).filter_by(status="ready").one()
        wf.complete_user_task(task.id, admin, {"decision": "approve"})
        db.commit()

        # the workflow still says it is done ...
        assert wf.get_instance(f"blog_post:{post.id}").status == "completed"
        # ... but the effect the real test asserts on is absent.
        with pytest.raises(AssertionError):
            assert db.get(BlogPost, post.id).state == "published"
    finally:
        _REGISTRY["svc_publish_post"] = original


# --------------------------------------------------------------- mutant 2
def test_mutant_g24_keep_only_the_first_candidate_group(db, post, author, senior, monkeypatch):
    """Break G24 (`split(",")[0]`) and confirm the inbox test goes red. The bug
    produces NO error anywhere -- the task is simply invisible."""
    import app.bpm.service as svc

    original = svc.WorkflowService._resolve_assignment

    def first_only(self, workflow, task):
        user_id, role = original(self, workflow, task)
        return user_id, (role.split(",")[0] if role else None)

    monkeypatch.setattr(svc.WorkflowService, "_resolve_assignment", first_only)

    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()

    assert db.query(WorkflowTask).one().assignee_role == "admin"     # truncated
    assert wf.get_inbox(senior) == []                                # invisible
    task = db.query(WorkflowTask).one()
    with pytest.raises(PermissionError):                             # and 403
        wf.complete_user_task(task.id, senior, {"decision": "approve"})


# --------------------------------------------------------------- mutant 3
def _deploy_broken_order(db, *, strip_terminate: bool):
    from app.models.workflow import ProcessDefinition

    row = (db.query(ProcessDefinition)
             .filter(ProcessDefinition.process_key == "order")
             .order_by(ProcessDefinition.version.desc()).first())
    xml = row.bpmn_xml.replace('"PT30M"', '"PT1S"')
    if strip_terminate:
        xml = xml.replace("<bpmn:terminateEventDefinition />", "")
    row.bpmn_xml = xml
    db.flush()
    db.commit()


def _count_handler_runs(name):
    calls = []
    original = _REGISTRY[name]

    def counting(ctx):
        calls.append(1)
        return original(ctx)

    _REGISTRY[name] = counting
    return calls, original


def test_mutant_g23_alone_strands_the_instance_but_does_not_re_run_the_handler(
    db, session_factory, order_fixture
):
    """Break G23 ONLY (G8's scan-based count still in place).

    MEASURED RESULT, which REFINES G23: the instance is stranded in `running`
    forever and its inventory stays reserved -- but the handler runs exactly
    ONCE, not "every 60 seconds forever". The runaway sweep needs the G8 defect
    as well. G23 says so in one paragraph ("two defects compound into the exact
    incident") and contradicts it in another ("That is ... G14's runaway sweep,
    from one missing XML element"). The first is correct."""
    import time

    from app.bpm.timer import tick

    _deploy_broken_order(db, strip_terminate=True)
    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()

    calls, original = _count_handler_runs("svc_timeout_release_and_cancel")
    try:
        time.sleep(1.2)
        for _ in range(4):
            tick(session_factory)
        db.expire_all()
        inst = WorkflowService(db).get_instance(f"order:{order_fixture.id}")
        assert inst.status == "running", "without terminate the instance never leaves running"
        assert len(calls) == 1, (
            f"with G8 fixed the handler runs once, not per tick; got {len(calls)}"
        )
    finally:
        _REGISTRY["svc_timeout_release_and_cancel"] = original


# --------------------------------------------------------------- mutant 4
def test_mutant_g23_and_g8_together_reproduce_the_runaway_sweep(
    db, session_factory, order_fixture, monkeypatch
):
    """Break BOTH. This is the actual incident: the persist gate evaluates
    False while the handler's domain write commits, so the sweep re-runs the
    handler every tick, forever."""
    import time

    from SpiffWorkflow.util.task import TaskState

    import app.bpm.timer as timer_mod

    _deploy_broken_order(db, strip_terminate=True)
    monkeypatch.setattr(
        timer_mod, "completed_count",
        lambda w: sum(1 for _ in w.get_tasks(state=TaskState.COMPLETED)),
    )

    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()

    calls, original = _count_handler_runs("svc_timeout_release_and_cancel")
    try:
        time.sleep(1.2)
        for _ in range(4):
            stats = timer_mod.tick(session_factory)
            assert stats["advanced"] == 0, "the persist gate must report no progress"
        assert len(calls) == 4, f"expected one handler run per tick, got {len(calls)}"
        db.expire_all()
        assert WorkflowService(db).get_instance(f"order:{order_fixture.id}").status == "running"
    finally:
        _REGISTRY["svc_timeout_release_and_cancel"] = original


# --------------------------------------------------------------- mutant 5
def test_mutant_g26_drop_the_cancel_guard_and_shipped_stock_is_released(db, order_fixture):
    """Break G26 (remove the current-state guard) and confirm a stale
    payment_failed releases the inventory of an order already paid and shipped."""
    from app.webhooks import handle_stripe_event

    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()
    o = db.get(Order, order_fixture.id)
    o.payment_status, o.fulfillment_status = "paid", "shipped"
    db.flush()
    db.commit()

    def unguarded(ctx):
        order = db.get(Order, ctx.get("order_id"))
        item = db.get(InventoryItem, ctx.get("sku", "SKU-1"))
        item.reserved = max(0, item.reserved - order.inventory_reserved)
        order.inventory_reserved = 0
        order.state = "canceled"
        db.flush()
        return {"canceled": True, "cancel_blocked": None}

    original = _REGISTRY["svc_handle_payment_failed"]
    _REGISTRY["svc_handle_payment_failed"] = unguarded
    try:
        handle_stripe_event(db, "payment_intent.payment_failed", o, {})
        db.commit()
        db.expire_all()
        assert db.get(Order, order_fixture.id).state == "canceled"
        assert db.get(InventoryItem, "SKU-1").reserved == 0, (
            "the mutant should release stock for a SHIPPED order"
        )
    finally:
        _REGISTRY["svc_handle_payment_failed"] = original


# --------------------------------------------------------------- mutant 6
def test_mutant_g5_skip_the_camunda_graft_and_nobody_can_see_the_task(
    db, post, author, admin, monkeypatch
):
    """Break G5 (skip grafting camunda extensions) and confirm the inbox
    empties -- with no error anywhere."""
    import app.bpm.engine as eng

    monkeypatch.setattr(eng, "_inject_candidate_groups", lambda *a, **k: None)

    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()
    assert db.query(WorkflowTask).one().assignee_role is None
    assert wf.get_inbox(admin) == []


# --------------------------------------------------------------- mutant 7
def test_mutant_write_form_data_only_to_the_workflow_root_and_the_gateway_nameerrors(
    db, post, author, admin, monkeypatch
):
    """Break the G4 write (put the user task's form data on `workflow.data`
    only, not on the task about to fire) and confirm the gateway raises.

    NOTE what this mutant reveals about G4's wording. G4 says workflow data must
    be seeded in three places or "a gateway condition can't see a variable you
    know you set". But a HANDLER reading `ctx.get("post_id")` sees
    `workflow.data` fine, because §5.2's three-way ctx merge includes it. Only
    GATEWAY CONDITIONS are blind. G4 conflates the two; the seeding rule is a
    gateway rule, not a handler rule."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException
    from SpiffWorkflow.util.task import TaskState

    import app.bpm.service as svc

    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id})
    db.commit()
    # the handler DID see post_id via workflow.data alone
    assert db.get(BlogPost, post.id).state == "pending_review"

    row = db.query(WorkflowTask).filter_by(status="ready").one()
    workflow = wf._load_workflow(inst, admin)
    workflow.data.update({"decision": "approve"})      # root only -- the mutant
    target = next(t for t in workflow.get_tasks(state=TaskState.READY)
                  if str(t.id) == row.spiff_task_id)
    target.run()
    with pytest.raises(WorkflowTaskException) as e:
        workflow.do_engine_steps()
    assert "decision" in str(e.value)


# --------------------------------------------------------------- mutant 8
def test_mutant_catchable_signals_reading_the_plural_returns_nothing(db, order_fixture, admin):
    """§5.3's own warning: "Reading the plural returns [] for *every* workflow,
    so available_actions() never offers a signal and the buttons never render --
    silently, with no error." Confirmed as a mutant."""
    from SpiffWorkflow.util.task import TaskState

    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()

    svc = WorkflowService(db)
    inst = svc.get_instance(f"order:{order_fixture.id}")
    workflow = svc._load_workflow(inst, admin)

    def plural_only(wf):
        signals = []
        for t in wf.get_tasks(state=TaskState.WAITING):
            for ed in getattr(t.task_spec, "event_definitions", None) or []:
                if type(ed).__name__ == "SignalEventDefinition" and getattr(ed, "name", None):
                    signals.append(ed.name)
        return sorted(set(signals))

    assert plural_only(workflow) == []                       # the shipped bug
    from app.bpm.service import _catchable_signals
    assert _catchable_signals(workflow) != []                # the fix
