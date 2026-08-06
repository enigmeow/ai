from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db as dbmod  # noqa: E402
from app.db import Base, make_engine, make_session_factory  # noqa: E402
import app.bpm_tasks  # noqa: E402,F401  (G3 — registration is an import side effect)
from app.bpm.loader import sync_definitions  # noqa: E402
from app.models.domain import (  # noqa: E402
    BlogPost, EmailMessage, InventoryItem, MessageThread, Order, User, Video,
)


def pytest_report_header(config):
    url, dialect = dbmod.resolve_url()
    return f"bpm test db: {dialect} ({url.split('@')[-1]})"


@pytest.fixture(scope="session")
def db_url():
    url, dialect = dbmod.resolve_url()
    return url, dialect


@pytest.fixture(scope="session")
def engine(db_url):
    url, _ = db_url
    eng = make_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def db(engine, session_factory):
    """A clean database per test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = session_factory()
    sync_definitions(s)
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def mysql_only(db_url):
    _, dialect = db_url
    if dialect != "mysql":
        pytest.skip(
            "§10.2: SQLite silently ignores VARCHAR(n) limits, so this test "
            "cannot prove anything there. Needs strict-mode MySQL."
        )


# ---------------------------------------------------------------- fixtures
def mkuser(db, username, roles="") -> User:
    u = User(id=str(uuid.uuid4()), username=username, roles_csv=roles)
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def admin(db):
    return mkuser(db, "admin_user", "admin")


@pytest.fixture()
def senior(db):
    return mkuser(db, "senior_user", "senior_instructor")


@pytest.fixture()
def author(db):
    return mkuser(db, "author_user", "instructor")


@pytest.fixture()
def post(db, author):
    p = BlogPost(id=str(uuid.uuid4()), title="A post", author_id=author.id, state="draft")
    db.add(p)
    db.flush()
    return p


@pytest.fixture()
def thread(db):
    t = MessageThread(id=str(uuid.uuid4()), subject="Hello", state="alive")
    db.add(t)
    db.flush()
    return t


@pytest.fixture()
def order_fixture(db, author):
    db.add(InventoryItem(sku="SKU-1", on_hand=10, reserved=0))
    o = Order(id="ord-" + uuid.uuid4().hex[:8], buyer_id=author.id)
    db.add(o)
    db.flush()
    return o


@pytest.fixture()
def video(db, author):
    v = Video(id=str(uuid.uuid4()), owner_user_id=author.id, stream_uid="uid-1",
              state="uploading")
    db.add(v)
    db.flush()
    return v
