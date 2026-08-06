"""§10.1 contract test for message_thread.bpmn (Shape B)."""
from __future__ import annotations

import pytest
from SpiffWorkflow.exceptions import WorkflowException

from tests.harness import completed_bpmn_ids, load, signal, waiting_bpmn_ids


def test_parks_at_the_single_durable_anchor():
    wf, eng = load("message_thread", seed={"thread_id": "t1"})
    assert eng.calls == []
    assert waiting_bpmn_ids(wf) == ["state_alive"]
    assert not wf.is_completed()


def test_the_terminal_signal_advances_and_ends():
    wf, eng = load("message_thread", seed={"thread_id": "t1"})
    signal(wf, "delete_thread")
    assert eng.calls == ["svc_delete_thread"]
    assert "end_deleted" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_the_anchor_has_exactly_one_outgoing_transition():
    """Shape B's whole point: the anchor advances only on a FORWARD or TERMINAL
    signal. Anything unbounded/repeatable is a ledger row, never an edge (G7)."""
    wf, _ = load("message_thread", seed={"thread_id": "t1"})
    spec = wf.spec.task_specs["state_alive"]
    assert len(spec.outputs) == 1


def test_an_unmodelled_signal_raises_rather_than_silently_vanishing():
    wf, _ = load("message_thread", seed={"thread_id": "t1"})
    with pytest.raises(WorkflowException):
        signal(wf, "archive")   # deliberately NOT a graph edge
