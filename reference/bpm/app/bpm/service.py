"""§5.3 — `WorkflowService`, the only public API.

Domain code never touches SpiffWorkflow.  Every mutating method follows the
five-beat pattern in §5.3:

    1. load/create the ProcessInstance row
    2. deserialize/build the workflow with a ctx factory bound to instance+actor
    3. apply input, step the engine
    4. write state_transitions rows
    5. persist: serialize, recompute current_states, re-project workflow_tasks,
       flush()   <- NEVER commit
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from SpiffWorkflow.bpmn.specs.event_definitions import SignalEventDefinition
from SpiffWorkflow.bpmn.util.event import BpmnEvent
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.util.task import TaskState

from app.bpm import audit
from app.bpm.engine import (
    ServiceTaskContext,
    build_workflow,
    deserialize_workflow,
    merged_task_data,
    serialize_workflow,
)
from app.models.state_transition import StateTransition
from app.models.workflow import ProcessDefinition, ProcessInstance, WorkflowTask, utcnow

log = logging.getLogger("bpm.service")

RUNNING, COMPLETED, ERROR, CANCELED = "running", "completed", "error", "canceled"

_USER_TASK_CLASSES = {"UserTask", "ManualTask", "NoneTask"}


# --------------------------------------------------------------------------
# G24 — candidateGroups is a LIST. Every reader goes through these two helpers.
# --------------------------------------------------------------------------
def role_names_of(assignee_role: Optional[str]) -> list[str]:
    return [r.strip() for r in (assignee_role or "").split(",") if r.strip()]


def user_matches_role(assignee_role: Optional[str], user) -> bool:
    return any(user.has_role(r) for r in role_names_of(assignee_role))


def task_visible_to(task: WorkflowTask, user) -> bool:
    """§7.5's single visibility predicate."""
    if user is None:
        return False
    if task.assignee_user_id and task.assignee_user_id == user.id:
        return True
    return user_matches_role(task.assignee_role, user)


# --------------------------------------------------------------------------
def _spec_class(task) -> str:
    return type(task.task_spec).__name__


def _is_user_task(task) -> bool:
    return _spec_class(task) in _USER_TASK_CLASSES


def _bpmn_id(task) -> Optional[str]:
    return getattr(task.task_spec, "bpmn_id", None)


def _task_type(task) -> str:
    cls = _spec_class(task)
    if cls in _USER_TASK_CLASSES:
        return "user"
    if cls == "ScriptTask":
        return "script"
    if "Timer" in cls or _has_timer(task.task_spec):
        return "timer"
    return "service"


def _has_timer(task_spec) -> bool:
    ed = getattr(task_spec, "event_definition", None)
    return ed is not None and "Timer" in type(ed).__name__


def _catchable_signals(workflow) -> list[str]:
    """§5.3.  `event_definition` is SINGULAR on a 3.1.2 catch-event spec; the
    plural is kept only as a fallback for composite definitions."""
    signals: list[str] = []

    def _collect(ed):
        if ed is None:
            return
        if type(ed).__name__ == "SignalEventDefinition":
            if getattr(ed, "name", None):
                signals.append(ed.name)
            return
        for child in getattr(ed, "event_definitions", None) or []:
            _collect(child)

    for t in workflow.get_tasks(state=TaskState.WAITING):
        _collect(getattr(t.task_spec, "event_definition", None))
        for ed in getattr(t.task_spec, "event_definitions", None) or []:
            _collect(ed)
    return sorted(set(signals))


def completed_count(workflow) -> int:
    """G8 — scan and filter in Python.  `get_tasks(state=COMPLETED)` stops
    descending at the first ancestor below min_state, so any WAITING node hides
    everything completed beneath it."""
    return sum(1 for t in workflow.get_tasks() if t.state == TaskState.COMPLETED)


def _apply_initial_data(workflow, seed: dict) -> None:
    """§5.3.  NOTE: no `workflow.data_objects.update(seed)` — it is a read-only
    property returning `self.data.get('data_objects', {})`, so the update lands
    on a throwaway dict.  Verified empirically; see FINDINGS Part 3."""
    if not seed:
        return
    workflow.data.update(seed)
    for t in workflow.get_tasks(state=TaskState.READY):
        if type(t.task_spec).__name__ == "BpmnStartTask":
            t.data.update(seed)
            break
    else:
        for t in workflow.get_tasks(state=TaskState.READY):
            t.data.update(seed)


