"""Database plumbing.

The spec (§10.2) is emphatic that handler contract tests must run against real,
strict-mode MySQL, because SQLite silently ignores VARCHAR(n) limits.  So the
URL is switchable: BPM_DB_URL wins, else MySQL on 13306 if reachable, else
SQLite (with a loud marker so tests can skip the width-sensitive ones).
"""
from __future__ import annotations

import os
import socket

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


MYSQL_URL = os.environ.get(
    "BPM_MYSQL_URL", "mysql+pymysql://root:bpmspec@127.0.0.1:13306/bpmspec"
)


def _mysql_reachable(host: str = "127.0.0.1", port: int = 13306) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def resolve_url() -> tuple[str, str]:
    """Return (url, dialect) where dialect is 'mysql' or 'sqlite'."""
    explicit = os.environ.get("BPM_DB_URL")
    if explicit:
        return explicit, ("mysql" if explicit.startswith("mysql") else "sqlite")
    if _mysql_reachable():
        return MYSQL_URL, "mysql"
    return "sqlite+pysqlite:///:memory:", "sqlite"


def make_engine(url: str | None = None):
    if url is None:
        url, _ = resolve_url()
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        # keep an in-memory DB alive across sessions in one process
        from sqlalchemy.pool import StaticPool

        kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
