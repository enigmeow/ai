"""Engine tables — spec §4.

Deviations from the DDL in §4, all deliberate and all flagged in FINDINGS.md:

* index names are prefixed per table (`idx_pi_object`, `idx_st_object`, ...)
  because §4.1 says unprefixed names are fatal on SQLite/PostgreSQL.
* `workflow_tasks.spiff_task_id` is an invented column — §5 says the reference
  implementation has no way to identify *which* Spiff task a row projects and
  tells the implementer to supply one.
* `state_transitions.metadata` is mapped as `transition_metadata` (G18).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# MEDIUMTEXT/LONGTEXT on MySQL, TEXT elsewhere.
from sqlalchemy.dialects import mysql  # noqa: E402

MEDIUMTEXT = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
LONGTEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")


class ProcessDefinition(Base):
    __tablename__ = "process_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    process_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    bpmn_xml: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    bpmn_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deployed_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("process_key", "version", name="uk_pd_key_version"),
        Index("idx_pd_key", "process_key"),
    )


class ProcessInstance(Base):
    __tablename__ = "process_instances"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    process_definition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("process_definitions.id"), nullable=False
    )
    # §4.1: process_key(100) + ':' + object_id(100) => up to 201 chars. 255.
    business_key: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)  # G16
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    serialized_state: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    current_states: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)

    definition: Mapped[ProcessDefinition] = relationship()

    __table_args__ = (
        Index("idx_pi_business_key", "business_key"),
        Index("idx_pi_object", "object_type", "object_id"),
        Index("idx_pi_status", "status", "updated_at"),
    )


class WorkflowTask(Base):
    __tablename__ = "workflow_tasks"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    process_instance_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("process_instances.id"),
        nullable=False,
    )
    task_spec_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # INVENTED (see module docstring / §5): the Spiff task uuid this row projects.
    spiff_task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    task_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    assignee_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # G24: a COMMA-SEPARATED LIST of candidate groups, never just the first.
    assignee_role: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    due_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)
    completed_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    form_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_wt_instance", "process_instance_id", "status"),
        Index("idx_wt_user_inbox", "assignee_user_id", "status"),
        Index("idx_wt_role_inbox", "assignee_role", "status"),
    )


class AIInvocation(Base):
    """§9.4.  Modeled and — unlike the reference implementation — actually written
    to by the AI service task in this build."""

    __tablename__ = "ai_invocations"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    process_instance_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("process_instances.id"), nullable=True
    )
    state_transition_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)  # G16
    prompt_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reviewed_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)
    human_override_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_ai_object", "object_type", "object_id"),)


__all__ = [
    "ProcessDefinition",
    "ProcessInstance",
    "WorkflowTask",
    "AIInvocation",
    "utcnow",
]
