"""Shape E handlers + the §5.7 retry protocol in anger.

`svc_poll_stream_status` is the handler G10 is about: it originally
short-circuited only on `state == 'ready'`, so a backfill run against
already-`published` rows fell through, called the external API, and regressed
`published` back to `ready`.
"""
from __future__ import annotations

from app.bpm.engine import ServiceTaskContext
from app.bpm.registry import service_task
from app.bpm.retry import clear_retry, retry_pending, terminal_failure
from app.models.domain import Video

# A fake "Cloudflare Stream". Tests script it; production would swap the impl.
FAKE_STREAM: dict[str, str] = {}
CALL_LOG: list[str] = []

# G10: EVERY terminal and post-loop state, not just the one you expect.
_NO_OP_STATES = ("ready", "published", "unlisted", "archived")


def _stream_status(uid: str) -> str:
    CALL_LOG.append(uid)
    return FAKE_STREAM.get(uid, "inprogress")


@service_task("svc_poll_stream_status")
def svc_poll_stream_status(ctx: ServiceTaskContext) -> dict:
    video = ctx.db.get(Video, ctx.get("video_id"))
    max_attempts = int(ctx.get("poll_max_attempts", 5))
    if video is None:
        return {
            "stream_ready": False,
            "stream_errored": True,
            **terminal_failure("poll", "no_such_video", max_attempts=max_attempts),
        }

    if video.state in _NO_OP_STATES:                       # G10
        return {
            "stream_ready": True,
            "stream_errored": False,
            **clear_retry("poll", max_attempts=max_attempts),
        }
    if video.state == "error":
        return {
            "stream_ready": False,
            "stream_errored": True,
            **terminal_failure("poll", "transcode_error", max_attempts=max_attempts),
        }

    status = _stream_status(video.stream_uid)
    if status == "ready":
        video.state = "ready"
        ctx.db.flush()
        return {
            "stream_ready": True,
            "stream_errored": False,
            **clear_retry("poll", max_attempts=max_attempts),
        }
    if status == "error":
        video.state = "error"
        ctx.db.flush()
        return {
            "stream_ready": False,
            "stream_errored": True,
            **terminal_failure("poll", "transcode_error", max_attempts=max_attempts),
        }

    attempt = int(ctx.get("poll_attempt", 0)) + 1
    return {
        "stream_ready": False,
        "stream_errored": False,
        **retry_pending("poll", attempt, max_attempts=max_attempts),
    }


@service_task("svc_flag_needs_intervention")
def svc_flag_needs_intervention(ctx: ServiceTaskContext) -> dict:
    video = ctx.db.get(Video, ctx.get("video_id"))
    if video is None:
        return {"flagged": False}
    video.needs_intervention = 1
    ctx.db.flush()
    return {"flagged": True}


@service_task("svc_publish_video")
def svc_publish_video(ctx: ServiceTaskContext) -> dict:
    video = ctx.db.get(Video, ctx.get("video_id"))
    if video is None:
        return {"published": False}
    if video.state == "published":                          # G26
        return {"published": True, "skipped": "already_published"}
    video.state = "published"
    ctx.db.flush()
    return {"published": True}
