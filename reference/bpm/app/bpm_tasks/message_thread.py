"""Shape B handlers + the ledger surface for unbounded per-actor transitions."""
from __future__ import annotations

from app.bpm.engine import ServiceTaskContext
from app.bpm.registry import service_task
from app.bpm.service import ledger_transition
from app.models.domain import MessageThread
from app.models.workflow import utcnow


@service_task("svc_delete_thread")
def svc_delete_thread(ctx: ServiceTaskContext) -> dict:
    thread = ctx.db.get(MessageThread, ctx.get("thread_id"))
    if thread is None:
        return {"deleted": False}
    if thread.state == "deleted":          # G26
        return {"deleted": True, "skipped": "already_deleted"}
    thread.state = "deleted"
    thread.deleted_at = utcnow()
    ctx.db.flush()
    return {"deleted": True}


# --- the ledger half of Shape B (§6.4.2) ----------------------------------
# Unbounded, repeatable, per-actor. NOT graph edges (G7).
def archive_for_member(db, thread_id: str, actor_user_id: str) -> bool:
    return ledger_transition(
        db,
        object_type="message_thread",
        object_id=thread_id,
        event="archive",
        from_state="alive",
        to_state="archived",
        actor_user_id=actor_user_id,
    )


def unarchive_for_member(db, thread_id: str, actor_user_id: str) -> bool:
    return ledger_transition(
        db,
        object_type="message_thread",
        object_id=thread_id,
        event="unarchive",
        from_state="archived",
        to_state="alive",
        actor_user_id=actor_user_id,
    )
