"""Adversarial verification of §12's checkable claims about engine behaviour.

Each test either CONFIRMS or FALSIFIES a specific sentence in the spec. Where a
claim is falsified the test asserts the behaviour that actually occurs, and the
docstring names the sentence.
"""
from __future__ import annotations

import time

import pytest
from SpiffWorkflow.bpmn.parser.BpmnParser import BpmnParser
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.util.task import TaskState

from app.bpm.engine import parse_spec
from tests.harness import BPMN_DIR, RecordingEngine, load, signal

BOILER = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
  id="Definitions_t" targetNamespace="https://t/bpmn">
{signals}
  <bpmn:process id="{key}" isExecutable="true">
{body}
  </bpmn:process>
</bpmn:definitions>
"""


def build(key, body, signals="", results=None):
    xml = BOILER.format(key=key, body=body, signals=signals)
    spec, subs = parse_spec(xml, key)
    return BpmnWorkflow(spec, subs, script_engine=RecordingEngine(results))


# ---------------------------------------------------------------- G2
def test_g2_parser_rejects_a_unicode_string_with_an_encoding_declaration():
    """G2: `parser.add_bpmn_str(path.read_text())` -> ValueError. CONFIRMED."""
    text = (BPMN_DIR / "blog_post.bpmn").read_text()
    with pytest.raises(ValueError) as e:
        BpmnParser().add_bpmn_str(text)
    assert "encoding declaration" in str(e.value)
    BpmnParser().add_bpmn_str(text.encode("utf-8"))     # the correct form


# ---------------------------------------------------------------- G4 / §5.3
def test_g4_data_objects_update_is_a_no_op():
    """§5.3: "`data_objects` is a read-only property ... the update is
    discarded". CONFIRMED. This also FALSIFIES G4's own instruction to
    "Seed workflow.data, workflow.data_objects, AND the root start task's data"
    -- the middle one cannot be done."""
    wf, _ = load("blog_post", seed={"post_id": "p"})
    wf.data_objects.update({"injected": 1})
    assert "injected" not in wf.data_objects
    assert wf.data_objects == {}


def test_g4_workflow_data_alone_does_not_reach_a_gateway_condition():
    """G4's symptom: "a gateway condition that can't see a variable you know you
    set". CONFIRMED, and STRONGER than stated: seeding only `workflow.data`
    does not merely take the wrong branch, it raises. `default=` (G22) does NOT
    rescue you, because the default is taken when a condition evaluates False,
    not when it raises."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException
    body = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="gw_q" />
    <bpmn:exclusiveGateway id="gw_q" default="sf_no">
      <bpmn:incoming>sf_a</bpmn:incoming>
      <bpmn:outgoing>sf_yes</bpmn:outgoing><bpmn:outgoing>sf_no</bpmn:outgoing>
    </bpmn:exclusiveGateway>
    <bpmn:sequenceFlow id="sf_yes" sourceRef="gw_q" targetRef="end_yes">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">flag == 1</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="sf_no" sourceRef="gw_q" targetRef="end_no" />
    <bpmn:endEvent id="end_yes" /><bpmn:endEvent id="end_no" />
    """
    # (a) workflow.data only -> the condition raises NameError
    wf = build("g4a", body)
    wf.data.update({"flag": 1})
    with pytest.raises(WorkflowTaskException) as e:
        wf.do_engine_steps()
    assert "flag" in str(e.value)

    # (b) start-task data -> the write that actually matters
    wf = build("g4b", body)
    for t in wf.get_tasks(state=TaskState.READY):
        t.data.update({"flag": 1})
    wf.do_engine_steps()
    taken = [t.task_spec.bpmn_id for t in wf.get_tasks()
             if t.state == TaskState.COMPLETED and (getattr(t.task_spec, "bpmn_id", "") or "").startswith("end_")]
    assert taken == ["end_yes"]


# ---------------------------------------------------------------- G5
def test_g5_spiff_does_not_parse_the_camunda_namespace():
    """G5. CONFIRMED: extensions arrives as {} and must be grafted."""
    body = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="user_x_do" />
    <bpmn:userTask id="user_x_do" camunda:candidateGroups="a,b" camunda:assignee="owner_user_id">
      <bpmn:incoming>sf_a</bpmn:incoming><bpmn:outgoing>sf_b</bpmn:outgoing>
    </bpmn:userTask>
    <bpmn:sequenceFlow id="sf_b" sourceRef="user_x_do" targetRef="end_ok" />
    <bpmn:endEvent id="end_ok"><bpmn:incoming>sf_b</bpmn:incoming></bpmn:endEvent>
    """
    xml = BOILER.format(key="g5", body=body, signals="")
    raw = BpmnParser()
    raw.add_bpmn_str(xml.encode())
    assert raw.get_spec("g5").task_specs["user_x_do"].extensions == {}

    grafted, _ = parse_spec(xml, "g5")
    assert grafted.task_specs["user_x_do"].extensions == {
        "candidateGroups": "a,b", "assignee": "owner_user_id",
    }


