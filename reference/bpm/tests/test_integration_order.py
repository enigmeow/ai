"""§10.3 integration — Shape C through the real service, plus §7.2 webhooks,
G25 and G26."""
from __future__ import annotations

import logging

import pytest

from app.bpm.service import WorkflowService
from app.models.domain import InventoryItem, Order
from app.models.workflow import ProcessInstance
from app.webhooks import handle_stripe_event

SEED = {"sku": "SKU-1", "qty": 2}


def _start(db, order):
    return WorkflowService(db).start(
        "order", object_type="order", object_id=order.id,
        data={"order_id": order.id, **SEED},
    )


def test_start_reserves_inventory_and_parks_on_the_race(db, order_fixture):
    inst = _start(db, order_fixture)
    db.commit()
    assert inst.status == "running"
    assert db.get(InventoryItem, "SKU-1").reserved == 2
    assert db.get(Order, order_fixture.id).inventory_reserved == 2
    assert set(inst.current_states) == {"gw_await_payment", "timer_payment_timeout"}


def test_available_actions_offers_the_race_signals(db, order_fixture, admin):
    _start(db, order_fixture)
    db.commit()
    actions = WorkflowService(db).available_actions(f"order:{order_fixture.id}", admin)
    assert sorted(a["signal_name"] for a in actions if a["kind"] == "signal") == [
        "canceled", "order_payment_failed", "payment_captured",
    ]


def test_webhook_captures_payment_and_completes(db, order_fixture):
    _start(db, order_fixture)
    db.commit()
    out = handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {"id": "pi_1"})
    db.commit()
    assert out == {"signaled": ["order:payment_captured"]}
    o = db.get(Order, order_fixture.id)
    assert (o.state, o.payment_status, o.inventory_reserved) == ("paid", "paid", 0)
    assert db.get(InventoryItem, "SKU-1").reserved == 0
    inst = WorkflowService(db).get_instance(f"order:{order_fixture.id}")
    assert inst.status == "completed"


def test_a_late_duplicate_webhook_is_a_clean_no_op(db, order_fixture, caplog):
    """§7.2 rule 3 — a webhook arriving for a finished workflow is normal."""
    _start(db, order_fixture)
    db.commit()
    handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {})
    db.commit()
    with caplog.at_level(logging.DEBUG, logger="bpm.service"):
        out = handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {})
    db.commit()
    assert out == {"signaled": []}
    # G25 mitigation 1: the discard is LOGGED, and as the "diverged" kind.
    assert "DROPPED" in caplog.text
    assert "none are running" in caplog.text


def test_a_webhook_for_an_object_with_no_workflow_logs_at_debug_not_warning(db, order_fixture, caplog):
    with caplog.at_level(logging.DEBUG, logger="bpm.service"):
        out = handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {})
    assert out == {"signaled": []}
    assert "no instance for" in caplog.text
    assert "DROPPED" not in caplog.text          # routine, not diverged


def test_g26_a_stale_payment_failed_does_not_cancel_a_paid_order(db, order_fixture):
    """G26, in the exact form the spec describes: `payment_failed` used to route
    straight to cancel-and-release. The buyer retried with another card and
    succeeded, so by the time the handler runs the order is paid. Cancelling on
    the strength of the stale trigger would release stock already dispatched."""
    _start(db, order_fixture)
    db.commit()

    # the buyer's second card succeeded, out of band
    o = db.get(Order, order_fixture.id)
    o.payment_status = "paid"
    o.fulfillment_status = "shipped"
    db.flush()
    db.commit()

    handle_stripe_event(db, "payment_intent.payment_failed", o, {})
    db.commit()

    o = db.get(Order, order_fixture.id)
    assert o.state != "canceled"
    assert o.inventory_reserved == 2                    # NOT released
    assert db.get(InventoryItem, "SKU-1").reserved == 2


def test_g25_an_errored_instance_swallows_every_later_signal(db, order_fixture, caplog, monkeypatch):
    """G25 -- the emergent behaviour, reproduced end to end, then shown to be
    recoverable because this build implements retry_failed_task."""
    import app.bpm_tasks.order as handlers

    _start(db, order_fixture)
    db.commit()
    inst_id = WorkflowService(db).get_instance(f"order:{order_fixture.id}").id

    boom = {"n": 0}

    def exploding(ctx):
        boom["n"] += 1
        if boom["n"] == 1:
            raise AttributeError("'Order' object has no attribute 'canceled_at'")
        return handlers._release_and_cancel(ctx, "admin_canceled")

    monkeypatch.setitem(
        __import__("app.bpm.registry", fromlist=["_REGISTRY"])._REGISTRY,
        "svc_release_and_cancel", exploding,
    )

    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException

    with pytest.raises(WorkflowTaskException):
        WorkflowService(db).signal(f"order:{order_fixture.id}", "canceled")
    db.commit()
    assert db.get(ProcessInstance, inst_id).status == "error"

    # ...and now every later signal is discarded. The mitigation the spec asks
    # for is that it is LOGGED, loudly, as the diverged kind.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="bpm.service"):
        out = handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {})
    assert out == {"signaled": []}
    assert "DROPPED" in caplog.text and "none are running" in caplog.text

    # G25 mitigation 2: make `error` recoverable. NOTE: this only works because
    # `_record_error` persists the HALF-STEPPED workflow. §7.6's stated recovery
    # ("fix the handler, deploy, let the 60s tick re-drive") cannot work for a
    # signal-driven failure if the errored state is not saved -- the signal is
    # not durable anywhere and nothing re-delivers it. See FINDINGS Part 3.
    WorkflowService(db).retry_failed_task(inst_id)
    db.commit()
    assert db.get(ProcessInstance, inst_id).status == "completed"
    # ...and G26's guard correctly refuses the (now stale) cancel, because the
    # webhook above already marked the order paid.
    assert db.get(Order, order_fixture.id).state != "canceled"
    assert db.get(Order, order_fixture.id).payment_status == "paid"


def test_admin_cancel_signal_releases_inventory(db, order_fixture, admin):
    _start(db, order_fixture)
    db.commit()
    WorkflowService(db).signal(f"order:{order_fixture.id}", "canceled", actor=admin)
    db.commit()
    o = db.get(Order, order_fixture.id)
    assert o.state == "canceled"
    assert o.inventory_reserved == 0
    assert db.get(InventoryItem, "SKU-1").reserved == 0
    events = [t.event for t in WorkflowService(db).get_history("order", order_fixture.id)]
    assert events == ["started", "signal", "ended"]


def test_webhook_inline_write_must_not_trip_the_handlers_own_idempotency_guard(db, order_fixture):
    """NOT IN THE SPEC. §7.2 rule 4 ("update the row inline") and G10/G26
    ("short-circuit on the object's current state") collide: if the handler
    guards on `payment_status`, the webhook's own inline write makes it skip
    releasing the inventory reservation on the FIRST run. Silent phantom
    oversell. This test fails if the guard is moved back to `payment_status`."""
    _start(db, order_fixture)
    db.commit()
    assert db.get(InventoryItem, "SKU-1").reserved == 2

    handle_stripe_event(db, "payment_intent.succeeded", order_fixture, {})
    db.commit()

    assert db.get(InventoryItem, "SKU-1").reserved == 0, (
        "inventory still reserved after payment -- the handler short-circuited "
        "on a column the webhook had already written"
    )
    from app.models.domain import EmailMessage
    assert db.query(EmailMessage).filter_by(template="order_paid").count() == 1
