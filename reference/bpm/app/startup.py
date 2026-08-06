"""§11.1 — startup sequence."""
from __future__ import annotations

import logging

# G3 — handler modules MUST be imported before anything advances a workflow.
import app.bpm_tasks  # noqa: F401
from app.bpm import loader as bpm_loader
from app.bpm import timer as bpm_timer

log = logging.getLogger("bpm.startup")


def route_loggers() -> None:
    """G12 — uvicorn configures its own logger and leaves the root alone, so
    logging.getLogger("bpm.timer").info(...) vanishes silently."""
    bpm_log = logging.getLogger("bpm")
    bpm_log.setLevel(logging.INFO)
    if not bpm_log.handlers:
        for h in logging.getLogger("uvicorn").handlers:
            bpm_log.addHandler(h)


def bpm_startup(session_factory, *, start_timer: bool = True, on_startup=None) -> None:
    route_loggers()

    db = session_factory()
    try:
        bpm_loader.sync_definitions(db)
        # §5.4/§14.3: the reference implementation writes missing_handlers() and
        # never calls it. Here it is a HARD startup failure -- a missing handler
        # is otherwise discovered at runtime, mid-transition, in production.
        missing = bpm_loader.missing_handlers(db)
        if missing:
            raise RuntimeError(
                f"BPMN definitions reference unregistered service tasks: {missing}"
            )
        db.commit()
    finally:
        db.close()

    if start_timer:
        bpm_timer.start(session_factory)

    if on_startup is not None:
        try:
            on_startup()
        except Exception:
            log.exception("on_startup() failed")   # never abort startup