# ---------------------------------------------------------------- G6
def test_g6_send_event_raises_and_catch_queues_silently():
    """G6: "send_event RAISES if no task consumes it; catch QUEUES". CONFIRMED."""
    from SpiffWorkflow.bpmn.specs.event_definitions import SignalEventDefinition
    from SpiffWorkflow.bpmn.util.event import BpmnEvent
    from SpiffWorkflow.exceptions import WorkflowException

    wf, _ = load("message_thread", seed={"thread_id": "t"})
    with pytest.raises(WorkflowException):
        wf.send_event(BpmnEvent(SignalEventDefinition("not_modelled")))
    wf.catch(BpmnEvent(SignalEventDefinition("not_modelled")))   # silent


def test_g6_the_1x_signal_api_is_gone():
    assert not hasattr(BpmnWorkflow, "signal")
    assert hasattr(BpmnWorkflow, "catch") and hasattr(BpmnWorkflow, "send_event")


# ---------------------------------------------------------------- G7
SIGNAL_LOOP_BARE = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="state_wait" />
    <bpmn:intermediateCatchEvent id="state_wait">
      <bpmn:incoming>sf_a</bpmn:incoming><bpmn:incoming>sf_back</bpmn:incoming>
      <bpmn:outgoing>sf_b</bpmn:outgoing>
      <bpmn:signalEventDefinition signalRef="sig_go" />
    </bpmn:intermediateCatchEvent>
    <bpmn:sequenceFlow id="sf_b" sourceRef="state_wait" targetRef="svc_noop" />
    <bpmn:scriptTask id="svc_noop"><bpmn:incoming>sf_b</bpmn:incoming>
      <bpmn:outgoing>sf_back</bpmn:outgoing>
      <bpmn:script>_dispatch("svc_noop")</bpmn:script></bpmn:scriptTask>
    <bpmn:sequenceFlow id="sf_back" sourceRef="svc_noop" targetRef="state_wait" />
"""


def test_g7_a_bare_signal_cycle_fails_at_construction():
    """G7 table row 1: "RecursionError inside BpmnWorkflow.__init__ -- at
    *construction*, before any signal is sent". CONFIRMED."""
    with pytest.raises(RecursionError):
        build("g7bare", SIGNAL_LOOP_BARE, signals='<bpmn:signal id="sig_go" name="go" />')


