"""§5.7 — the transient-failure protocol, including the flaw §5.7 admits to."""
from __future__ import annotations

import pytest

from app.bpm.retry import clear_retry, flag_keys, retry_pending, terminal_failure


def test_g9_all_three_helpers_emit_the_same_flag_keys():
    """§5.7: "Every helper sets *all* of _ok / _failed / _retry_pending
    explicitly, even the irrelevant ones." This build extends that to the
    counters -- see the next test for why."""
    keys = flag_keys("poll")
    for out in (retry_pending("poll", 1), terminal_failure("poll", "x"), clear_retry("poll")):
        assert keys <= set(out), f"missing {keys - set(out)}"


def test_the_specs_own_terminal_failure_would_nameerror_on_a_reordered_gateway():
    """§5.7 admits: "terminal_failure sets neither <op>_attempt nor
    <op>_max_attempts, which the retry condition references. It survives only
    because Python's `and` short-circuits."

    Reproduced with the spec's OWN helper shape, and shown to be safe with this
    build's.

    (The `eval` calls below are deliberate: Spiff evaluates gateway conditions
    with `eval(expression, task_data)` (verified in
    `TaskDataEnvironment.evaluate`), so reproducing the hazard faithfully means
    reproducing that call. The expressions are literals in this file.)"""
    spec_terminal = {
        "poll_failed": True, "poll_ok": False, "poll_retry_pending": False,
        "poll_failure_reason": "x", "poll_failure_code": None,
    }
    ok_order = "poll_retry_pending and poll_attempt >= poll_max_attempts"
    bad_order = "poll_attempt >= poll_max_attempts and poll_retry_pending"

    assert eval(ok_order, dict(spec_terminal)) is False          # short-circuits
    with pytest.raises(NameError):
        eval(bad_order, dict(spec_terminal))                     # ... load-bearing

    ours = terminal_failure("poll", "x")
    assert eval(ok_order, dict(ours)) is False
    assert eval(bad_order, dict(ours)) is False                  # order-independent


def test_retry_pending_carries_the_attempt_counter_forward():
    a = retry_pending("poll", 1, max_attempts=3)
    assert (a["poll_attempt"], a["poll_max_attempts"]) == (1, 3)
    assert (a["poll_retry_pending"], a["poll_ok"], a["poll_failed"]) == (True, False, False)


def test_clear_retry_resets_the_counter():
    c = clear_retry("poll")
    assert c["poll_attempt"] == 0
    assert (c["poll_retry_pending"], c["poll_ok"], c["poll_failed"]) == (False, True, False)


def test_g10_the_loop_body_short_circuits_on_every_post_loop_state(db, video):
    """G10's actual incident: svc_poll_stream_status short-circuited only on
    'ready'; run against an already-'published' row during a backfill it fell
    through, called the external API, and regressed published -> ready."""
    from app.bpm.engine import ServiceTaskContext
    from app.bpm_tasks import video as vh

    vh.CALL_LOG.clear()
    vh.FAKE_STREAM.clear()

    for state in ("ready", "published", "unlisted", "archived"):
        video.state = state
        db.flush()
        ctx = ServiceTaskContext(db=db, data={"video_id": video.id}, task_name="svc_poll")
        out = vh.svc_poll_stream_status(ctx)
        assert out["stream_ready"] is True
        assert db.get(type(video), video.id).state == state, f"{state} was regressed"

    assert vh.CALL_LOG == [], "the external API must not be called from a terminal state"
