"""§10.3 — integration tests: the real service round-tripping through the DB."""
from __future__ import annotations

import pytest

from app.bpm.service import WorkflowService, start_lifecycle
from app.models.domain import BlogPost, EmailMessage
from app.models.state_transition import StateTransition
from app.models.workflow import ProcessInstance, WorkflowTask


def test_start_persists_and_parks_on_user_task(db, post, author):
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id, "author_id": author.id})
    db.commit()

    assert inst.status == "running"
    assert inst.business_key == f"blog_post:{post.id}"
    assert inst.current_states == ["user_admin_review_post"]

    # Law 4: the denormalized column was written by the HANDLER.
    assert db.get(BlogPost, post.id).state == "pending_review"

    rows = db.query(WorkflowTask).filter_by(process_instance_id=inst.id).all()
    assert len(rows) == 1
    assert rows[0].status == "ready"
    # G24 — BOTH candidate groups survive projection.
    assert rows[0].assignee_role == "admin,senior_instructor"

    events = [t.event for t in wf.get_history("blog_post", post.id)]
    assert events == ["started", "task_started"]


def test_approve_path_publishes_and_ends(db, post, author, admin):
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id, "author_id": author.id})
    db.commit()
    task = db.query(WorkflowTask).filter_by(process_instance_id=inst.id, status="ready").one()

    wf.complete_user_task(task.id, admin, {"decision": "approve"})
    db.commit()

    p = db.get(BlogPost, post.id)
    assert p.state == "published"
    assert p.published_at is not None
    assert db.get(ProcessInstance, inst.id).status == "completed"
    # Law 2: the email side effect lives in the handler.
    assert db.query(EmailMessage).filter_by(template="post_published").count() == 1

    events = [t.event for t in wf.get_history("blog_post", post.id)]
    assert events == ["started", "task_started", "task_completed", "ended"]


def test_default_branch_rejects(db, post, author, admin):
    """G22 — no matching condition takes default=, which points at the SAFE
    branch. Here the form data does not even contain `decision`."""
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id, "author_id": author.id})
    db.commit()
    task = db.query(WorkflowTask).filter_by(process_instance_id=inst.id, status="ready").one()
    wf.complete_user_task(task.id, admin, {"decision": "nonsense", "reject_reason": "off topic"})
    db.commit()

    assert db.get(BlogPost, post.id).state == "rejected"
    assert db.get(BlogPost, post.id).reject_reason == "off topic"


def test_second_candidate_group_can_see_and_complete(db, post, author, senior):
    """G24 in anger: `senior_instructor` is the SECOND group. Keeping only the
    first would make this task invisible to them and 403 the completion."""
    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()

    inbox = wf.get_inbox(senior)
    assert [t.task_spec_name for t in inbox] == ["user_admin_review_post"]

    wf.complete_user_task(inbox[0].id, senior, {"decision": "approve"})
    db.commit()
    assert db.get(BlogPost, post.id).state == "published"


def test_unrelated_role_cannot_see_or_complete(db, post, author):
    from tests.conftest import mkuser

    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()
    outsider = mkuser(db, "outsider", "consumer")
    assert wf.get_inbox(outsider) == []
    task = db.query(WorkflowTask).filter_by(status="ready").one()
    with pytest.raises(PermissionError):
        wf.complete_user_task(task.id, outsider, {"decision": "approve"})


def test_available_actions_lists_the_user_task(db, post, author, admin):
    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()
    actions = wf.available_actions(f"blog_post:{post.id}", admin)
    assert [a["kind"] for a in actions] == ["user_task"]
    assert actions[0]["task_spec_name"] == "user_admin_review_post"


def test_start_refuses_a_second_running_instance(db, post, author):
    """G19/§4.1 — business_key is not unique; uniqueness is behavioural."""
    wf = WorkflowService(db)
    wf.start("blog_post", object_type="blog_post", object_id=post.id,
             actor=author, data={"post_id": post.id})
    db.commit()
    with pytest.raises(ValueError):
        wf.start("blog_post", object_type="blog_post", object_id=post.id, actor=author)


def test_sequential_lifecycles_are_allowed_and_get_instance_prefers_running(db, post, author, admin):
    wf = WorkflowService(db)
    inst1 = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                     actor=author, data={"post_id": post.id})
    db.commit()
    task = db.query(WorkflowTask).filter_by(status="ready").one()
    wf.complete_user_task(task.id, admin, {"decision": "approve"})
    db.commit()

    post.state = "draft"
    db.flush()
    inst2 = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                     actor=author, data={"post_id": post.id})
    db.commit()
    assert inst2.id != inst1.id
    assert wf.get_instance(f"blog_post:{post.id}").id == inst2.id
    assert db.query(ProcessInstance).count() == 2


def test_start_lifecycle_swallows_missing_definition(db, post, author, caplog):
    """§7.1.3 — a workflow that can't start must not break the entity."""
    start_lifecycle(db, process_key="no_such_process", object_type="blog_post",
                    object_id=post.id, actor=author)
    assert db.query(ProcessInstance).count() == 0
    assert "start_lifecycle failed" in caplog.text


def test_cancel_freezes_the_instance(db, post, author, admin):
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id})
    db.commit()
    wf.cancel(f"blog_post:{post.id}", admin, reason="spam")
    db.commit()
    assert db.get(ProcessInstance, inst.id).status == "canceled"
    assert db.query(WorkflowTask).filter_by(status="canceled").count() == 1
    row = db.query(StateTransition).filter_by(event="canceled").one()
    assert row.reason == "spam"
    assert row.actor_user_id == admin.id
    # cancel is mandatory-reason
    with pytest.raises(ValueError):
        wf.cancel(f"blog_post:{post.id}", admin, reason="")
