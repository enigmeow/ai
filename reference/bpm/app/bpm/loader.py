"""§5.4 — deploying .bpmn files."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable, Optional

from lxml import etree

from app.bpm.engine import BPMN_NS, extract_process_key
from app.bpm.registry import get_handler, registered_tasks
from app.models.workflow import ProcessDefinition, utcnow

log = logging.getLogger("bpm.loader")

_BPMN_DIR = Path(__file__).resolve().parent.parent / "bpmn"


def bpmn_dirs(extra: Optional[Path] = None) -> list[Path]:
    dirs = [_BPMN_DIR]
    if extra:
        dirs.append(Path(extra))
    return [d for d in dirs if d.exists()]


def all_bpmn(extra: Optional[Path] = None) -> list[Path]:
    out: list[Path] = []
    for d in bpmn_dirs(extra):
        out.extend(sorted(d.glob("*.bpmn")))
    return out


def sync_definitions(db, bpmn_dir: Optional[Path] = None) -> list[ProcessDefinition]:
    dirs = [Path(bpmn_dir)] if bpmn_dir else bpmn_dirs()

    keyed: dict[str, Path] = {}
    for d in dirs:                            # later dirs shadow earlier ones by key
        for path in sorted(d.glob("*.bpmn")):
            keyed[extract_process_key(path.read_text(encoding="utf-8"), path.stem)] = path

    rows: list[ProcessDefinition] = []
    for key, path in keyed.items():
        xml = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(xml.encode()).hexdigest()
        latest = (
            db.query(ProcessDefinition)
            .filter(ProcessDefinition.process_key == key)
            .order_by(ProcessDefinition.version.desc())
            .first()
        )
        if latest and latest.bpmn_hash == digest:
            rows.append(latest)
            continue
        new = ProcessDefinition(
            process_key=key,
            version=(latest.version + 1) if latest else 1,
            bpmn_xml=xml,
            bpmn_hash=digest,
            deployed_at=utcnow(),
        )
        db.add(new)
        db.flush()
        rows.append(new)
    return rows


_TASK_TAGS = (f"{{{BPMN_NS}}}scriptTask", f"{{{BPMN_NS}}}serviceTask")


def _dispatched_names(xml: str) -> set[str]:
    """Every id of a script/service task that dispatches to a handler."""
    root = etree.fromstring(xml.encode("utf-8"))
    names: set[str] = set()
    for tag in _TASK_TAGS:
        for el in root.iter(tag):
            tid = el.get("id")
            if tid and tid.startswith("svc_"):
                names.add(tid)
    return names


def missing_handlers(db) -> list[str]:
    """§5.4/§14.3 — written but never called at startup in the reference impl.
    Here it IS called, as a hard startup failure (see app/startup.py)."""
    missing: set[str] = set()
    keys = {
        r.process_key
        for r in db.query(ProcessDefinition.process_key).distinct().all()  # type: ignore[arg-type]
    }
    for key in keys:
        row = (
            db.query(ProcessDefinition)
            .filter(ProcessDefinition.process_key == key)
            .order_by(ProcessDefinition.version.desc())
            .first()
        )
        for name in _dispatched_names(row.bpmn_xml):
            if get_handler(name) is None:
                missing.add(name)
    return sorted(missing)
