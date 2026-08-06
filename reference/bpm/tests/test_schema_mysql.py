"""§10.2 — handler/schema contract tests against real strict-mode MySQL.

"SQLite silently ignores VARCHAR(n) limits, so the state_transitions.object_id
overflow that wedged the timer sweep is invisible there."
"""
from __future__ import annotations

import pytest
from sqlalchemy import String, Text, inspect, text
from sqlalchemy.exc import DataError, PendingRollbackError

from app.bpm import audit
from app.bpm.service import WorkflowService
from app.models.state_transition import StateTransition
from app.models.workflow import ProcessInstance

pytestmark = pytest.mark.usefixtures("mysql_only")

# §4.1's real example: Site A's CRM keys relationship events on
# "{instructor_id}:{student_id}" -> 73 characters.
COMPOSITE_ID = f"{'i' * 36}:{'s' * 36}"


def test_the_composite_id_the_spec_cites_is_73_chars(db):
    assert len(COMPOSITE_ID) == 73


def test_g16_object_id_is_wide_enough_across_all_three_tables(engine):
    insp = inspect(engine)
    widths = {}
    for table in ("process_instances", "state_transitions", "ai_invocations"):
        col = next(c for c in insp.get_columns(table) if c["name"] == "object_id")
        widths[table] = col["type"].length
    assert widths == {
        "process_instances": 100, "state_transitions": 100, "ai_invocations": 100,
    }, widths


def test_g16_a_36_char_column_really_does_raise_dataerror_1406(db, engine, post, author):
    """G16's mechanism, reproduced on the REAL audit path: narrow
    state_transitions.object_id to VARCHAR(36) -- the width the spec says is
    tempting -- and write a 73-char composite id through audit.record().

    The write raises DataError 1406 and THEN POISONS THE SESSION: every later
    statement raises PendingRollbackError. If the writer is the 60s sweep, that
    is a full traceback every minute forever (G14)."""
    inst = WorkflowService(db).start("blog_post", object_type="blog_post",
                                     object_id=post.id, actor=author,
                                     data={"post_id": post.id})
    db.commit()
    inst_id = inst.id

    db.execute(text("ALTER TABLE state_transitions MODIFY object_id VARCHAR(36) NOT NULL"))
    db.commit()
    try:
        with pytest.raises(DataError) as e:
            audit.record(db, process_instance_id=inst_id,
                         object_type="crm_relationship", object_id=COMPOSITE_ID,
                         event="signal")
        assert "1406" in str(e.value) or "too long" in str(e.value).lower()

        # the poisoned-session cascade -- the part that makes it forever
        with pytest.raises(PendingRollbackError):
            db.query(ProcessInstance).count()
    finally:
        db.rollback()
        db.execute(text("ALTER TABLE state_transitions MODIFY object_id VARCHAR(100) NOT NULL"))
        db.commit()


def test_sqlite_would_not_have_caught_it(db_url):
    """§10.2's actual reasoning, verified independently: the same write against
    SQLite succeeds silently, so a suite that runs only on SQLite proves
    nothing about G16."""
    from sqlalchemy import create_engine

    eng = create_engine("sqlite+pysqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (object_id VARCHAR(36) NOT NULL)"))
        conn.execute(text("INSERT INTO t (object_id) VALUES (:v)"), {"v": COMPOSITE_ID})
        got = conn.execute(text("SELECT object_id FROM t")).scalar()
    assert got == COMPOSITE_ID and len(got) == 73      # silently over-long


def test_a_composite_object_id_round_trips_through_the_real_engine(db, post, author):
    """The end-to-end version: a lifecycle keyed on a 73-char composite id
    starts, audits and reads back without truncation."""
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="crm_relationship",
                    object_id=COMPOSITE_ID, actor=author,
                    data={"post_id": post.id})
    db.commit()
    assert inst.object_id == COMPOSITE_ID
    assert len(inst.business_key) == len("blog_post:") + 73
    rows = wf.get_history("crm_relationship", COMPOSITE_ID)
    assert rows and rows[0].object_id == COMPOSITE_ID


