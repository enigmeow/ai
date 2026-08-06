"""§5.5 — one writer, one place."""
from __future__ import annotations

from typing import Any, Optional

from app.models.state_transition import StateTransition
from app.models.workflow import utcnow

# §5.5 canonical events
STARTED = "started"
TASK_STARTED = "task_started"
TASK_COMPLETED = "task_completed"
SIGNAL = "signal"
ERROR = "error"
CANCELED = "canceled"
ENDED = "ended"


def record(
    db,
    *,
    process_instance_id: int,
    object_type: str,
    object_id: str,
    event: str,
    task_spec_name: Optional[str] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[Any] = None,
) -> StateTransition:
    row = StateTransition(
        process_instance_id=process_instance_id,
        object_type=object_type,
        object_id=object_id,
        event=event,
        task_spec_name=task_spec_name,
        from_state=from_state,
        to_state=to_state,
        actor_user_id=actor_user_id,
        reason=reason,
        transition_metadata=metadata,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row