def _apply_payload(workflow, payload: dict) -> None:
    """§5.3: signal() does the same three-way write AND updates every
    waiting/ready task's local data — a condition evaluates against the task
    that is about to fire, not the workflow root (G4)."""
    if not payload:
        return
    workflow.data.update(payload)
    for t in workflow.get_tasks():
        if t.state in (TaskState.READY, TaskState.WAITING):
            t.data.update(payload)


class WorkflowService:
    def __init__(self, db, error_session_factory=None) -> None:
        self.db = db
        # NOT IN THE SPEC. §5.3 says a failed step flips the instance to
        # status='error', writes an audit row, "and the exception re-raises so
        # the request fails loudly". Those two are incompatible on one session:
        # a request that fails loudly is rolled back, taking the error row and
        # the status flip with it. So `status='error'` -- §11.5's headline
        # monitoring signal, and the precondition for G25 -- is never durably
        # written for any router-driven failure. The error record needs its own
        # transaction. See FINDINGS Part 3.
        self.error_session_factory = error_session_factory

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _ctx_factory(self, instance: Optional[ProcessInstance], actor, workflow_ref: dict):
        def factory(task, task_name: str) -> ServiceTaskContext:
            wf = workflow_ref.get("wf")
            return ServiceTaskContext(
                db=self.db,
                data=merged_task_data(wf, task),
                actor=actor,
                instance=instance,
                task_name=task_name,
                task=task,
            )

        return factory

    def _latest_definition(self, process_key: str) -> ProcessDefinition:
        row = (
            self.db.query(ProcessDefinition)
            .filter(ProcessDefinition.process_key == process_key)
            .order_by(ProcessDefinition.version.desc())
            .first()
        )
        if row is None:
            raise LookupError(f"no deployed process definition for {process_key!r}")
        return row

    def _load_workflow(self, instance: ProcessInstance, actor) -> BpmnWorkflow:
        ref: dict = {}
        wf = deserialize_workflow(instance.serialized_state, self._ctx_factory(instance, actor, ref))
        ref["wf"] = wf
        return wf

    def _active_state_names(self, workflow) -> list[str]:
        names = []
        for t in workflow.get_tasks():
            if t.state in (TaskState.READY, TaskState.WAITING):
                bid = _bpmn_id(t)
                if bid:
                    names.append(bid)
        return sorted(set(names))

    def _persist(self, workflow, instance: ProcessInstance) -> ProcessInstance:
        """Beat 5.  NEVER commits — the caller's request handler owns the txn."""
        instance.serialized_state = serialize_workflow(workflow)
        instance.current_states = self._active_state_names(workflow)
        instance.updated_at = utcnow()
        if workflow.is_completed():
            instance.status = COMPLETED
            instance.completed_at = instance.completed_at or utcnow()
        self._reproject_tasks(workflow, instance)
        self.db.flush()
        return instance

    # -- task projection ------------------------------------------------
    def _resolve_assignment(self, workflow, task) -> tuple[Optional[str], Optional[str]]:
        ext = getattr(task.task_spec, "extensions", None) or {}
        # G24: keep the WHOLE comma-separated list.
        role = ext.get("candidateGroups") or None
        user_id = None
        var_name = ext.get("assignee")
        if var_name:
            # G5: camunda:assignee holds the NAME OF A WORKFLOW VARIABLE.
            merged = merged_task_data(workflow, task)
            val = merged.get(var_name)
            user_id = str(val) if val is not None else None
        return user_id, role

    def _reproject_tasks(self, workflow, instance: ProcessInstance) -> None:
        """§5.3 — a reconciliation, not an append."""
        ready = [t for t in workflow.get_tasks(state=TaskState.READY) if _is_user_task(t)]
        ready_by_id = {str(t.id): t for t in ready}

        rows = (
            self.db.query(WorkflowTask)
            .filter(
                WorkflowTask.process_instance_id == instance.id,
                WorkflowTask.status == "ready",
            )
            .all()
        )
        existing_ids = set()
        for row in rows:
            if row.spiff_task_id in ready_by_id:
                existing_ids.add(row.spiff_task_id)   # still ready -> leave alone (preserves a claim)
            else:
                row.status = "canceled"
                row.completed_at = utcnow()

        for tid, task in ready_by_id.items():
            if tid in existing_ids:
                continue
            user_id, role = self._resolve_assignment(workflow, task)
            row = WorkflowTask(
                process_instance_id=instance.id,
                task_spec_name=_bpmn_id(task) or task.task_spec.name,
                spiff_task_id=tid,
                task_type=_task_type(task),
                status="ready",
                assignee_user_id=user_id,
                assignee_role=role,
                created_at=utcnow(),
            )
            self.db.add(row)
            audit.record(
                self.db,
                process_instance_id=instance.id,
                object_type=instance.object_type,
                object_id=instance.object_id,
                event=audit.TASK_STARTED,
                task_spec_name=row.task_spec_name,
                to_state=row.task_spec_name,
                metadata={"assignee_role": role, "assignee_user_id": user_id},
            )

    def _record_error(self, workflow, instance: ProcessInstance, actor, exc: Exception) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:2000]
        # Persist the HALF-STEPPED workflow deliberately. §5.3 says the instance
        # is "left where it died for inspection"; it does not say whether the
        # serialized state is saved. It must be, or the token position -- which
        # includes the catch event the signal already consumed -- is lost and
        # the signal can never be re-delivered. See FINDINGS Part 3 on §7.6's
        # claim that the 60s tick re-drives an errored instance.
        try:
            state = serialize_workflow(workflow)
        except Exception:  # pragma: no cover - defensive
            state = None

        if self.error_session_factory is not None:
            edb = self.error_session_factory()
            try:
                inst = edb.get(ProcessInstance, instance.id)
                if inst is not None:
                    inst.status = ERROR
                    inst.updated_at = utcnow()
                    if state is not None:
                        inst.serialized_state = state
                    audit.record(
                        edb,
                        process_instance_id=inst.id,
                        object_type=inst.object_type,
                        object_id=inst.object_id,
                        event=audit.ERROR,
                        reason=reason,
                        actor_user_id=getattr(actor, "id", None),
                    )
                    edb.commit()
            except Exception:  # pragma: no cover
                edb.rollback()
                log.exception("failed to durably record workflow error")
            finally:
                edb.close()
            return

        # NOT IN THE SPEC either: if the step failed because of a DATABASE error
        # (G16's DataError 1406 is the canonical one), the caller's session is
        # already poisoned with PendingRollbackError, so writing the audit row
        # on it raises and MASKS the original exception. §5.3 assumes the error
        # row can always be written on the caller's session; it cannot.
        try:
            instance.status = ERROR
            instance.updated_at = utcnow()
            if state is not None:
                instance.serialized_state = state
            audit.record(
                self.db,
                process_instance_id=instance.id,
                object_type=instance.object_type,
                object_id=instance.object_id,
                event=audit.ERROR,
                reason=reason,
                actor_user_id=getattr(actor, "id", None),
            )
            self.db.flush()
        except Exception:
            log.exception(
                "could not record workflow error on the caller's session "
                "(poisoned?); instance %s status not persisted", instance.id
            )

    def _step(self, workflow, instance: ProcessInstance, actor) -> None:
        """§5.3 error handling: flip to error, audit, re-raise."""
        try:
            workflow.do_engine_steps()
        except Exception as exc:
            self._record_error(workflow, instance, actor, exc)
            raise

    # ------------------------------------------------------------------
    # mutating API
    # ------------------------------------------------------------------
    def start(
        self,
        process_key: str,
        *,
        object_type: str,
        object_id: str,
        actor=None,
        data: Optional[dict] = None,
    ) -> ProcessInstance:
        # G19/§4.1 — uniqueness is behavioural: one RUNNING instance per object.
        existing = (
            self.db.query(ProcessInstance)
            .filter(
                ProcessInstance.object_type == object_type,
                ProcessInstance.object_id == object_id,
                ProcessInstance.status == RUNNING,
            )
            .first()
        )
        if existing is not None:
            raise ValueError(
                f"a running instance already exists for {object_type} {object_id}"
            )

        definition = self._latest_definition(process_key)
        instance = ProcessInstance(
            process_definition_id=definition.id,
            business_key=f"{process_key}:{object_id}",
            object_type=object_type,
            object_id=object_id,
            status=RUNNING,
            serialized_state="{}",
            current_states=[],
            started_at=utcnow(),
            updated_at=utcnow(),
        )
        self.db.add(instance)
        self.db.flush()          # need instance.id for audit + task rows

        ref: dict = {}
        workflow = build_workflow(
            definition.bpmn_xml, process_key, self._ctx_factory(instance, actor, ref)
        )
        ref["wf"] = workflow

        _apply_initial_data(workflow, dict(data or {}))

        audit.record(
            self.db,
            process_instance_id=instance.id,
            object_type=object_type,
            object_id=object_id,
            event=audit.STARTED,
            to_state="started",
            actor_user_id=getattr(actor, "id", None),
            metadata={"process_key": process_key, "version": definition.version},
        )
        self._step(workflow, instance, actor)
        self._persist(workflow, instance)
        self._audit_end_if_complete(workflow, instance, actor)
        return instance

    def _audit_end_if_complete(self, workflow, instance, actor) -> None:
        if instance.status == COMPLETED:
            already = (
                self.db.query(StateTransition)
                .filter(
                    StateTransition.process_instance_id == instance.id,
                    StateTransition.event == audit.ENDED,
                )
                .first()
            )
            if already is None:
                audit.record(
                    self.db,
                    process_instance_id=instance.id,
                    object_type=instance.object_type,
                    object_id=instance.object_id,
                    event=audit.ENDED,
                    to_state=",".join(self._terminal_end_names(workflow)) or "ended",
                    actor_user_id=getattr(actor, "id", None),
                )

    def _terminal_end_names(self, workflow) -> list[str]:
        out = []
        for t in workflow.get_tasks():
            if t.state == TaskState.COMPLETED and type(t.task_spec).__name__ == "EndEvent":
                bid = _bpmn_id(t)
                if bid:
                    out.append(bid)
        return sorted(set(out))

    def get_instance(self, business_key: str) -> Optional[ProcessInstance]:
        """§4.1/G19 — prefer the running instance, fall back to the most recent."""
        q = self.db.query(ProcessInstance).filter(ProcessInstance.business_key == business_key)
        running = q.filter(ProcessInstance.status == RUNNING).order_by(
            ProcessInstance.started_at.desc()
        ).first()
        if running is not None:
            return running
        return (
            self.db.query(ProcessInstance)
            .filter(ProcessInstance.business_key == business_key)
            .order_by(ProcessInstance.started_at.desc())
            .first()
        )

    def signal(
        self,
        business_key: str,
        signal_name: str,
        payload: Optional[dict] = None,
        actor=None,
    ) -> ProcessInstance:
        instance = self.get_instance(business_key)
        if instance is None:
            raise LookupError(f"no instance for business key {business_key!r}")
        if instance.status != RUNNING:
            # G25: an instance that is not running is deaf. Say so loudly.
            log.warning(
                "signal %r DROPPED for %s — instance %s is %s, not running",
                signal_name, business_key, instance.id, instance.status,
            )
            raise LookupError(
                f"instance for {business_key!r} is {instance.status}, not running"
            )
        return self._signal_instance(instance, signal_name, payload, actor)

    def _signal_instance(self, instance, signal_name, payload, actor) -> ProcessInstance:
        workflow = self._load_workflow(instance, actor)
        _apply_payload(workflow, dict(payload or {}))
        before = self._active_state_names(workflow)

        # G6: BpmnEvent wrapper. send_event RAISES if nothing consumes it.
        workflow.send_event(BpmnEvent(SignalEventDefinition(signal_name)))

        audit.record(
            self.db,
            process_instance_id=instance.id,
            object_type=instance.object_type,
            object_id=instance.object_id,
            event=audit.SIGNAL,
            from_state=",".join(before) or None,
            actor_user_id=getattr(actor, "id", None),
            reason=signal_name,
            metadata={"signal": signal_name, "payload": payload or {}},
        )
        self._step(workflow, instance, actor)
        self._persist(workflow, instance)
        self._audit_end_if_complete(workflow, instance, actor)
        return instance

    def signal_by_correlation(
        self,
        object_type: str,
        object_id: str,
        signal_name: str,
        payload: Optional[dict] = None,
        actor=None,
    ) -> Optional[ProcessInstance]:
        """§5.3 — the webhook entry point.  Returns None (not an error) when
        nothing is running, but distinguishes the two meanings of None (G25)."""
        instance = (
            self.db.query(ProcessInstance)
            .filter(
                ProcessInstance.object_type == object_type,
                ProcessInstance.object_id == object_id,
                ProcessInstance.status == RUNNING,
            )
            .order_by(ProcessInstance.started_at.desc())
            .first()
        )
        if instance is None:
            others = (
                self.db.query(ProcessInstance.id, ProcessInstance.status)
                .filter(
                    ProcessInstance.object_type == object_type,
                    ProcessInstance.object_id == object_id,
                )
                .limit(5)
                .all()
            )
            if not others:
                log.debug(
                    "signal %r dropped: no instance for %s %s",
                    signal_name, object_type, object_id,
                )
            else:
                log.warning(
                    "signal %r DROPPED for %s %s — instance(s) exist but none are "
                    "running: %s",
                    signal_name, object_type, object_id,
                    [(i, s) for i, s in others],
                )
            return None
        return self._signal_instance(instance, signal_name, payload, actor)

    def complete_user_task(self, task_id: int, actor, form_data: Optional[dict] = None) -> ProcessInstance:
        row = self.db.get(WorkflowTask, task_id)
        if row is None:
            raise LookupError(f"no workflow task {task_id}")
        if row.status != "ready":
            raise ValueError(f"task {task_id} is {row.status}, not ready")
        if not task_visible_to(row, actor):
            raise PermissionError(f"task {task_id} is not visible to {getattr(actor,'id',None)}")
        # UNDERSPECIFIED in §7.5: the claim rules refuse a claim on someone
        # else's task, but the visibility predicate used for COMPLETE does not,
        # so a claimed task would still be completable by anyone in the role.
        if row.assignee_user_id and row.assignee_user_id != getattr(actor, "id", None):
            raise PermissionError(f"task {task_id} is claimed by another user")

        instance = self.db.get(ProcessInstance, row.process_instance_id)
        if instance.status != RUNNING:
            raise ValueError(f"instance {instance.id} is {instance.status}")

        workflow = self._load_workflow(instance, actor)
        target = None
        for t in workflow.get_tasks(state=TaskState.READY):
            if str(t.id) == row.spiff_task_id:
                target = t
                break
        if target is None:
            raise LookupError(f"spiff task {row.spiff_task_id} is no longer ready")

        form_data = dict(form_data or {})
        target.data.update(form_data)
        # gateway conditions downstream evaluate against task data (G4)
        workflow.data.update(form_data)

        row.status = "completed"
        row.completed_at = utcnow()
        row.completed_by_user_id = getattr(actor, "id", None)
        row.form_data = form_data

        audit.record(
            self.db,
            process_instance_id=instance.id,
            object_type=instance.object_type,
            object_id=instance.object_id,
            event=audit.TASK_COMPLETED,
            task_spec_name=row.task_spec_name,
            from_state=row.task_spec_name,
            actor_user_id=getattr(actor, "id", None),
            metadata={"form_data": form_data},
        )

        target.run()
        self._step(workflow, instance, actor)
        self._persist(workflow, instance)
        self._audit_end_if_complete(workflow, instance, actor)
        return instance

    def cancel(self, business_key: str, actor, reason: str) -> ProcessInstance:
        """§7.6 — cancel does NOT step the workflow; a canceled instance is
        frozen for inspection.  `reason` is mandatory."""
        if not reason:
            raise ValueError("cancel requires a reason")
        instance = self.get_instance(business_key)
        if instance is None:
            raise LookupError(f"no instance for business key {business_key!r}")
        instance.status = CANCELED
        instance.updated_at = utcnow()
        instance.completed_at = utcnow()
        for row in (
            self.db.query(WorkflowTask)
            .filter(
                WorkflowTask.process_instance_id == instance.id,
                WorkflowTask.status == "ready",
            )
            .all()
        ):
            row.status = "canceled"
            row.completed_at = utcnow()
        audit.record(
            self.db,
            process_instance_id=instance.id,
            object_type=instance.object_type,
            object_id=instance.object_id,
            event=audit.CANCELED,
            actor_user_id=getattr(actor, "id", None),
            reason=reason,
        )
        self.db.flush()
        return instance

    def retry_failed_task(self, instance_id: int, actor=None) -> ProcessInstance:
        """§7.6/§14.3 say this is unimplemented in the reference implementation
        and that a fresh build should decide deliberately.  Decision: implement
        it, because G25 says an errored instance is otherwise both invisible AND
        unfixable.

        `_record_error` persisted the half-stepped workflow, so the failed task
        is sitting in Spiff's ERROR state with the token already past whatever
        catch event triggered it.  Retry resets that task and re-drives.
        """
        instance = self.db.get(ProcessInstance, instance_id)
        if instance is None:
            raise LookupError(f"no instance {instance_id}")
        if instance.status != ERROR:
            raise ValueError(f"instance {instance_id} is {instance.status}, not error")
        instance.status = RUNNING
        workflow = self._load_workflow(instance, actor)
        for t in list(workflow.get_tasks()):
            if t.state == TaskState.ERROR:
                workflow.reset_from_task_id(t.id)
                break
        audit.record(
            self.db,
            process_instance_id=instance.id,
            object_type=instance.object_type,
            object_id=instance.object_id,
            event="retry",
            actor_user_id=getattr(actor, "id", None),
        )
        self._step(workflow, instance, actor)
        self._persist(workflow, instance)
        self._audit_end_if_complete(workflow, instance, actor)
        return instance

    # ------------------------------------------------------------------
    # read-only API
    # ------------------------------------------------------------------
    def get_inbox(self, user) -> list[WorkflowTask]:
        """G24 — narrow in SQL, refine in Python (a CSV column breaks SQL IN)."""
        roles = [r.strip() for r in (user.roles_csv or "").split(",") if r.strip()]
        from sqlalchemy import or_

        clauses = [WorkflowTask.assignee_user_id == user.id]
        for r in roles:
            clauses.append(WorkflowTask.assignee_role.like(f"%{r}%"))
        candidates = (
            self.db.query(WorkflowTask)
            .filter(WorkflowTask.status == "ready", or_(*clauses))
            .order_by(WorkflowTask.created_at.asc())
            .all()
        )
        return [t for t in candidates if task_visible_to(t, user)]

    def get_history(self, object_type: str, object_id: str) -> list[StateTransition]:
        return (
            self.db.query(StateTransition)
            .filter(
                StateTransition.object_type == object_type,
                StateTransition.object_id == object_id,
            )
            .order_by(StateTransition.id.asc())
            .all()
        )

    def list_instances(self, *, status=None, process_key=None, limit=100) -> list[ProcessInstance]:
        q = self.db.query(ProcessInstance)
        if status:
            q = q.filter(ProcessInstance.status == status)
        if process_key:
            q = q.join(ProcessDefinition).filter(ProcessDefinition.process_key == process_key)
        return q.order_by(ProcessInstance.updated_at.desc()).limit(limit).all()

    def available_actions(self, business_key: str, user) -> list[dict]:
        instance = self.get_instance(business_key)
        if instance is None or instance.status != RUNNING:
            return []
        actions: list[dict] = []
        for row in (
            self.db.query(WorkflowTask)
            .filter(
                WorkflowTask.process_instance_id == instance.id,
                WorkflowTask.status == "ready",
            )
            .all()
        ):
            if task_visible_to(row, user):
                actions.append(
                    {
                        "kind": "user_task",
                        "task_id": row.id,
                        "task_spec_name": row.task_spec_name,
                    }
                )
        workflow = self._load_workflow(instance, user)
        for name in _catchable_signals(workflow):
            actions.append({"kind": "signal", "signal_name": name})
        return actions


