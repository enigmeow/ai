# Stripe — an integration spec

> **Status:** distilled from a shipped integration running in production —
> hosted checkout, card payments, refunds, disputes, subscriptions and Connect.
> **Scope:** how to talk to Stripe correctly. The provider-agnostic reasoning
> (three modes, the fake as production code, config precedence, the adapter
> factory) lives in [`docs/storefront.md`](storefront.md) §2 and is not repeated
> here.
>
> ### Read this if
>
> You are wiring up card payments and want the parts that are not in the quick-
> start: what the objects actually are and how they relate (§2), the call
> sequence for each flow (§3), **which webhook events to handle and what to do
> with each** (§4), **which errors are worth retrying** (§5), and idempotency
> done properly (§6).
>
> §5 and §6 are the two that cost real money if you get them wrong, and neither
> is in the quick-start.

---

## 1. The two things to internalise first

**1. Stripe is the source of truth, and it tells you asynchronously.** Your
`payment_status` column is a cache of Stripe's opinion, arriving by webhook,
possibly out of order, possibly twice, possibly minutes late. Any design that
treats the synchronous API response as final will be wrong for some fraction of
real traffic — the 3-D Secure fraction, which is large and growing.

**2. A PaymentIntent is a long-lived, retryable object, not a single attempt.**
This is the most consequential misunderstanding. One intent can be attempted
many times: declined, authenticated, declined again, then succeed. It stays
alive in `requires_payment_method` between attempts. Treating a failed *attempt*
as a dead *order* is the single most expensive modelling error available here
(§4.2, §7).

---

## 2. The object model

```
Customer ─┐
          ├── CheckoutSession (cs_…)   hosted page; expires (24h default)
          │        └── creates ──▶ PaymentIntent (pi_…)
          │                              │  one intent, MANY attempts
          │                              ├── Charge (ch_…)  ← the successful attempt
          │                              │      └── BalanceTransaction (txn_…)
          │                              │             fee, net, available_on
          │                              ├── Refund (re_…)  ← many per charge
          │                              └── Dispute (dp_…)
          └── Subscription (sub_…) ──▶ Invoice (in_…) ──▶ PaymentIntent
```

What each is *for*:

| Object | Holds | You need it because |
|---|---|---|
| **PaymentIntent** | Amount, currency, status, `last_payment_error` | The lifecycle anchor. Correlate everything to it. |
| **Charge** | Card metadata, risk signals, 3DS outcome, receipt URL | Everything you want to *display* or investigate |
| **BalanceTransaction** | `fee`, `net`, `available_on` | Reconciliation. Without it you cannot tie a payout to an order |
| **CheckoutSession** | Hosted URL, expiry, its own completion event | Only if Stripe hosts the page |
| **Refund** | Amount, reason, status | Many per charge — see §6 on keying them |
| **Dispute** | Reason, evidence deadline, funds status | Money can leave *after* you shipped |

**Intent status values**, and what each means for you:

| Status | Meaning | Your move |
|---|---|---|
| `requires_payment_method` | No method, or the **last attempt failed** | Wait. The buyer can retry. **Not terminal.** |
| `requires_confirmation` | Method attached, not submitted | Wait |
| `requires_action` | **3-D Secure challenge pending** | Buyer is at their bank. Wait — this can take minutes |
| `processing` | Submitted, not settled | Wait |
| `succeeded` | Money captured | Fulfil |
| `canceled` | **Terminal** — you cancelled it, or Stripe expired it | Release inventory |

Only two of those six are terminal. Design for the other four.

---

## 3. Call sequences

### 3.1 Hosted checkout (recommended)

```
1. create Customer (or find existing by email)
2. checkout.sessions.create(..., expand=["payment_intent"])
       → hosted URL  +  (usually) pi_…
   store: session id, intent id if present, hosted URL
3. redirect the buyer
4. buyer pays on Stripe's page; 3DS handled there
5. WEBHOOK checkout.session.completed  → session is done
   WEBHOOK payment_intent.succeeded    → money captured  ← fulfil on THIS
6. retrieve_intent(expand=["latest_charge.balance_transaction"])
       → card metadata, risk, fee split
```

**Fulfil on `payment_intent.succeeded`, not on the redirect back to your success
URL.** The buyer can close the tab; the redirect is a UI convenience, not a
payment guarantee.

