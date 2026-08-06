"""§5.7 — the transient-failure return-value protocol.

§5.7 tells us its own helpers are *not* exhaustive: `terminal_failure` sets
neither `<op>_attempt` nor `<op>_max_attempts`, and survives only because
Python's `and` short-circuits.  §5.7 then says "either have terminal_failure set
the counters too, or treat the short-circuit as load-bearing and comment it".
This build takes the first branch — the counters are always set — so no gateway
ordering is load-bearing.  A test asserts the three helpers emit identical key
sets.
"""
from __future__ import annotations

from typing import Any, Optional

# G9: every helper sets ALL of _ok / _failed / _retry_pending on every path,
# plus (this build's deviation from §5.7) _attempt and _max_attempts.
_KEYS = ("_attempt", "_max_attempts", "_retry_pending", "_ok", "_failed")


def retry_pending(op: str, attempt: int, max_attempts: int = 10) -> dict[str, Any]:
    return {
        f"{op}_attempt": attempt,
        f"{op}_max_attempts": max_attempts,
        f"{op}_retry_pending": True,
        f"{op}_ok": False,
        f"{op}_failed": False,
    }


def terminal_failure(
    op: str,
    reason: str,
    code: Optional[str] = None,
    *,
    attempt: int = 0,
    max_attempts: int = 10,
) -> dict[str, Any]:
    return {
        f"{op}_attempt": attempt,
        f"{op}_max_attempts": max_attempts,
        f"{op}_retry_pending": False,
        f"{op}_ok": False,
        f"{op}_failed": True,
        f"{op}_failure_reason": reason,
        f"{op}_failure_code": code,
    }


def clear_retry(op: str, *, max_attempts: int = 10) -> dict[str, Any]:
    return {
        f"{op}_attempt": 0,
        f"{op}_max_attempts": max_attempts,
        f"{op}_retry_pending": False,
        f"{op}_ok": True,
        f"{op}_failed": False,
    }


def flag_keys(op: str) -> set[str]:
    return {f"{op}{suffix}" for suffix in _KEYS}
