"""§10.1 contract test for blog_post.bpmn (Shape A). One module per process."""
from __future__ import annotations

from tests.harness import completed_bpmn_ids, load, ready_user_tasks


def test_parks_on_the_review_task_after_the_first_service_task():
    wf, eng = load("blog_post", seed={"post_id": "p1"})
    assert eng.calls == ["svc_mark_pending_review"]
    assert [t.task_spec.bpmn_id for t in ready_user_tasks(wf)] == ["user_admin_review_post"]
    assert not wf.is_completed()


def test_approve_branch_reaches_publish_and_terminates():
    wf, eng = load("blog_post", seed={"post_id": "p1"})
    t = ready_user_tasks(wf)[0]
    t.data.update({"decision": "approve"})
    t.run()
    wf.do_engine_steps()
    assert eng.calls == ["svc_mark_pending_review", "svc_publish_post"]
    assert "end_published" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_reject_branch_reaches_reject_and_terminates():
    wf, eng = load("blog_post", seed={"post_id": "p1"})
    t = ready_user_tasks(wf)[0]
    t.data.update({"decision": "reject"})
    t.run()
    wf.do_engine_steps()
    assert eng.calls == ["svc_mark_pending_review", "svc_reject_post"]
    assert "end_rejected" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_unknown_decision_takes_the_default_safe_branch():
    """G22: no matching condition and no default is a runtime exception; with
    default= it routes to the safe branch."""
    wf, eng = load("blog_post", seed={"post_id": "p1"})
    t = ready_user_tasks(wf)[0]
    t.data.update({"decision": "banana"})
    t.run()
    wf.do_engine_steps()
    assert eng.calls[-1] == "svc_reject_post"
    assert wf.is_completed()


def test_every_branch_is_reachable_and_every_path_terminates():
    ends = set()
    for decision in ("approve", "reject", "banana"):
        wf, _ = load("blog_post", seed={"post_id": "p1"})
        t = ready_user_tasks(wf)[0]
        t.data.update({"decision": decision})
        t.run()
        wf.do_engine_steps()
        assert wf.is_completed()
        ends |= {n for n in completed_bpmn_ids(wf) if n.startswith("end_")}
    assert ends == {"end_published", "end_rejected"}
