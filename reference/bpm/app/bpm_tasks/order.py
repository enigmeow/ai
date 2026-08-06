"""Shape C handlers.

The interesting one is the cancel pair: G26 says a destructive handler must
re-derive safety from the domain object's CURRENT state, never from the trigger
that invoked it, because a signal describes what was true when it was *sent*.
"""
from __future__ import annotations

import logging

from app.bpm.engine import ServiceTaskContext
from app.bpm.registry import service_task
from app.models.domain import EmailMessage, InventoryItem, Order

log = logging.getLogger("bpm.handlers.order")

PAID_STATUSES = ("paid", "captured")
SHIPPED_STATUSES = ("shipped", "delivered")


def _order(ctx: ServiceTaskContext) -> Order | None:
    return ctx.db.get(Order, ctx.get("order_id"))


def _cancel_block_reason(order: Order) -> str | None:
    """G26 — the guard, derived from the object, not from the signal."""
    if order.payment_status in PAID_STATUSES:
        return f"payment_{order.payment_status}"
    if order.fulfillment_status in SHIPPED_STATUSES:
        return f"fulfillment_{order.fulfillment_status}"
    if order.state == "canceled":
        return "already_canceled"
    return None


def _release_and_cancel(ctx: ServiceTaskContext, reason: str) -> dict:
    order = _order(ctx)
    if order is None:
        return {"canceled": False, "cancel_blocked": "missing_order"}
    blocked = _cancel_block_reason(order)
    if blocked:
        # Log the refusal, so a guard that fires is visible rather than silent.
        log.warning(
            "refusing to cancel order %s: %s (trigger=%s)", order.id, blocked, reason
        )
        return {"canceled": False, "cancel_blocked": blocked}
    item = ctx.db.get(InventoryItem, ctx.get("sku", "SKU-1"))
    if item is not None and order.inventory_reserved:
        item.reserved = max(0, item.reserved - order.inventory_reserved)
        order.inventory_reserved = 0
    order.state = "canceled"
    order.canceled_reason = reason
    ctx.db.flush()
    return {"canceled": True, "cancel_blocked": None}


@service_task("svc_reserve_inventory")
def svc_reserve_inventory(ctx: ServiceTaskContext) -> dict:
    order = _order(ctx)
    if order is None:
        return {"reserved": False}
    if order.inventory_reserved:              # idempotent (§7.6: the tick re-drives)
        return {"reserved": True, "skipped": "already_reserved"}
    qty = int(ctx.get("qty", 1))
    item = ctx.db.get(InventoryItem, ctx.get("sku", "SKU-1"))
    if item is not None:
        item.reserved += qty
    order.inventory_reserved = qty
    order.state = "placed"
    ctx.db.flush()
    return {"reserved": True}


@service_task("svc_capture_payment")
def svc_capture_payment(ctx: ServiceTaskContext) -> dict:
    """NOT IN THE SPEC, and it cost a real debugging session here.

    §7.2 rule 4 says the webhook must "do the row update inline and treat the
    signal as best-effort" -- so by the time this handler runs, the webhook has
    ALREADY written `order.payment_status = 'paid'`. G10/G26 say a handler must
    short-circuit on its object's current state. Key the idempotency guard on
    `payment_status` -- the obvious column, and the one the trigger describes --
    and the handler skips its OWN remaining side effects (inventory release,
    receipt email) on the very first run. The workflow completes, the order says
    paid, and the reserved stock is never released: the phantom oversell §6.4.3
    exists to prevent, arrived at by following two spec rules correctly.

    The guard must therefore key on THIS handler's own output (`state`), never
    on a field an upstream writer also owns.
    """
    order = _order(ctx)
    if order is None:
        return {"paid": False}
    if order.state == "paid":
        return {"paid": True, "skipped": "already_captured"}
    item = ctx.db.get(InventoryItem, ctx.get("sku", "SKU-1"))
    if item is not None and order.inventory_reserved:
        item.reserved = max(0, item.reserved - order.inventory_reserved)
        item.on_hand = max(0, item.on_hand - order.inventory_reserved)
        order.inventory_reserved = 0
    order.payment_status = "paid"
    order.state = "paid"
    ctx.db.add(
        EmailMessage(to_user_id=order.buyer_id, template="order_paid",
                     payload={"order_id": order.id})
    )
    ctx.db.flush()
    return {"paid": True}


@service_task("svc_handle_payment_failed")
def svc_handle_payment_failed(ctx: ServiceTaskContext) -> dict:
    """G26 in its original form: `payment_failed` used to route straight to
    'cancel the order and release its inventory'. A failed attempt is
    retryable, so by the time a re-run reaches here the buyer may have paid
    with another card. Route through the same guard."""
    return _release_and_cancel(ctx, "payment_failed")


@service_task("svc_release_and_cancel")
def svc_release_and_cancel(ctx: ServiceTaskContext) -> dict:
    return _release_and_cancel(ctx, "admin_canceled")


@service_task("svc_timeout_release_and_cancel")
def svc_timeout_release_and_cancel(ctx: ServiceTaskContext) -> dict:
    return _release_and_cancel(ctx, "abandoned_checkout")
