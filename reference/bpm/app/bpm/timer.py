"""§5.6 — the 60-second tick."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from app.bpm.service import ERROR, RUNNING, WorkflowService, completed_count
from app.models.workflow import ProcessInstance, utcnow

log = logging.getLogger("bpm.timer")

# §5.6 / G14: a permanently-failing instance retries forever. Give the sweep a
# give-up path. The spec does not say what the give-up threshold should be or
# where it is stored -- INVENTED (see FINDINGS Part 2).
MAX_CONSECUTIVE_TICK_FAILURES = 5
_failure_counts: dict[int, int] = {}


def tick(session_factory: Callable[[], object], *, now=None) -> dict:
    """One sweep. Returns a small stats dict so the caller can alert on the
    ERROR RATE (§5.6, G14), not just on crashes."""
    stats = {"scanned": 0, "advanced": 0, "errors": 0, "given_up": 0}
    db = session_factory()
    try:
        instances = (
            db.query(ProcessInstance).filter(ProcessInstance.status == RUNNING).all()
        )
        ids = [i.id for i in instances]
    finally:
        db.close()

    for instance_id in ids:
        db = session_factory()
        try:
            stats["scanned"] += 1
            instance = db.get(ProcessInstance, instance_id)
            if instance is None or instance.status != RUNNING:
                continue
            if _failure_counts.get(instance_id, 0) >= MAX_CONSECUTIVE_TICK_FAILURES:
                stats["given_up"] += 1
                continue

            svc = WorkflowService(db)
            workflow = svc._load_workflow(instance, None)

            before = completed_count(workflow)
            workflow.refresh_waiting_tasks()
            workflow.do_engine_steps()
            after = completed_count(workflow)

            if after > before or workflow.is_completed():
                svc._persist(workflow, instance)
                svc._audit_end_if_complete(workflow, instance, None)
                db.commit()
                stats["advanced"] += 1
            else:
                db.rollback()
            _failure_counts.pop(instance_id, None)
        except Exception:
            # §5.6: every instance in its own try/except with a rollback --
            # without it a DataError poisons the session and every subsequent
            # instance in the same tick fails too.
            db.rollback()
            stats["errors"] += 1
            n = _failure_counts.get(instance_id, 0) + 1
            _failure_counts[instance_id] = n
            log.exception("timer tick failed for instance %s (failure %s)", instance_id, n)
            if n >= MAX_CONSECUTIVE_TICK_FAILURES:
                try:
                    inst = db.get(ProcessInstance, instance_id)
                    if inst is not None:
                        inst.status = ERROR
                        inst.updated_at = utcnow()
                        db.commit()
                        log.error(
                            "timer giving up on instance %s after %s failures; "
                            "flipped to status=error",
                            instance_id, n,
                        )
                except Exception:
                    db.rollback()
        finally:
            db.close()
    return stats


_scheduler = None


def start(session_factory, interval_seconds: int = 60):
    """G13: with `--workers N` uvicorn runs the startup event in every worker,
    so this starts N times and every tick fires N times. Wasteful, not
    incorrect; the fix is a SELECT ... FOR UPDATE SKIP LOCKED claim per
    instance."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        lambda: tick(session_factory), "interval", seconds=interval_seconds, id="bpm_tick"
    )
    _scheduler.start()
    return _scheduler


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
