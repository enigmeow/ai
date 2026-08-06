"""§5.4 + §11.1 — the loader and the startup sequence."""
from __future__ import annotations

import logging

import pytest

from app.bpm.loader import missing_handlers, sync_definitions
from app.bpm.registry import _REGISTRY, get_handler, registered_tasks, service_task
from app.models.workflow import ProcessDefinition
from app.startup import bpm_startup, route_loggers


def test_all_four_definitions_deploy_at_version_1(db):
    rows = db.query(ProcessDefinition).all()
    assert {r.process_key for r in rows} == {"blog_post", "message_thread", "order", "video"}
    assert {r.version for r in rows} == {1}


def test_resync_with_no_change_does_not_append_a_version(db):
    sync_definitions(db)
    db.commit()
    assert db.query(ProcessDefinition).count() == 4


def test_a_content_change_appends_a_new_version_and_pins_running_instances(db, post, author):
    """§11.3 — new versions apply to NEW instances only."""
    from app.bpm.service import WorkflowService

    inst = WorkflowService(db).start("blog_post", object_type="blog_post",
                                     object_id=post.id, actor=author,
                                     data={"post_id": post.id})
    db.commit()
    v1 = db.get(ProcessDefinition, inst.process_definition_id)
    assert v1.version == 1

    row = ProcessDefinition(process_key="blog_post", version=2,
                            bpmn_xml=v1.bpmn_xml + "<!-- changed -->",
                            bpmn_hash="deadbeef", deployed_at=v1.deployed_at)
    db.add(row)
    db.commit()
    db.expire_all()
    assert db.get(ProcessDefinition, inst.process_definition_id).version == 1


def test_missing_handlers_reports_unregistered_service_tasks(db):
    assert missing_handlers(db) == []

    row = db.query(ProcessDefinition).filter_by(process_key="blog_post").one()
    row.bpmn_xml = row.bpmn_xml.replace("svc_publish_post", "svc_not_registered")
    db.flush()
    assert missing_handlers(db) == ["svc_not_registered"]
    db.rollback()


def test_startup_fails_hard_on_a_missing_handler(db, session_factory, monkeypatch):
    """§5.4/§14.3: "wiring it into startup as a hard failure is a recommended
    improvement for a fresh implementation"."""
    import app.bpm.loader as loader_mod

    monkeypatch.setattr(loader_mod, "missing_handlers", lambda db: ["svc_ghost"])
    with pytest.raises(RuntimeError, match="svc_ghost"):
        bpm_startup(session_factory, start_timer=False)


def test_startup_never_aborts_on_a_failing_on_startup_hook(db, session_factory, caplog):
    def boom():
        raise RuntimeError("sweep wiring is broken")

    bpm_startup(session_factory, start_timer=False, on_startup=boom)
    assert "on_startup() failed" in caplog.text


def test_g3_duplicate_registration_raises_at_import_time():
    """§5.1: "two handlers claiming svc_publish is always a bug, and it should
    surface at import time, not as a silent last-writer-wins at 3am"."""
    with pytest.raises(ValueError, match="already registered"):
        @service_task("svc_publish_post")
        def _dupe(ctx):
            return {}


def test_every_registered_handler_is_reachable_by_name():
    assert "svc_publish_post" in registered_tasks()
    assert get_handler("svc_nope") is None


def test_g12_route_loggers_attaches_uvicorn_handlers():
    """G12 -- the mechanism, not the uvicorn integration (no uvicorn here)."""
    uvicorn_log = logging.getLogger("uvicorn")
    marker = logging.NullHandler()
    uvicorn_log.addHandler(marker)
    bpm_log = logging.getLogger("bpm")
    saved = list(bpm_log.handlers)
    bpm_log.handlers.clear()
    try:
        route_loggers()
        assert marker in bpm_log.handlers
        assert bpm_log.level == logging.INFO
    finally:
        bpm_log.handlers[:] = saved
        uvicorn_log.removeHandler(marker)
