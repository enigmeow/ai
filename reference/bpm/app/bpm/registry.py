"""§5.1 — binding BPMN ids to Python callables. Lifted near-verbatim."""
from __future__ import annotations

from typing import Callable

_REGISTRY: dict[str, Callable] = {}


def service_task(task_id: str) -> Callable[[Callable], Callable]:
    """Decorator: bind a Python callable to a BPMN service-task id."""

    def wrap(fn: Callable) -> Callable:
        if task_id in _REGISTRY:
            raise ValueError(f"service task {task_id!r} already registered")
        _REGISTRY[task_id] = fn
        return fn

    return wrap


def get_handler(task_id: str) -> Callable | None:
    return _REGISTRY.get(task_id)


def registered_tasks() -> list[str]:
    return sorted(_REGISTRY.keys())
