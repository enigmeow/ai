"""§7.4–§7.6 — the invented HTTP/auth substrate, including the horizontal-access
hole §7.6 warns about."""
from __future__ import annotations

import pytest

from app import api
from app.bpm.service import WorkflowService
from app.models.domain import BlogPost
from app.models.workflow import WorkflowTask
from tests.conftest import mkuser


@pytest.fixture()
def owned_post_instance(db, post, author):
    api.register_owner_predicate(
        "blog_post",
        lambda d, oid, u: (d.get(BlogPost, oid) or BlogPost(author_id="")).author_id == u.id,
    )
    inst = WorkflowService(db).start("blog_post", object_type="blog_post",
                                     object_id=post.id, actor=author,
                                     data={"post_id": post.id})
    db.commit()
    return inst


def test_claim_locks_a_role_task_to_one_user(db, owned_post_instance, admin, senior):
    task = db.query(WorkflowTask).filter_by(status="ready").one()
    api.claim_task(db, task.id, admin)
    db.commit()
    assert db.get(WorkflowTask, task.id).assignee_user_id == admin.id
    with pytest.raises(api.Forbidden):
        api.claim_task(db, task.id, senior)

    # UNDERSPECIFIED. §7.5's `_visible_to` is `assignee_user_id == user.id OR
    # user.has_role(assignee_role)` -- it does not exclude a CLAIMED task from
    # the other candidates' inboxes. So a claimed task stays in every candidate
    # role's inbox forever, and the only feedback is a 403 on claim. The spec
    # never says which behaviour it wants; this build follows it literally.
    assert [t.id for t in WorkflowService(db).get_inbox(senior)] == [task.id]

    # Worse: with §7.5's predicate used verbatim as the COMPLETE check, the
    # other candidate could complete a task someone else had claimed, which
    # defeats claiming entirely. This build adds the implied guard.
    with pytest.raises(PermissionError):
        api.complete_task(db, task.id, senior, {"decision": "approve"})


def test_claim_refuses_a_non_ready_task_with_conflict(db, owned_post_instance, admin):
    task = db.query(WorkflowTask).filter_by(status="ready").one()
    api.complete_task(db, task.id, admin, {"decision": "approve"})
    db.commit()
    with pytest.raises(api.Conflict):
        api.claim_task(db, task.id, admin)


def test_by_key_history_is_authorized_not_merely_authenticated(db, owned_post_instance, author):
    """§7.6's warning, closed. Business keys are enumerable by construction, so
    an authenticated-only read is a horizontal-access hole."""
    stranger = mkuser(db, "stranger", "consumer")
    key = owned_post_instance.business_key

    assert api.instance_by_key(db, key, author)["history"]      # the owner
    with pytest.raises(api.Forbidden):
        api.instance_by_key(db, key, stranger)
    with pytest.raises(api.Forbidden):
        api.actions_by_key(db, key, stranger)


def test_an_unregistered_object_type_defaults_to_deny(db, order_fixture, author):
    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()
    assert "order" not in api._OWNER_PREDICATES
    with pytest.raises(api.Forbidden):
        api.instance_by_key(db, f"order:{order_fixture.id}", author)


def test_admin_only_endpoints_reject_non_admins(db, owned_post_instance, author, admin):
    with pytest.raises(api.Forbidden):
        api.list_instances(db, author)
    assert api.list_instances(db, admin)
    with pytest.raises(api.Forbidden):
        api.cancel_instance(db, owned_post_instance.business_key, author, "nope")


def test_label_for_strips_the_convention_prefixes():
    """§7.4/§6.2 — the prefixes are load-bearing for the generic frontend."""
    assert api.label_for("svc_publish_post") == "Publish post"      # explicit map
    assert api.label_for("user_admin_review_post") == "admin review post"
    assert api.label_for("end_published") == "published"
    assert api.label_for("timer_wait_30s") == "wait 30s"
    assert api.label_for("unprefixed_name") == "unprefixed name"


def test_state_badge_class_is_derived_from_the_state():
    assert api.state_badge_class("published") == "wf-state wf-state-published"
