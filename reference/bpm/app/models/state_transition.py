"""§4 table 4 — the global audit log."""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.workflow import utcnow


class StateTransition(Base):
    __tablename__ = "state_transitions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    process_instance_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("process_instances.id"),
        nullable=False,
    )
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)  # G16
    task_spec_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    to_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # G18 — `metadata` is reserved on the declarative class.
    transition_metadata: Mapped[Optional[Any]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_st_object", "object_type", "object_id", "created_at"),
        Index("idx_st_instance", "process_instance_id", "created_at"),
        Index("idx_st_actor", "actor_user_id", "created_at"),
    )
