"""§7.5 + §7.6 — the task inbox and admin ops.

INVENTED SUBSTRATE. §5 says plainly that the HTTP/auth layer is not in the spec
and must be supplied. This module is the framework-independent core of it:
plain functions with an explicit `user`, raising PermissionError / LookupError /
ValueError, which a FastAPI layer maps to 403 / 404 / 409.
"""
from __future__ import annotations

from typing import Optional

from app.bpm.service import WorkflowService, task_visible_to
from app.models.workflow import ProcessInstance, WorkflowTask


class Forbidden(PermissionError):
    pass


class Conflict(ValueError):
    pass


# ------------------------------------------------------------------ §7.5
def inbox(db, user) -> list[WorkflowTask]:
    return WorkflowService(db).get_inbox(user)


def get_task(db, task_id: int, user) -> WorkflowTask:
    task = db.get(WorkflowTask, task_id)
    if task is None:
        raise LookupError(task_id)
    if not task_visible_to(task, user):
        raise Forbidden("task not visible")
    return task


def claim_task(db, task_id: int, user) -> WorkflowTask:
    """§7.5 claim rules: must be `ready` (else 409), role must match (403), and
    a task already assigned to a DIFFERENT user is refused (403)."""
    task = db.get(WorkflowTask, task_id)
    if task is None:
        raise LookupError(task_id)
    if task.status != "ready":
        raise Conflict(f"task is {task.status}")
    if task.assignee_user_id and task.assignee_user_id != user.id:
        raise Forbidden("already claimed by another user")
    if not task_visible_to(task, user):
        raise Forbidden("role does not match")
    task.assignee_user_id = user.id
    db.flush()
    return task


def complete_task(db, task_id: int, user, form_data: Optional[dict] = None):
    return WorkflowService(db).complete_user_task(task_id, user, form_data or {})


# ------------------------------------------------------------------ §7.6
#
# The spec's own warning: "As shipped in the reference implementation [by-key]
# requires only *authentication*: any logged-in user who can guess a business
# key reads that object's full transition history ... The fix is a
# per-object-type ownership check before returning history."
#
# So this build carries one. Registering an owner predicate per object_type is
# mandatory: an unregistered type is DENIED, not allowed, so adding a new
# lifecycle cannot silently open a hole.
_OWNER_PREDICATES: dict[str, callable] = {}


def register_owner_predicate(object_type: str, fn) -> None:
    _OWNER_PREDICATES[object_type] = fn


def _may_read(db, instance: ProcessInstance, user) -> bool:
    if user is None:
        return False
    if user.has_role("admin"):
        return True
    fn = _OWNER_PREDICATES.get(instance.object_type)
    if fn is None:
        return False            # default DENY
    return bool(fn(db, instance.object_id, user))


def instance_by_key(db, business_key: str, user) -> dict:
    svc = WorkflowService(db)
    instance = svc.get_instance(business_key)
    if instance is None:
        raise LookupError(business_key)
    if not _may_read(db, instance, user):
        raise Forbidden("not authorized for this object")
    return {
        "instance": instance,
        "history": svc.get_history(instance.object_type, instance.object_id),
    }


def actions_by_key(db, business_key: str, user) -> list[dict]:
    svc = WorkflowService(db)
    instance = svc.get_instance(business_key)
    if instance is None:
        raise LookupError(business_key)
    if not _may_read(db, instance, user):
        raise Forbidden("not authorized for this object")
    return svc.available_actions(business_key, user)


def list_instances(db, user, *, status=None, process_key=None, limit=100):
    if not user.has_role("admin"):
        raise Forbidden("admin only")
    return WorkflowService(db).list_instances(status=status, process_key=process_key, limit=limit)


def cancel_instance(db, business_key: str, user, reason: str):
    if not user.has_role("admin"):
        raise Forbidden("admin only")
    return WorkflowService(db).cancel(business_key, user, reason)


def retry_instance(db, instance_id: int, user):
    if not user.has_role("admin"):
        raise Forbidden("admin only")
    return WorkflowService(db).retry_failed_task(instance_id, user)


# ------------------------------------------------------------------ §7.4
_LABELS = {"svc_publish_post": "Publish post", "state_alive": "Alive"}
_PREFIXES = ("svc_", "user_", "state_", "end_", "timer_", "gw_", "sf_", "sig_")


def label_for(name: str) -> str:
    if name in _LABELS:
        return _LABELS[name]
    for p in _PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    return name.replace("_", " ")


def state_badge_class(state: str) -> str:
    return f"wf-state wf-state-{state}"
