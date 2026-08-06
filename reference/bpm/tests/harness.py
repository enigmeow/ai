"""§10.1 — BPMN contract-test harness. No database."""
from __future__ import annotations

from pathlib import Path

from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine
from SpiffWorkflow.bpmn.specs.event_definitions import SignalEventDefinition
from SpiffWorkflow.bpmn.util.event import BpmnEvent
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.util.task import TaskState

from app.bpm.engine import parse_spec

BPMN_DIR = Path(__file__).resolve().parent.parent / "app" / "bpmn"


class RecordingEngine(PythonScriptEngine):
    def __init__(self, results=None):
        super().__init__()
        self.calls: list[str] = []
        self._results = results or {}

    def execute(self, task, script, external_context=None):
        calls, results = self.calls, self._results
        def _dispatch(name):
            calls.append(name)
            out = results.get(name, {"dispatched": name})
            if isinstance(out, dict):
                # §10.1: the harness MUST mirror the real _dispatch and merge
                # results into task.data, or no gateway downstream of a service
                # task can see the flag it branches on.
                task.data.update(out)
            return out
        ext = dict(external_context or {})
        ext["_dispatch"] = _dispatch
        return super().execute(task, script, ext)


def load(process_key: str, *, results=None, xml: str | None = None, seed=None):
    text = xml if xml is not None else (BPMN_DIR / f"{process_key}.bpmn").read_text()
    spec, subs = parse_spec(text, process_key)
    engine = RecordingEngine(results)
    wf = BpmnWorkflow(spec, subs, script_engine=engine)
    if seed:
        wf.data.update(seed)
        for t in wf.get_tasks(state=TaskState.READY):
            t.data.update(seed)
    wf.do_engine_steps()
    return wf, engine


def ready_user_tasks(wf):
    return [
        t for t in wf.get_tasks(state=TaskState.READY)
        if type(t.task_spec).__name__ in ("UserTask", "ManualTask", "NoneTask")
    ]


def signal(wf, name):
    wf.send_event(BpmnEvent(SignalEventDefinition(name)))
    wf.do_engine_steps()


def completed_bpmn_ids(wf):
    return [
        t.task_spec.bpmn_id
        for t in wf.get_tasks()
        if t.state == TaskState.COMPLETED and getattr(t.task_spec, "bpmn_id", None)
    ]


def waiting_bpmn_ids(wf):
    return sorted(
        {
            t.task_spec.bpmn_id
            for t in wf.get_tasks(state=TaskState.WAITING)
            if getattr(t.task_spec, "bpmn_id", None)
        }
    )
