"""§10.1 contract test for order.bpmn (Shape C — event-based race + timeout).

These are the tests that would have caught the G23 defect: they assert the
instance actually COMPLETES on each arm, not merely that the right handler fired.
"""
from __future__ import annotations

import time

import pytest
from SpiffWorkflow.util.task import TaskState

from tests.harness import BPMN_DIR, completed_bpmn_ids, load, signal, waiting_bpmn_ids


def _xml(timer='"PT30M"'):
    return (BPMN_DIR / "order.bpmn").read_text().replace('"PT30M"', timer)


def test_reserves_then_parks_on_the_event_gateway():
    wf, eng = load("order", xml=_xml(), seed={"order_id": "o1"})
    assert eng.calls == ["svc_reserve_inventory"]
    # NOT DOCUMENTED IN THE SPEC: behind an eventBasedGateway only the gateway
    # itself and the TIMER arm are WAITING; the signal catch events sit in
    # MAYBE. So `_catchable_signals`, which iterates WAITING tasks, only finds
    # the signals via the *composite* `event_definitions` on the gateway spec --
    # the branch §5.3 describes as "kept only as a fallback".
    assert set(waiting_bpmn_ids(wf)) == {"gw_await_payment", "timer_payment_timeout"}
    states = {
        t.task_spec.bpmn_id: TaskState.get_name(t.state)
        for t in wf.get_tasks() if getattr(t.task_spec, "bpmn_id", None)
    }
    assert states["evt_payment_captured"] == "MAYBE"


def test_catchable_signals_finds_the_race_arms_via_the_composite_definition():
    from app.bpm.service import _catchable_signals

    wf, _ = load("order", xml=_xml(), seed={"order_id": "o1"})
    assert _catchable_signals(wf) == ["canceled", "order_payment_failed", "payment_captured"]


@pytest.mark.parametrize(
    "sig,handler,end",
    [
        ("payment_captured", "svc_capture_payment", "end_paid"),
        ("order_payment_failed", "svc_handle_payment_failed", "end_payment_failed"),
        ("canceled", "svc_release_and_cancel", "end_canceled"),
    ],
)
def test_each_signal_arm_wins_the_race_and_terminates(sig, handler, end):
    wf, eng = load("order", xml=_xml(), seed={"order_id": "o1"})
    signal(wf, sig)
    assert eng.calls == ["svc_reserve_inventory", handler]
    assert end in completed_bpmn_ids(wf)
    # G23: without terminateEventDefinition this assertion is False and the
    # instance is stranded in `running` forever.
    assert wf.is_completed()
    assert [t.task_spec.bpmn_id for t in wf.get_tasks(state=TaskState.WAITING)] == []


def test_timer_arm_wins_when_nothing_else_fires_and_terminates():
    """The abandonment path. G23 measured this shortened to 1s; so do we."""
    wf, eng = load("order", xml=_xml('"PT1S"'), seed={"order_id": "o1"})
    assert not wf.is_completed()
    time.sleep(1.2)
    wf.refresh_waiting_tasks()
    wf.do_engine_steps()
    assert eng.calls == ["svc_reserve_inventory", "svc_timeout_release_and_cancel"]
    assert wf.is_completed()
    assert "end_timed_out" in completed_bpmn_ids(wf)


def test_a_late_signal_after_completion_raises_rather_than_vanishing():
    """G6 — send_event raises if nothing consumes it, which is why every
    best-effort signaller wraps it in try/except."""
    from SpiffWorkflow.exceptions import WorkflowException

    wf, _ = load("order", xml=_xml(), seed={"order_id": "o1"})
    signal(wf, "payment_captured")
    with pytest.raises(WorkflowException):
        signal(wf, "canceled")
