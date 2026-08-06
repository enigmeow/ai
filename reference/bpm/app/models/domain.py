"""Domain tables + the user/role model.

ALL OF THIS IS INVENTED.  §5 warns that the spec supplies no user/role model and
no HTTP layer; this is the minimum that lets §7.5's `_visible_to` predicate and
G24's `user_matches_role` helper be real.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.workflow import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    # comma-separated role names; a real system would use a join table.
    roles_csv: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def has_role(self, role: str) -> bool:
        return role in {r.strip() for r in self.roles_csv.split(",") if r.strip()}


class BlogPost(Base):
    """Shape A subject."""

    __tablename__ = "blog_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    author_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Law 4: denormalized cache, written ONLY by handlers.
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    published_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MessageThread(Base):
    """Shape B subject."""

    __tablename__ = "message_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="alive")
    deleted_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)


class Order(Base):
    """Shape C subject."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="placed")
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    fulfillment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unfulfilled")
    inventory_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Video(Base):
    """Shape E subject."""

    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    stream_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="uploading")
    needs_intervention: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EmailMessage(Base):
    """Law 7's 'best effort notification' sink, so tests can assert on side
    effects without a mail server."""

    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
