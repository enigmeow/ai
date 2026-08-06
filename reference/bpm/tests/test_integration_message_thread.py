"""§10.3 integration — Shape B: durable anchor + ledger."""
from __future__ import annotations

import pytest

from app.bpm.service import WorkflowService, ledger_transition
from app.bpm_tasks.message_thread import archive_for_member, unarchive_for_member
from app.models.domain import MessageThread
from app.models.state_transition import StateTransition


def test_anchor_parks_and_the_ledger_records_unbounded_per_actor_transitions(db, thread, author, admin):
    wf = WorkflowService(db)
    inst = wf.start("message_thread", object_type="message_thread",
                    object_id=thread.id, data={"thread_id": thread.id})
    db.commit()
    assert inst.current_states == ["state_alive"]

    # unbounded + repeatable + per-actor: 6 transitions, zero token movements
    for _ in range(3):
        assert archive_for_member(db, thread.id, author.id)
        assert unarchive_for_member(db, thread.id, author.id)
    assert archive_for_member(db, thread.id, admin.id)
    db.commit()

    # the token has not moved
    assert wf.get_instance(f"message_thread:{thread.id}").current_states == ["state_alive"]
    assert db.get(MessageThread, thread.id).state == "alive"

    rows = wf.get_history("message_thread", thread.id)
    assert [r.event for r in rows] == (
        ["started"] + ["archive", "unarchive"] * 3 + ["archive"]
    )
    # every one is attributed
    assert {r.actor_user_id for r in rows if r.event == "archive"} == {author.id, admin.id}
    # and every one is attached to the running lifecycle instance
    assert {r.process_instance_id for r in rows} == {inst.id}


def test_the_terminal_signal_moves_the_token_and_ends(db, thread, admin):
    wf = WorkflowService(db)
    wf.start("message_thread", object_type="message_thread",
             object_id=thread.id, data={"thread_id": thread.id})
    db.commit()
    archive_for_member(db, thread.id, admin.id)
    wf.signal(f"message_thread:{thread.id}", "delete_thread", actor=admin)
    db.commit()

    t = db.get(MessageThread, thread.id)
    assert t.state == "deleted" and t.deleted_at is not None
    assert wf.get_instance(f"message_thread:{thread.id}").status == "completed"
    assert [r.event for r in wf.get_history("message_thread", thread.id)] == [
        "started", "archive", "signal", "ended",
    ]


def test_ledger_transition_is_a_no_op_without_a_running_lifecycle(db, thread, author, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="bpm.service"):
        assert ledger_transition(db, object_type="message_thread", object_id=thread.id,
                                 event="archive", actor_user_id=author.id) is False
    assert db.query(StateTransition).count() == 0
    assert "no running lifecycle" in caplog.text


def test_an_unmodelled_signal_surfaces_rather_than_vanishing(db, thread, admin):
    """G6's reason for preferring send_event: a signal nothing catches must
    surface. `archive` is deliberately not a graph edge (G7/§6.4.2)."""
    from SpiffWorkflow.exceptions import WorkflowException

    wf = WorkflowService(db)
    wf.start("message_thread", object_type="message_thread",
             object_id=thread.id, data={"thread_id": thread.id})
    db.commit()
    with pytest.raises(WorkflowException):
        wf.signal(f"message_thread:{thread.id}", "archive", actor=admin)
