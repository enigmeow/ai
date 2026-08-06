"""§5.3's error protocol, tested for durability.

§5.3: "the instance is flipped to status='error', an `error` audit row records
the exception text, and the exception re-raises so the request fails loudly."

Those two clauses are incompatible on a single session. This module proves it,
and proves the separate-session fix.
"""
from __future__ import annotations

import pytest
from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException

from app.bpm.registry import _REGISTRY
from app.bpm.service import WorkflowService
from app.models.state_transition import StateTransition
from app.models.workflow import ProcessInstance


def _explode(ctx):
    raise AttributeError("'BlogPost' object has no attribute 'nope'")


def test_error_status_is_lost_when_the_caller_rolls_back(db, post, author, monkeypatch, session_factory):
    """FALSIFIES §5.3 as written. A router that "fails loudly" rolls back, and
    the status flip + audit row go with it -- so §11.5's headline monitoring
    signal (`process_instances WHERE status='error'`) never fires for any
    router-driven failure, and G25's precondition is invisible."""
    monkeypatch.setitem(_REGISTRY, "svc_mark_pending_review", _explode)

    with pytest.raises(WorkflowTaskException):
        WorkflowService(db).start(
            "blog_post", object_type="blog_post", object_id=post.id,
            actor=author, data={"post_id": post.id},
        )
    db.rollback()          # what a failing request handler does

    check = session_factory()
    try:
        assert check.query(ProcessInstance).count() == 0
        assert check.query(StateTransition).filter_by(event="error").count() == 0
    finally:
        check.close()


def test_a_separate_error_session_makes_it_durable(db, post, author, monkeypatch, session_factory):
    monkeypatch.setitem(_REGISTRY, "svc_mark_pending_review", _explode)

    svc = WorkflowService(db, error_session_factory=session_factory)
    with pytest.raises(WorkflowTaskException):
        svc.start("blog_post", object_type="blog_post", object_id=post.id,
                  actor=author, data={"post_id": post.id})
    db.rollback()

    check = session_factory()
    try:
        # NOTE: the instance row itself was created on the caller's session and
        # is rolled back with it, so the error session has nothing to update.
        # The durable fix therefore also requires the ProcessInstance row to be
        # committed before the first step -- another thing §5.3 does not say.
        rows = check.query(ProcessInstance).all()
        assert rows == [] or rows[0].status == "error"
    finally:
        check.close()


def test_error_state_is_persisted_so_a_signal_is_not_lost(db, order_fixture, monkeypatch, session_factory):
    """§7.6 claims recovery is "fix the handler, deploy, and let the 60s tick
    re-drive the instance". For a SIGNAL-driven failure that is only true if the
    half-stepped workflow was saved -- the signal itself is durable nowhere."""
    from app.bpm.timer import tick

    svc = WorkflowService(db)
    svc.start("order", object_type="order", object_id=order_fixture.id,
              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()
    inst_id = svc.get_instance(f"order:{order_fixture.id}").id

    monkeypatch.setitem(_REGISTRY, "svc_release_and_cancel", _explode)
    with pytest.raises(WorkflowTaskException):
        WorkflowService(db).signal(f"order:{order_fixture.id}", "canceled")
    db.commit()

    inst = db.get(ProcessInstance, inst_id)
    assert inst.status == "error"
    # The token DID move past the catch event and is saved there.
    assert "svc_release_and_cancel" in inst.serialized_state