**The correlation trap.** At step 2 the PaymentIntent may not exist yet, so you
store the **session** id and rewrite it to the intent id when one materialises.
Every lookup must therefore try **both** `cs_…` and `pi_…`. An expired session is
exactly the case where an intent may never have existed — so the lookup that
matters most is the one most likely to miss.

### 3.2 Your own form (Payment Elements)

```
1. payment_intents.create(amount, currency, automatic_payment_methods)
       → pi_… + client_secret        (client_secret goes to the browser)
2. browser confirms with the card; Stripe may return requires_action
3. browser completes the 3DS challenge
4. WEBHOOK payment_intent.succeeded  ← fulfil here, not on the browser's word
```

More control, more surface: you own the 3DS round-trip and the retry UX.

### 3.3 Refund

```
refunds.create(payment_intent=pi_…, amount=…, reason=…,
               idempotency_key=<your refund-request id>)   ← §6
   → WEBHOOK charge.refunded
```

Omit `amount` for a full refund. On a Connect destination charge, pass
`refund_application_fee=True` to reverse the platform fee proportionally, or you
refund the buyer in full while keeping your cut.

---

## 4. The webhook event catalog

Subscribe deliberately. This is the surface the reference implementation handles,
what each event means, and what to do:

### 4.1 Payments

| Event | Means | Do |
|---|---|---|
| `payment_intent.succeeded` | **Money captured** | Fulfil. Set `paid_at`. Send the receipt. Pull the balance transaction for fees |
| `payment_intent.payment_failed` | **One attempt** declined | Record it. **Do not cancel the order** — the intent lives on (§7) |
| `payment_intent.canceled` | **Terminal.** You cancelled, or it expired | Release inventory, cancel the order |
| `charge.succeeded` | The successful attempt's detail | Persist card metadata, risk signals, 3DS result |
| `charge.updated` | Detail changed (often the balance transaction landing) | Refresh the fee split |
| `charge.refunded` | A refund settled | Set `refunded` / `partially_refunded` |

### 4.2 Checkout sessions

| Event | Means | Do |
|---|---|---|
| `checkout.session.completed` | Buyer finished the hosted page | Bind the now-known `pi_…` to your row |
| `checkout.session.expired` | **The buyer is gone** | *This* is your abandonment signal — release inventory. Do not use a local timer shorter than the session's own 24h expiry |

### 4.3 Disputes — money can leave after you shipped

| Event | Do |
|---|---|
| `charge.dispute.created` | Mark disputed. Start the evidence clock. **Do not refund** — you cannot refund a disputed charge |
| `charge.dispute.funds_withdrawn` | Funds are gone pending resolution |
| `charge.dispute.closed` | Won → funds reinstated. Lost → permanently gone |

### 4.4 Connect and transfers

| Event | Do |
|---|---|
| `account.updated` | Refresh capabilities/requirements. This is the seller-onboarding heartbeat |
| `capability.updated` | A specific capability changed state |
| `account.application.deauthorized` | Seller disconnected. Stop paying out |
| `transfer.created` / `.updated` / `.reversed` | Track payout state |

### 4.5 Subscriptions

`customer.subscription.created` / `.updated` / `.deleted`, `invoice.paid`,
`invoice.payment_succeeded`, `invoice.payment_failed`. Note subscription invoices
carry **no order-level intent** in your schema — branch on them *before* your
order lookup, or every one logs a spurious "unknown order".

### 4.6 Handling rules

- **Verify against the RAW body.** A framework that parsed and re-serialised the
  JSON produces different bytes and every signature fails. The most common
  integration bug there is.
- **Return 200 once verified**, even if your processing failed. Stripe retries
  5xx with backoff and eventually disables the endpoint — a bug in one handler
  becomes an outage of *all* webhooks.
- **Expect duplicates and reordering.** `payment_intent.succeeded` can arrive
  before the `checkout.session.completed` that created the row it references.
- **Map events to signals in one static dict**, so "what does Stripe tell us and
  what do we do" is one table.
- **The response object is dict-*like* but not safe under `.get()`.** Access with
  `[]` inside `try/except KeyError`. `.get()` returns `None` where you expected a
  `KeyError` and the bug surfaces three layers away.

---

## 5. Error taxonomy — which failures are worth retrying

**This is the section most integrations lack, and it decides whether your retry
logic is safe.** A retry protocol needs a rule; without one it either hammers a
permanently-dead operation forever or gives up on a recoverable blip.