# --------------------------------------------------------------------------
# §7.1.3 — the best-effort start wrapper
# --------------------------------------------------------------------------
def start_lifecycle(db, *, process_key, object_type, object_id, actor=None, data=None) -> None:
    try:
        WorkflowService(db).start(
            process_key, object_type=object_type, object_id=object_id,
            actor=actor, data=data or {},
        )
    except ValueError:
        pass  # already running for this object — idempotent, fine
    except Exception:
        # LookupError (no process definition) lands here too. It does NOT
        # self-heal. If lifecycles never start, grep for this line.
        log.exception(
            "start_lifecycle failed: %s for %s %s", process_key, object_type, object_id
        )


# --------------------------------------------------------------------------
# §6.4.2 — ledger transitions (Shape B's other half)
# --------------------------------------------------------------------------
def ledger_transition(
    db,
    *,
    object_type: str,
    object_id: str,
    event: str,
    from_state=None,
    to_state=None,
    actor_user_id=None,
    metadata=None,
) -> bool:
    inst = (
        db.query(ProcessInstance)
        .filter(
            ProcessInstance.object_type == object_type,
            ProcessInstance.object_id == object_id,
            ProcessInstance.status == RUNNING,
        )
        .order_by(ProcessInstance.started_at.desc())
        .first()
    )
    if inst is None:
        log.info("ledger_transition: no running lifecycle for %s %s", object_type, object_id)
        return False
    try:
        audit.record(
            db,
            process_instance_id=inst.id,
            object_type=object_type,
            object_id=object_id,
            event=event,
            from_state=from_state,
            to_state=to_state,
            actor_user_id=actor_user_id,
            metadata=metadata,
        )
        return True
    except Exception:
        log.exception("ledger_transition failed")  # audit must never break the write
        return False
