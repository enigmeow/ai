"""§5.6 — the sweep, tested for the three things it taught the reference impl."""
from __future__ import annotations

import time

import pytest

from app.bpm.loader import sync_definitions
from app.bpm.registry import _REGISTRY
from app.bpm.service import WorkflowService
from app.bpm.timer import MAX_CONSECUTIVE_TICK_FAILURES, _failure_counts, tick
from app.bpm_tasks import video as video_handlers
from app.models.domain import Order, Video
from app.models.workflow import ProcessDefinition, ProcessInstance


@pytest.fixture(autouse=True)
def _clear_counters():
    _failure_counts.clear()
    video_handlers.FAKE_STREAM.clear()
    video_handlers.CALL_LOG.clear()
    yield
    _failure_counts.clear()


def _deploy_with_fast_timer(db, process_key, old, new):
    """Deploy a variant of a definition with a shortened timer."""
    row = (
        db.query(ProcessDefinition)
        .filter(ProcessDefinition.process_key == process_key)
        .order_by(ProcessDefinition.version.desc())
        .first()
    )
    row.bpmn_xml = row.bpmn_xml.replace(old, new)
    db.flush()
    db.commit()


def test_the_sweep_fires_a_due_timer_and_persists(db, session_factory, order_fixture):
    _deploy_with_fast_timer(db, "order", '"PT30M"', '"PT1S"')
    svc = WorkflowService(db)
    inst = svc.start("order", object_type="order", object_id=order_fixture.id,
                     data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()
    inst_id = inst.id

    stats = tick(session_factory)
    assert stats["advanced"] == 0          # not due yet

    time.sleep(1.2)
    stats = tick(session_factory)
    assert stats == {"scanned": 1, "advanced": 1, "errors": 0, "given_up": 0}

    db.expire_all()
    assert db.get(ProcessInstance, inst_id).status == "completed"
    o = db.get(Order, order_fixture.id)
    assert o.state == "canceled" and o.canceled_reason == "abandoned_checkout"
    assert o.inventory_reserved == 0


def test_the_sweep_does_not_re_run_a_completed_workflows_handler(db, session_factory, order_fixture):
    """G23's real cost: with the terminate fix the instance leaves `running`, so
    the sweep stops selecting it and the handler runs exactly once."""
    _deploy_with_fast_timer(db, "order", '"PT30M"', '"PT1S"')
    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()
    time.sleep(1.2)
    tick(session_factory)
    for _ in range(3):
        assert tick(session_factory)["scanned"] == 0


def test_the_sweep_advances_a_poll_loop_across_ticks(db, session_factory, video):
    """§5.6 + G8: the loop returns to the SAME task-spec name every iteration,
    so only a monotonic completed-count sees the progress. Phase-2 gate from
    §13: an instance advances with no HTTP request involved."""
    _deploy_with_fast_timer(db, "video", '"PT30S"', '"PT1S"')
    WorkflowService(db).start("video", object_type="video", object_id=video.id,
                              data={"video_id": video.id, "poll_max_attempts": 10})
    db.commit()
    assert video_handlers.CALL_LOG == ["uid-1"]

    for _ in range(2):
        time.sleep(1.1)
        assert tick(session_factory)["advanced"] == 1
    assert len(video_handlers.CALL_LOG) == 3

    video_handlers.FAKE_STREAM["uid-1"] = "ready"
    time.sleep(1.1)
    tick(session_factory)
    db.expire_all()
    assert db.get(Video, video.id).state == "ready"
    inst = WorkflowService(db).get_instance(f"video:{video.id}")
    assert inst.current_states == ["user_owner_publish_decision"]


def test_a_poisoned_instance_does_not_stop_the_sweep_for_the_others(
    db, session_factory, order_fixture, video, monkeypatch
):
    """§5.6: "every instance is processed in its own try/except with a
    db.rollback() on failure. One poisoned workflow must not stop the sweep for
    everything else"."""
    _deploy_with_fast_timer(db, "order", '"PT30M"', '"PT1S"')
    _deploy_with_fast_timer(db, "video", '"PT30S"', '"PT1S"')
    svc = WorkflowService(db)
    svc.start("order", object_type="order", object_id=order_fixture.id,
              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    svc.start("video", object_type="video", object_id=video.id,
              data={"video_id": video.id, "poll_max_attempts": 10})
    db.commit()

    def boom(ctx):
        raise RuntimeError("poisoned")

    monkeypatch.setitem(_REGISTRY, "svc_timeout_release_and_cancel", boom)
    time.sleep(1.2)
    stats = tick(session_factory)
    assert stats["scanned"] == 2
    assert stats["errors"] == 1
    assert stats["advanced"] == 1          # the healthy one still moved


def test_the_sweep_gives_up_on_a_permanently_failing_instance(
    db, session_factory, order_fixture, monkeypatch
):
    """G14: "a permanently-failing instance retries forever ... give the sweep a
    give-up path." The spec never says WHAT the threshold should be -- invented."""
    _deploy_with_fast_timer(db, "order", '"PT30M"', '"PT1S"')
    WorkflowService(db).start("order", object_type="order", object_id=order_fixture.id,
                              data={"order_id": order_fixture.id, "sku": "SKU-1", "qty": 2})
    db.commit()

    def boom(ctx):
        raise RuntimeError("permanently broken")

    monkeypatch.setitem(_REGISTRY, "svc_timeout_release_and_cancel", boom)
    time.sleep(1.2)
    for _ in range(MAX_CONSECUTIVE_TICK_FAILURES):
        tick(session_factory)

    db.expire_all()
    inst = db.query(ProcessInstance).one()
    assert inst.status == "error"
    # and now the sweep no longer selects it at all
    assert tick(session_factory)["scanned"] == 0