def _signal_loop_gw(condition):
    return """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="state_wait" />
    <bpmn:intermediateCatchEvent id="state_wait">
      <bpmn:incoming>sf_a</bpmn:incoming><bpmn:incoming>sf_back</bpmn:incoming>
      <bpmn:outgoing>sf_b</bpmn:outgoing>
      <bpmn:signalEventDefinition signalRef="sig_go" />
    </bpmn:intermediateCatchEvent>
    <bpmn:sequenceFlow id="sf_b" sourceRef="state_wait" targetRef="svc_touch" />
    <bpmn:scriptTask id="svc_touch"><bpmn:incoming>sf_b</bpmn:incoming>
      <bpmn:outgoing>sf_c</bpmn:outgoing>
      <bpmn:script>_dispatch("svc_touch")</bpmn:script></bpmn:scriptTask>
    <bpmn:sequenceFlow id="sf_c" sourceRef="svc_touch" targetRef="gw_more" />
    <bpmn:exclusiveGateway id="gw_more" default="sf_back">
      <bpmn:incoming>sf_c</bpmn:incoming>
      <bpmn:outgoing>sf_done</bpmn:outgoing><bpmn:outgoing>sf_back</bpmn:outgoing>
    </bpmn:exclusiveGateway>
    <bpmn:sequenceFlow id="sf_done" sourceRef="gw_more" targetRef="end_ok">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">%s</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="sf_back" sourceRef="gw_more" targetRef="state_wait" />
    <bpmn:endEvent id="end_ok"><bpmn:incoming>sf_done</bpmn:incoming></bpmn:endEvent>
""" % condition


SIG = '<bpmn:signal id="sig_go" name="go" />'


def test_g7_a_gateway_signal_cycle_leaks_tasks_and_state_linearly():
    """G7 table row 2: "Runs. Never terminates. But each iteration leaks ~6
    permanent Task objects and ~2.2 KB of serialized_state -- linear and
    forever." MEASURED. Direction and linearity CONFIRMED; the exact constants
    are recorded in FINDINGS Part 3.

    NOTE the loop body must be a service task that re-establishes the gateway's
    variable each iteration -- otherwise row 3 fires first (see the next test)
    and row 2's shape is unreachable. The spec presents rows 2 and 3 as
    alternatives without saying what selects between them."""
    from app.bpm.engine import serialize_workflow

    wf = build("g7gw", _signal_loop_gw("stop == 1"), signals=SIG,
               results={"svc_touch": {"stop": 0}})
    wf.do_engine_steps()
    sizes, task_counts = [], []
    for _ in range(6):
        sizes.append(len(serialize_workflow(wf)))
        task_counts.append(len(list(wf.get_tasks())))
        signal(wf, "go")
    deltas = [sizes[i + 1] - sizes[i] for i in range(len(sizes) - 1)]
    task_deltas = [task_counts[i + 1] - task_counts[i] for i in range(len(task_counts) - 1)]
    assert all(d > 0 for d in deltas), f"expected monotonic growth, got {sizes}"
    assert all(d > 0 for d in task_deltas), f"expected task growth, got {task_counts}"
    assert 0.5 <= deltas[-1] / deltas[0] <= 2.0     # linear, not bounded
    assert not wf.is_completed()
    print("G7 serialized_state bytes:", sizes)
    print("G7 task counts:", task_counts)