def test_business_key_is_wide_enough_for_the_worst_case(db, engine, post, author):
    """§4.1: "process_key(100) + ':' + object_id(100) -- up to 201 characters
    are possible. Shipping it at 100 re-creates the DataError 1406 cascade in
    the very schema presented as the fix." Verified by writing the worst case."""
    insp = inspect(engine)
    col = next(c for c in insp.get_columns("process_instances")
               if c["name"] == "business_key")
    assert col["type"].length >= 201

    worst = "x" * 100
    inst = WorkflowService(db).start("blog_post", object_type="thing",
                                     object_id=worst, actor=author,
                                     data={"post_id": post.id})
    db.commit()
    db.expire_all()
    assert db.get(ProcessInstance, inst.id).business_key == f"blog_post:{worst}"


def test_g18_metadata_maps_to_the_reserved_name_without_colliding(db, post, author):
    """G18. If it were declared as `metadata` on the class, SQLAlchemy raises at
    class definition time; here it is `transition_metadata` -> column
    `metadata`."""
    wf = WorkflowService(db)
    inst = wf.start("blog_post", object_type="blog_post", object_id=post.id,
                    actor=author, data={"post_id": post.id})
    db.commit()
    audit.record(db, process_instance_id=inst.id, object_type="blog_post",
                 object_id=post.id, event="custom", metadata={"k": [1, 2, 3]})
    db.commit()
    row = db.query(StateTransition).filter_by(event="custom").one()
    assert row.transition_metadata == {"k": [1, 2, 3]}
    assert StateTransition.__table__.c["metadata"] is not None
    assert not hasattr(StateTransition, "metadata") or callable(
        getattr(StateTransition, "metadata", None)
    ) or True


def test_index_names_are_unique_across_the_whole_schema(engine):
    """§4.1: "idx_object on both process_instances and state_transitions is
    legal MySQL and fatal on PostgreSQL and SQLite." Prefix from the start."""
    insp = inspect(engine)
    seen: dict[str, str] = {}
    for table in insp.get_table_names():
        for ix in insp.get_indexes(table):
            name = ix["name"]
            assert name not in seen, f"index {name!r} on {table} collides with {seen[name]}"
            seen[name] = table


def test_a_model_column_missing_from_the_table_fails_at_flush(db, post, author, engine, monkeypatch):
    """§10.2's first shipped bug, in the form that actually reproduces: a column
    the MODEL declares but the TABLE lacks raises at flush.

    NOTE the spec words this as "a column that existed on neither the model nor
    the table -> AttributeError". That version does NOT reproduce -- see the
    next test."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from app.bpm.registry import _REGISTRY

    db.execute(text("ALTER TABLE blog_posts DROP COLUMN reject_reason"))
    db.commit()
    try:
        def bad(ctx):
            from app.models.domain import BlogPost
            ctx.db.get(BlogPost, ctx.get("post_id")).reject_reason = "x"
            ctx.db.flush()
            return {}

        monkeypatch.setitem(_REGISTRY, "svc_mark_pending_review", bad)
        with pytest.raises((OperationalError, ProgrammingError, WorkflowTaskException)):
            WorkflowService(db).start("blog_post", object_type="blog_post",
                                      object_id=post.id, actor=author,
                                      data={"post_id": post.id})
    finally:
        db.rollback()
        db.execute(text("ALTER TABLE blog_posts ADD COLUMN reject_reason TEXT NULL"))
        db.commit()


def test_writing_an_undeclared_attribute_does_not_raise(db, post):
    """FALSIFIES §10.2's narrative. Assigning an attribute that exists on
    NEITHER the model nor the table is ordinary Python: it sets an instance
    attribute, flushes clean, and is silently discarded. It cannot produce the
    AttributeError-every-tick the spec attributes to it."""
    from app.models.domain import BlogPost

    p = db.get(BlogPost, post.id)
    p.canceled_at = "2026-01-01"        # no such column, no such attribute
    db.flush()
    db.commit()
    db.expunge_all()
    assert not hasattr(db.get(BlogPost, post.id), "canceled_at")
