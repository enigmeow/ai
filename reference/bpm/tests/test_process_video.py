"""§10.1 contract test for video.bpmn (Shape E — timer poll loop + §5.7 retry)."""
from __future__ import annotations

import time

import pytest

from tests.harness import BPMN_DIR, completed_bpmn_ids, load, ready_user_tasks, waiting_bpmn_ids


def _xml(timer='"PT30S"'):
    return (BPMN_DIR / "video.bpmn").read_text().replace('"PT30S"', timer)


READY = {
    "svc_poll_stream_status": {
        "stream_ready": True, "stream_errored": False,
        "poll_attempt": 0, "poll_max_attempts": 5,
        "poll_retry_pending": False, "poll_ok": True, "poll_failed": False,
    }
}
ERRORED = {
    "svc_poll_stream_status": {
        "stream_ready": False, "stream_errored": True,
        "poll_attempt": 0, "poll_max_attempts": 5,
        "poll_retry_pending": False, "poll_ok": False, "poll_failed": True,
    }
}
WAITING = {
    "svc_poll_stream_status": {
        "stream_ready": False, "stream_errored": False,
        "poll_attempt": 1, "poll_max_attempts": 5,
        "poll_retry_pending": True, "poll_ok": False, "poll_failed": False,
    }
}
EXHAUSTED = {
    "svc_poll_stream_status": {
        "stream_ready": False, "stream_errored": False,
        "poll_attempt": 5, "poll_max_attempts": 5,
        "poll_retry_pending": True, "poll_ok": False, "poll_failed": False,
    }
}


def test_ready_branch_reaches_the_user_task_then_publishes():
    wf, eng = load("video", xml=_xml(), results=READY, seed={"video_id": "v1"})
    assert eng.calls == ["svc_poll_stream_status"]
    t = ready_user_tasks(wf)[0]
    assert t.task_spec.bpmn_id == "user_owner_publish_decision"
    t.run()
    wf.do_engine_steps()
    assert eng.calls == ["svc_poll_stream_status", "svc_publish_video"]
    assert "end_published" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_error_branch_terminates_immediately():
    wf, eng = load("video", xml=_xml(), results=ERRORED, seed={"video_id": "v1"})
    assert eng.calls == ["svc_poll_stream_status"]
    assert "end_error" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_still_waiting_parks_on_the_timer():
    wf, _ = load("video", xml=_xml(), results=WAITING, seed={"video_id": "v1"})
    assert waiting_bpmn_ids(wf) == ["timer_wait_30s"]
    assert not wf.is_completed()


def test_exhausted_attempts_flag_for_a_human_and_terminate():
    """§5.7's bounded loop: attempt >= max routes to svc_flag_needs_intervention
    rather than polling forever."""
    wf, eng = load("video", xml=_xml(), results=EXHAUSTED, seed={"video_id": "v1"})
    assert eng.calls == ["svc_poll_stream_status", "svc_flag_needs_intervention"]
    assert "end_needs_intervention" in completed_bpmn_ids(wf)
    assert wf.is_completed()


def test_a_timer_loop_back_actually_loops_and_makes_progress():
    """G7's nuance: TIMER loop-backs are fine. G8's claim: the loop ends each
    iteration at the SAME task-spec name, so a name-keyed progress snapshot
    sees nothing while the completed-count climbs. Both measured here."""
    from SpiffWorkflow.util.task import TaskState

    wf, eng = load("video", xml=_xml('"PT1S"'), results=WAITING, seed={"video_id": "v1"})
    names, counts = [], []
    for _ in range(3):
        time.sleep(1.05)
        names.append(sorted(waiting_bpmn_ids(wf)))
        counts.append(sum(1 for t in wf.get_tasks() if t.state == TaskState.COMPLETED))
        wf.refresh_waiting_tasks()
        wf.do_engine_steps()
    # identical names every iteration ...
    assert names[0] == names[1] == names[2] == ["timer_wait_30s"]
    # ... while the completed count is strictly monotonic.
    assert counts[0] < counts[1] < counts[2]
    assert eng.calls.count("svc_poll_stream_status") >= 3
