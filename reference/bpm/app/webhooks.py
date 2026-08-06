"""§7.2 — webhooks: correlate, signal, tolerate.

The HTTP layer itself is not in the spec (§5 says so); this is the routing core
of it, written as a plain function so it is testable without a web framework.
"""
from __future__ import annotations

import logging

from app.bpm.service import WorkflowService
from app.models.domain import Order

log = logging.getLogger("bpm.webhooks")

# Rule 1: a static dict maps provider event -> domain signal.
_STRIPE_TO_ORDER_SIGNAL = {
    "payment_intent.succeeded": "payment_captured",
    "payment_intent.payment_failed": "order_payment_failed",
    "charge.dispute.created": "dispute_opened",
}


def handle_stripe_event(db, event_type: str, order: Order, payload: dict) -> dict:
    fired: list[str] = []
    wf = WorkflowService(db)

    # Rule 4: do the domain row update INLINE and treat the signal as
    # best-effort. A provider that gets a 500 retries and eventually disables
    # the endpoint.
    if event_type == "payment_intent.succeeded":
        order.payment_status = "paid"
        db.flush()

    if (name := _STRIPE_TO_ORDER_SIGNAL.get(event_type)):
        try:
            # Rule 2: correlate by (object_type, object_id), not by process key.
            inst = wf.signal_by_correlation(
                object_type="order", object_id=order.id, signal_name=name,
                payload={"stripe_event_type": event_type, "stripe_payload": payload},
            )
            if inst is not None:
                fired.append(f"order:{name}")
        except Exception as exc:
            log.warning("order signal routing failed event=%s err=%s", event_type, exc)
    return {"signaled": fired}