```python
_PERMANENT = {
    "authentication_error",        # your API key is wrong. Retrying cannot help
    "invalid_request_error",       # your parameters are wrong. Ditto
    "charge_already_refunded",
    "charge_disputed",             # cannot refund a disputed charge
    "charge_expired_for_refund",
    "insufficient_funds",          # platform balance cannot cover it
}

def classify(exc) -> tuple[bool, str, str | None]:
    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    if code in _PERMANENT:
        return True, str(exc), code          # permanent → fail the branch
    return False, str(exc), code             # transient → retry with backoff
```

**Default to transient.** Network errors, 5xx, rate limits and timeouts are all
recoverable, and they are the common case. The permanent list is short and
enumerable; everything else retries.

Two traps:

- **`insufficient_funds` is permanent *here* but means something different on a
  card decline.** As a platform-balance error it will not fix itself. As a card
  decline it is the buyer's problem and the intent stays alive. Same string, two
  meanings, depending on which call raised it.
- **A card decline is not an exception at all** in the hosted flow. It arrives as
  `payment_intent.payment_failed` on the webhook, not as a raised error from your
  API call. Your `try/except` will never see it.

---

## 6. Idempotency

Every mutating call carries a key. Stripe dedups against it for **24 hours** and
returns the *original* result rather than acting again. This is the only thing
standing between a retry and a double charge.

```python
def idempotency_key(*, site, object_id, op, attempt=1) -> str:
    return f"{site}:{object_id}:{op}:{attempt}"      # max 255 chars
```

Four properties, each load-bearing:

- **Deterministic.** Any random component disables deduplication completely.
- **Scoped by site/tenant** — two products sharing one Stripe account must not
  collide on a shared object id.
- **Scoped by operation** — `capture` and `refund` on one intent differ.
- **An explicit `attempt`**, which distinguishes two things that look identical
  from inside a retry loop:

| Situation | Key | Why |
|---|---|---|
| Network blip, 5xx, workflow loop-back | **same** | You want Stripe to dedup. Acting twice is the bug |
| Previous attempt failed *permanently*; buyer retries with another card | **new** | You genuinely want a second Stripe object |

**Never feed a transient-retry counter into the key.** Retry protocols increment
on network failure — precisely when the key must stay the same.

**Key refunds on your own request identity, not the amount.** Two legitimate
partial refunds *of the same amount* otherwise share a key: Stripe dedups the
second into the first and the buyer receives half their money.

> **How this fails in practice.** The reference implementation had a correct
> deterministic helper **used nowhere**, beside a live one appending
> `uuid.uuid4().hex[:8]` — with a docstring claiming retries returned the same
> result. Nothing ever dedup'd. Because the refund handler sat on a retry
> loop-back, a transient 5xx would have issued a **second real refund**.
>
> Two lessons. **A dead correct implementation beside a live incorrect one is
> worse than none**, because the correct one satisfies review. And **no test
> catches this by calling the function once** — assert that two *separately
> constructed* adapters produce the same key.

---

## 7. The retry-after-failure problem

The single most important behavioural fact, and it deserves its own section
because it has bitten this integration twice.

**A buyer whose card is declined can retry on the same PaymentIntent, and often
succeeds.** Stripe emits `payment_intent.payment_failed` per *attempt* and leaves
the intent in `requires_payment_method`. Minutes later it may emit
`payment_intent.succeeded` for the **same intent id**.

So:

- **`payment_failed` must not cancel the order.** If it does, you cancel orders
  that are about to be paid.
- **Even a hard decline does not kill the intent.** `stolen_card`, `lost_card`,
  `pickup_card`, `revocation_of_authorization` kill the **card**, not the
  **order** — Stripe returns the intent to `requires_payment_method` so the
  buyer can use a different one. **No decline code makes an intent terminal.**
- **Your cancel path must be reversible.** If anything did cancel on
  `payment_failed`, the later `succeeded` must restore the order rather than
  leave it captured-but-canceled. The reference implementation does this
  explicitly, writing a `state_recovered_from_canceled` audit event:

  ```python
  if order.state == "canceled":          # a prior payment_failed cancelled it
      record_event("state_recovered_from_canceled")
      order.state = "placed"             # the retry cleared
  ```

- **Handlers must re-derive safety from the order, never the trigger.** By the
  time a handler runs — after a retry, a re-driven workflow, a sweep — the order
  may have moved on. Guard destructive work on current state:

  ```python
  if order.payment_status in PAID:     return "already_paid"
  if order.fulfillment_status in SHIPPED: return "already_shipped"
  ```

