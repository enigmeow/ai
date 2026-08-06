"""Static, mechanical rules over EVERY definition (§12 G23's own suggestion,
extended to the other mechanical rules in §6)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.bpm.engine import BPMN_NS
from app.bpm.loader import all_bpmn
from app.bpm.registry import get_handler

PATHS = all_bpmn()
IDS = [p.stem for p in PATHS]


def _root(path):
    return ET.fromstring(path.read_text())


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_g23_every_end_event_terminates_when_an_event_gateway_exists(path):
    root = _root(path)
    if root.find(f".//{{{BPMN_NS}}}eventBasedGateway") is None:
        pytest.skip("no eventBasedGateway in this definition")
    for end in root.iter(f"{{{BPMN_NS}}}endEvent"):
        assert end.find(f"{{{BPMN_NS}}}terminateEventDefinition") is not None, (
            f"{path.name}: end event {end.get('id')!r} has no terminateEventDefinition"
        )


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_g22_every_exclusive_gateway_has_a_default(path):
    root = _root(path)
    for gw in root.iter(f"{{{BPMN_NS}}}exclusiveGateway"):
        outs = gw.findall(f"{{{BPMN_NS}}}outgoing")
        if len(outs) < 2:
            continue
        assert gw.get("default"), f"{path.name}: gateway {gw.get('id')!r} has no default="


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_g11_timer_bodies_are_quoted_python_expressions(path):
    root = _root(path)
    for td in root.iter(f"{{{BPMN_NS}}}timeDuration"):
        body = (td.text or "").strip()
        assert body.startswith(('"', "'")) and body.endswith(('"', "'")), (
            f"{path.name}: timeDuration {body!r} is a NameError, not an ISO-8601 string"
        )


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_process_key_matches_the_file_name(path):
    root = _root(path)
    proc = root.find(f"{{{BPMN_NS}}}process")
    assert proc.get("id") == path.stem


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_every_element_has_an_explicit_meaningful_id(path):
    """§6.2 — modeler-generated `Activity_0x7f2k` ids end up in the audit table
    forever."""
    root = _root(path)
    bad = []
    for el in root.iter():
        if not el.tag.startswith(f"{{{BPMN_NS}}}"):
            continue
        tag = el.tag.split("}", 1)[1]
        if tag in ("definitions", "process", "incoming", "outgoing", "script",
                   "conditionExpression", "signalEventDefinition",
                   "timerEventDefinition", "terminateEventDefinition", "timeDuration"):
            continue
        eid = el.get("id")
        if not eid or eid.split("_")[0] in ("Activity", "Gateway", "Event", "Flow"):
            bad.append((tag, eid))
    assert bad == []


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_every_dispatched_service_task_has_a_registered_handler(path):
    """§5.4's `missing_handlers`, as a test rather than a runtime surprise."""
    import app.bpm_tasks  # noqa: F401  (G3)

    root = _root(path)
    for tag in ("scriptTask", "serviceTask"):
        for el in root.iter(f"{{{BPMN_NS}}}{tag}"):
            tid = el.get("id")
            if tid and tid.startswith("svc_"):
                assert get_handler(tid) is not None, f"{path.name}: no handler for {tid}"


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_naming_conventions_hold(path):
    """§6.2 — the prefixes are load-bearing for the frontend labeller and the
    loader's handler check."""
    root = _root(path)
    checks = {
        "scriptTask": "svc_",
        "userTask": "user_",
        "exclusiveGateway": "gw_",
        "sequenceFlow": "sf_",
        "endEvent": "end_",
        "signal": "sig_",
    }
    for tag, prefix in checks.items():
        for el in root.iter(f"{{{BPMN_NS}}}{tag}"):
            eid = el.get("id") or ""
            assert eid.startswith(prefix), f"{path.name}: {tag} {eid!r} should start with {prefix!r}"