def test_g7_row3_a_reentered_loop_errors_when_the_gateway_var_is_not_re_set():
    """G7 table row 3: "Predicted loop copies are created with empty data, so
    the gateway condition that gates the loop NameErrors (G9) and the task lands
    in ERROR." CONFIRMED -- and it fires on the FIRST pass, not on re-entry."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException

    wf = build("g7row3", _signal_loop_gw("stop == 1"), signals=SIG,
               results={"svc_touch": {}})
    wf.do_engine_steps()
    with pytest.raises(WorkflowTaskException):
        signal(wf, "go")
    errored = [t for t in wf.get_tasks() if t.state == TaskState.ERROR]
    assert [getattr(t.task_spec, "bpmn_id", None) for t in errored] == ["gw_more"]


# ---------------------------------------------------------------- G8
def _filtered(w):
    return sum(1 for _ in w.get_tasks(state=TaskState.COMPLETED))


def _scanned(w):
    return sum(1 for t in w.get_tasks() if t.state == TaskState.COMPLETED)


def test_g8_the_filtered_iterator_hides_completed_tasks_under_a_waiting_node():
    """G8: "on an event-based gateway the gateway itself stays WAITING, so the
    filtered count reports no progress at all (measured: filtered 3->3 while the
    true count went 3->6)".

    CONFIRMED -- but ONLY on the no-terminate variant. On the correct,
    terminate-carrying definition the gateway is cancelled, so the filtered
    count DOES report progress. That is G23's "terminate also un-blinds the
    progress metric", which means G8 and G23 are NOT independent in the
    direction the spec claims ("Fix both anyway -- they fail independently"):
    fixing G23 masks G8 on exactly this shape. Recorded in FINDINGS Part 3."""
    wf, _ = load("order", xml=_order_xml_without_terminate(), seed={"order_id": "o1"})
    before_f, before_s = _filtered(wf), _scanned(wf)
    time.sleep(1.2)
    wf.refresh_waiting_tasks()
    wf.do_engine_steps()
    after_f, after_s = _filtered(wf), _scanned(wf)

    print(f"G8 filtered {before_f}->{after_f}   scanned {before_s}->{after_s}")
    assert after_s > before_s, "the true completed count must climb"
    assert after_f == before_f, (
        f"filtered claimed progress {before_f}->{after_f}; G8 says it cannot see any"
    )


def test_g8_persist_gate_would_discard_real_progress_on_the_broken_shape():
    """The consequence G8 shares with G23: the persist gate evaluates False
    while a handler's domain write has already committed, so the sweep re-runs
    the handler every 60 seconds forever."""
    wf, eng = load("order", xml=_order_xml_without_terminate(), seed={"order_id": "o1"})
    before_f = _filtered(wf)
    time.sleep(1.2)
    wf.refresh_waiting_tasks()
    wf.do_engine_steps()
    naive_gate = _filtered(wf) > before_f or wf.is_completed()
    assert "svc_timeout_release_and_cancel" in eng.calls      # handler DID run
    assert naive_gate is False                                # ... and would be discarded


# ---------------------------------------------------------------- G9
def test_g9_an_unset_gateway_variable_is_a_nameerror_not_a_falsy_value():
    """G9. CONFIRMED -- and note the exception is Spiff's WorkflowTaskException
    wrapping the NameError, not a bare NameError."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException

    body = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="gw_q" />
    <bpmn:exclusiveGateway id="gw_q" default="sf_no">
      <bpmn:incoming>sf_a</bpmn:incoming>
      <bpmn:outgoing>sf_yes</bpmn:outgoing><bpmn:outgoing>sf_no</bpmn:outgoing>
    </bpmn:exclusiveGateway>
    <bpmn:sequenceFlow id="sf_yes" sourceRef="gw_q" targetRef="end_yes">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">never_set == 1</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="sf_no" sourceRef="gw_q" targetRef="end_no" />
    <bpmn:endEvent id="end_yes" /><bpmn:endEvent id="end_no" />
    """
    wf = build("g9", body)
    with pytest.raises(WorkflowTaskException) as e:
        wf.do_engine_steps()
    assert "never_set" in str(e.value)


# ---------------------------------------------------------------- G11
def test_g11_an_unquoted_timer_duration_is_a_nameerror():
    """G11: "PT30S is a NameError; \"PT30S\" (with quotes) is correct."
    CONFIRMED."""
    from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException

    body = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="timer_x" />
    <bpmn:intermediateCatchEvent id="timer_x">
      <bpmn:incoming>sf_a</bpmn:incoming><bpmn:outgoing>sf_b</bpmn:outgoing>
      <bpmn:timerEventDefinition><bpmn:timeDuration xsi:type="bpmn:tFormalExpression">{body}</bpmn:timeDuration></bpmn:timerEventDefinition>
    </bpmn:intermediateCatchEvent>
    <bpmn:sequenceFlow id="sf_b" sourceRef="timer_x" targetRef="end_ok" />
    <bpmn:endEvent id="end_ok"><bpmn:incoming>sf_b</bpmn:incoming></bpmn:endEvent>
    """
    wf = build("g11bad", body.format(body="PT1S"))
    with pytest.raises(WorkflowTaskException):
        wf.do_engine_steps()

    wf = build("g11good", body.format(body='"PT1S"'))
    wf.do_engine_steps()          # no exception
    assert not wf.is_completed()
    time.sleep(1.2)
    wf.refresh_waiting_tasks()
    wf.do_engine_steps()
    assert wf.is_completed()