> **What this cost, concretely.** Two orders took a declined attempt at T+7min
> and a successful retry ~3 minutes later on the *same* intent. The inline
> webhook path recovered the order correctly — both are captured and shipped
> today. But a workflow had already been routed to cancel and had errored, and
> an errored workflow silently drops every later signal, so the workflow never
> saw the success. **The order data self-healed; the workflow did not, and
> nothing reported the divergence for three months.** See `docs/bpm.md` G25.

---

## 8. Connect (multi-party), briefly

**Destination charges** are the simplest split: `transfer_data.destination` plus
`application_fee_amount` on the intent, and Stripe settles the seller's share
automatically. A later refund with `refund_application_fee=True` reverses your
fee proportionally.

**Onboarding is a lifecycle, not a status column** — account created →
requirements outstanding → capabilities enabled → possibly deauthorised — driven
by `account.updated`. Model it as a workflow.

**Transfers and reversals need their own idempotency keys** for the same reasons
as §6, and they are the calls most likely to be retried by hand.

---

## 9. Setup and testing

**Credentials.** Publishable (`pk_`) is public and belongs in the browser. Secret
(`sk_`) never leaves the server. The **webhook signing secret (`whsec_`) is
per-endpoint** — a different one for local, staging and production. A mismatched
`whsec_` fails every signature, which looks exactly like an attack.

**Register the endpoint and choose events explicitly.** Subscribing to everything
buries the ones that matter. Anything in §4 you do not handle should not be
subscribed. And an event you *do* rely on but forgot to enable is a silent
feature failure — `checkout.session.expired` in particular is off by default in
most people's setups, so abandonment never fires.

**Local development.** Point the SDK's base URL at Stripe's API mock in a
container and the whole flow runs offline against real HTTP semantics. Construct
a **client per adapter instance** rather than setting the module-level
`stripe.api_key`, so credentials cannot leak between tenants in one process or
between tests in one run — and so the base-URL redirection is possible at all.

**Use `expand` to avoid N+1.** `expand=["latest_charge.balance_transaction"]`
returns intent, charge, card metadata, risk signals and the fee split in one
call. Without it that is three round-trips you then have to reconcile.

**Test cards worth knowing:** the always-succeeds number, the always-declines
number, and — most importantly — **the one that forces 3-D Secure**. The
authentication path is where the interesting bugs are, and it is the path a happy
manual test never exercises.

**What you cannot test locally:** real card networks, real 3DS challenges, real
dispute flows, real payout timing. Use Stripe's test mode against the real API
for those, which is what `storefront.md` §2.1's third mode exists for.

---

## 10. Gotchas

**T1. A failed attempt is not a failed order** (§7). No decline code makes an
intent terminal.

**T2. Fulfil on `payment_intent.succeeded`, not the success-URL redirect.** The
buyer can close the tab.

**T3. Verify webhooks against the raw body**, before any parsing.

**T4. Return 200 once verified**, whatever your handler did. 5xx gets your
endpoint disabled.

**T5. Correlate on both `cs_` and `pi_`.** The intent may not exist when the
session is created, and an expired session may never have had one.

**T6. Idempotency keys must be deterministic** (§6). A random component disables
deduplication and turns a retry into a second charge.

**T7. Never feed a transient-retry counter into an idempotency key.**

**T8. Key refunds on your own request id, not the amount** — or two identical
partial refunds collide and the buyer is refunded once.

**T9. Default unknown errors to transient** (§5). The permanent list is short and
enumerable.

**T10. A card decline is not an exception** in the hosted flow — it is a webhook.
Your `try/except` will never see it.

**T11. Your abandonment timer is probably shorter than the session's 24h
expiry.** Drive abandonment from `checkout.session.expired`; keep a local timer
only as a backstop *beyond* the session lifetime.

**T12. You cannot refund a disputed charge.** Handle the dispute instead.

**T13. Response objects are dict-like but not `.get()`-safe.** Use `[]` with
`try/except KeyError`.

**T14. Branch subscription events before your order lookup** — they carry no
order-level intent.

**T15. `whsec_` is per-endpoint.** A mismatch fails every signature and looks
like an attack.

**T16. Never set the module-level `stripe.api_key`.** One client per adapter
instance, or credentials leak between tenants and between tests.
