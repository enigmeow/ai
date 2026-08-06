"""Shape A handlers.

Law 2: every side effect of the transition lives here, so it also happens when
the transition comes from a timer, a webhook, an admin, or an agent.
Law 3 / G1: flush, never commit.
"""
from __future__ import annotations

from app.bpm.engine import ServiceTaskContext
from app.bpm.registry import service_task
from app.models.domain import BlogPost, EmailMessage
from app.models.workflow import utcnow

# G10/G26 generalised: states in which a (re-)run must do nothing.
_TERMINAL = ("published", "rejected")


def _load(ctx: ServiceTaskContext) -> BlogPost | None:
    post_id = ctx.get("post_id")
    if post_id is None:
        return None
    return ctx.db.get(BlogPost, post_id)


@service_task("svc_mark_pending_review")
def svc_mark_pending_review(ctx: ServiceTaskContext) -> dict:
    post = _load(ctx)
    if post is None:
        return {"post_missing": True}
    if post.state in _TERMINAL:
        return {"post_missing": False, "skipped": post.state}
    post.state = "pending_review"
    ctx.db.flush()
    return {"post_missing": False, "post_state": post.state}


@service_task("svc_publish_post")
def svc_publish_post(ctx: ServiceTaskContext) -> dict:
    post = _load(ctx)
    if post is None:
        return {"published": False}
    # G26: re-derive safety from the object, not from the trigger.
    if post.state == "published":
        return {"published": True, "skipped": "already_published"}
    post.state = "published"
    post.published_at = utcnow()
    # Law 2: the CDN purge + the email both live here, not in the router.
    ctx.db.add(
        EmailMessage(
            to_user_id=post.author_id,
            template="post_published",
            payload={"post_id": post.id, "title": post.title},
        )
    )
    ctx.db.flush()
    return {"published": True, "post_state": post.state}


@service_task("svc_reject_post")
def svc_reject_post(ctx: ServiceTaskContext) -> dict:
    post = _load(ctx)
    if post is None:
        return {"published": False}
    if post.state in _TERMINAL:
        return {"published": False, "skipped": post.state}
    post.state = "rejected"
    post.reject_reason = ctx.get("reject_reason") or "no reason given"
    ctx.db.add(
        EmailMessage(
            to_user_id=post.author_id,
            template="post_rejected",
            payload={"post_id": post.id, "reason": post.reject_reason},
        )
    )
    ctx.db.flush()
    return {"published": False, "post_state": post.state}