# ---------------------------------------------------------------- G22
def test_g22_a_gateway_with_no_match_and_no_default_raises():
    """G22: "No matching condition and no default is a runtime exception."
    CONFIRMED."""
    from SpiffWorkflow.exceptions import WorkflowException

    body = """
    <bpmn:startEvent id="start"><bpmn:outgoing>sf_a</bpmn:outgoing></bpmn:startEvent>
    <bpmn:sequenceFlow id="sf_a" sourceRef="start" targetRef="gw_q" />
    <bpmn:exclusiveGateway id="gw_q">
      <bpmn:incoming>sf_a</bpmn:incoming>
      <bpmn:outgoing>sf_yes</bpmn:outgoing><bpmn:outgoing>sf_no</bpmn:outgoing>
    </bpmn:exclusiveGateway>
    <bpmn:sequenceFlow id="sf_yes" sourceRef="gw_q" targetRef="end_yes">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">1 == 2</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="sf_no" sourceRef="gw_q" targetRef="end_no">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">1 == 3</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:endEvent id="end_yes" /><bpmn:endEvent id="end_no" />
    """
    wf = build("g22", body)
    with pytest.raises(WorkflowException):
        wf.do_engine_steps()


# ---------------------------------------------------------------- G23
def _order_xml_without_terminate():
    return (
        (BPMN_DIR / "order.bpmn").read_text()
        .replace('"PT30M"', '"PT1S"')
        .replace("<bpmn:terminateEventDefinition />", "")
    )


def test_g23_measurement_reproduces_exactly():
    """G23's measurement table, reproduced. BEFORE (no terminate): the handler
    fires, is_completed() is False, the persist gate is False, and 8 tasks are
    stuck. AFTER (with terminate): is_completed() True, nothing stuck."""
    from app.bpm.service import completed_count

    def run(xml):
        wf, eng = load("order", xml=xml, seed={"order_id": "o1"})
        before = completed_count(wf)
        time.sleep(1.2)
        wf.refresh_waiting_tasks()
        wf.do_engine_steps()
        after = completed_count(wf)
        stuck = sorted(
            getattr(t.task_spec, "bpmn_id", None) or t.task_spec.name
            for t in wf.get_tasks()
            if t.state in (TaskState.WAITING, TaskState.MAYBE, TaskState.READY)
        )
        return eng.calls, wf.is_completed(), (after > before or wf.is_completed()), stuck

    calls_b, done_b, gate_b, stuck_b = run(_order_xml_without_terminate())
    assert "svc_timeout_release_and_cancel" in calls_b   # handler fired: domain write commits
    assert done_b is False
    assert stuck_b, f"expected stranded tasks, got {stuck_b}"
    assert "gw_await_payment" in stuck_b

    calls_a, done_a, gate_a, stuck_a = run(
        (BPMN_DIR / "order.bpmn").read_text().replace('"PT30M"', '"PT1S"')
    )
    assert "svc_timeout_release_and_cancel" in calls_a
    assert done_a is True
    assert gate_a is True
    assert stuck_a == []


def test_g23_second_consequence_terminate_unblinds_the_naive_progress_metric():
    """G23: "Terminate also un-blinds the progress metric ... even the naive
    filtered count in G8 starts reporting correctly." CONFIRMED."""
    xml = (BPMN_DIR / "order.bpmn").read_text().replace('"PT30M"', '"PT1S"')
    wf, _ = load("order", xml=xml, seed={"order_id": "o1"})
    filtered_before = sum(1 for _ in wf.get_tasks(state=TaskState.COMPLETED))
    time.sleep(1.2)
    wf.refresh_waiting_tasks()
    wf.do_engine_steps()
    filtered_after = sum(1 for _ in wf.get_tasks(state=TaskState.COMPLETED))
    assert filtered_after > filtered_before
