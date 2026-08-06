"""§5.2 — Spiff setup, the `_dispatch` trick, camunda grafting, serialization."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Optional

from lxml import etree
from SpiffWorkflow.bpmn.parser.BpmnParser import BpmnParser
from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine
from SpiffWorkflow.bpmn.serializer.workflow import BpmnWorkflowSerializer
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow

from app.bpm.registry import get_handler

log = logging.getLogger("bpm.engine")

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
CAMUNDA_NS = "http://camunda.org/schema/1.0/bpmn"


# --------------------------------------------------------------------------
# handler context
# --------------------------------------------------------------------------
@dataclass
class ServiceTaskContext:
    db: Any                       # Session — flush, never commit (G1)
    data: dict[str, Any]          # three-way merged workflow variables (G4)
    actor: Any = None
    instance: Any = None
    task_name: str = ""
    task: Any = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


CtxFactory = Callable[[Any, str], ServiceTaskContext]


def merged_task_data(workflow, task) -> dict[str, Any]:
    """§5.2's three-way merge, in precedence order:
    workflow.data -> workflow.data_objects -> task.data."""
    merged: dict[str, Any] = {}
    if workflow is not None:
        merged.update(getattr(workflow, "data", None) or {})
        try:
            merged.update(workflow.data_objects or {})
        except Exception:  # pragma: no cover - defensive
            pass
    if task is not None:
        merged.update(getattr(task, "data", None) or {})
    return merged


# --------------------------------------------------------------------------
# script engine
# --------------------------------------------------------------------------
class RegistryScriptEngine(PythonScriptEngine):
    def __init__(self, ctx_factory: CtxFactory) -> None:
        super().__init__()
        self._ctx_factory = ctx_factory

    def execute(self, task, script, external_context=None):
        def _dispatch(task_name: str):
            handler = get_handler(task_name)
            if handler is None:
                raise RuntimeError(
                    f"no Python handler registered for service task {task_name!r}"
                )
            ctx = self._ctx_factory(task, task_name)
            result = handler(ctx)
            if isinstance(result, dict):
                task.data.update(result)
            return result

        ext = dict(external_context or {})
        ext["_dispatch"] = _dispatch
        return super().execute(task, script, ext)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def _iter_user_task_extensions(payload: bytes):
    """Yield (task_id, {camunda attr -> value}) for every userTask in the XML."""
    root = etree.fromstring(payload)
    for el in root.iter(f"{{{BPMN_NS}}}userTask"):
        tid = el.get("id")
        attrs = {}
        for k, v in el.attrib.items():
            if k.startswith(f"{{{CAMUNDA_NS}}}"):
                attrs[k.split("}", 1)[1]] = v
        if tid:
            yield tid, attrs


def _inject_candidate_groups(payload: bytes, spec, subprocess_specs) -> None:
    """G5 — Spiff does not parse the camunda: namespace; extensions arrives {}.
    Walk the XML once and graft the values onto each user-task spec."""
    wanted = dict(_iter_user_task_extensions(payload))
    if not wanted:
        return
    all_specs = [spec] + list((subprocess_specs or {}).values())
    for s in all_specs:
        if s is None:
            continue
        for name, task_spec in getattr(s, "task_specs", {}).items():
            attrs = wanted.get(getattr(task_spec, "bpmn_id", None) or name)
            if not attrs:
                continue
            ext = getattr(task_spec, "extensions", None)
            if ext is None:
                task_spec.extensions = dict(attrs)
            else:
                ext.update(attrs)


def parse_spec(bpmn_xml, process_key: str):
    parser = BpmnParser()
    # G2: lxml rejects a unicode str carrying an XML encoding declaration.
    payload = bpmn_xml.encode("utf-8") if isinstance(bpmn_xml, str) else bpmn_xml
    parser.add_bpmn_str(payload)
    spec = parser.get_spec(process_key)
    subprocess_specs = parser.find_all_specs()
    _inject_candidate_groups(payload, spec, subprocess_specs)
    return spec, subprocess_specs


def extract_process_key(xml_text: str, fallback: str) -> str:
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except Exception:
        return fallback
    proc = root.find(f"{{{BPMN_NS}}}process")
    if proc is not None and proc.get("id"):
        return proc.get("id")
    return fallback


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _serializer() -> BpmnWorkflowSerializer:
    return BpmnWorkflowSerializer(registry=BpmnWorkflowSerializer.configure())


def build_workflow(bpmn_xml: str, process_key: str, ctx_factory: CtxFactory) -> BpmnWorkflow:
    spec, subs = parse_spec(bpmn_xml, process_key)
    return BpmnWorkflow(spec, subs, script_engine=RegistryScriptEngine(ctx_factory))


def serialize_workflow(workflow) -> str:
    return _serializer().serialize_json(workflow)


def deserialize_workflow(state_json: str, ctx_factory: CtxFactory) -> BpmnWorkflow:
    workflow = _serializer().deserialize_json(state_json)
    # §5.2 — the script engine is NOT part of the serialized state.
    workflow.script_engine = RegistryScriptEngine(ctx_factory)
    return workflow
