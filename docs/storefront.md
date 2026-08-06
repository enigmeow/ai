# Storefront — third-party integration as a domain spec

> **Status:** a spec distilled from a shipped storefront: catalogue, cart,
> checkout, payments, refunds, shipping, tax and transactional email, running in
> production against Stripe.
> **Stack of the reference implementation:** FastAPI + SQLAlchemy + MySQL,
> Stripe via the official SDK, workflow engine underneath every lifecycle.
>
> ### What this document is
>
> Not "how to build a shop". Catalogue and cart are well-trodden, and the code
> is the least interesting part. This is about the problem that actually makes
> commerce hard to build and harder to keep working:
>
> **Your system's correctness depends on third parties you cannot run, cannot
> pause, and cannot fully simulate — and the failure modes only appear in
> production, with real money.**
>
> §2 is the answer: a three-mode adapter layer where **the fake is the first
> stage of the production pipeline, not a test double**. §4 is the schema
> consequence: store the processor's own vocabulary rather than abstracting it.
> §5 is the money-movement model that stops you overselling.
>
> Read §2 even if you are not building commerce — the same shape applies to any
> product that depends on an external API for correctness: shipping, identity
> verification, e-signature, SMS, ledgers.
>
> **Companions:** [`docs/bpm.md`](bpm.md) — every lifecycle here (order, payment,
> refund, shipment, dispute) is a BPMN workflow, and §7.2's webhook rules live
> there. [`docs/project-management.md`](project-management.md) §5 — the money
> model for services rather than goods.

---

## 1. The shape of the problem

A storefront has more integration surface than domain logic. The reference
implementation's own breakdown:

| | Lines |
|---|---|
| Adapter layer + config + mode | ~540 |
| Four adapter families (payments, shipping, tax, email) | ~2,400 |
| Models | ~1,400 |
| Routers | the rest |

That ratio is the point. **Most of the work is not "what is a cart", it is
"what happens when Stripe is slow, or the webhook arrives twice, or the card
needs 3-D Secure, or the buyer walks away mid-checkout".**

Three properties make it unusually unforgiving:

1. **Money is not idempotent by default.** Double-charging is a refund, an
   apology and possibly a chargeback. Double-shipping is a loss.
2. **The truth lives elsewhere.** Your `payment_status` column is a cache of
   Stripe's opinion, arriving asynchronously, out of order, sometimes twice.
3. **You cannot test the interesting paths locally** without either real
   credentials or a very good fake.

---

## 2. The adapter layer

### 2.1 Three modes, not two

The usual split is "mocked in tests, real in production", which leaves the most
dangerous configuration — *real integration, fake money* — untested. Use three:

| Mode | Meaning |
|---|---|
| **`fake`** | In-process fakes. No network, no credentials. The default. |
| **`test`** | The real adapter, real HTTP, the provider's **test** credentials. |
| **`live`** | The real adapter, real credentials, real money. |

`test` is the one that earns its place: it exercises the real SDK, the real
serialization, the real webhook signatures and the real error taxonomy, and it
is the only mode that catches "our adapter passes the wrong field name". A fake
cannot find that, and you do not want production to.

Local development points `test` at the provider's own API mock in a container,
so the whole checkout path runs offline against real HTTP semantics.

### 2.2 The fake is production code

**This is the load-bearing idea.** A fake that only exists in tests rots, and
diverges, and lies to you. Give it a real job instead:

> The fake email adapter **writes every message to an `email_messages` table**,
> which backs a real admin inbox at `/admin/emails` — with a sandboxed-iframe
> preview and a resend button.

Consequences, all good:

- Local development has a **working mail client** with zero credentials. You can
  see the order confirmation, click the link in it, check the layout.
- The same table is the **production email log**, so the fake and the real
  adapter share an observability surface rather than one having none.
- The fake is exercised constantly by real use, so it cannot quietly diverge.
- Reviewing a template is a UI task, not a test-output-reading task.

Generalise it: **a fake that persists somewhere a human looks will stay honest.
A fake that returns a canned object will not.**

The same reasoning gives the fake payment adapter a scripted-failure sibling
(`testing.py` in each family) so a test can demand "decline the next charge"
without touching the fake's normal path.

### 2.3 Fakes must refuse to run in production

A mode switch is one environment variable away from catastrophe — a fake payment
adapter in production accepts every card and ships every order.

```python
def assert_not_production() -> None:
    if get_environment() == "production":
        raise RuntimeError(
            "Fake adapter refused to initialize: ENVIRONMENT=production. "
            "Fake adapters must never run in production.")
```

Called in each fake's `__init__`. Fail at construction, loudly, rather than
silently approving payments. **The guard belongs in the fake, not in the
factory** — anything that can construct a fake must hit it, including a test
helper or a console someone wrote later.

### 2.4 Per-adapter overrides, and why `auto` is not enough

Mode alone is too coarse. Real operations want mixtures: real emails while
payments stay fake during a launch rehearsal; fake shipping because the carrier
account is not open yet.

So each family carries its own override, resolved before mode:

```
storefront.payment.provider    auto | fake | real
storefront.shipping.provider   auto | fake | real
storefront.tax.provider        auto | fake | real
storefront.email.provider      auto | fake | real
```

`auto` follows the global mode; `fake`/`real` force it. Four families × three
values, resolved in one helper.

### 2.5 Config precedence: database first

```
1. DB-backed settings   storefront.*   ← admin panel, no redeploy
2. Environment          STOREFRONT_MODE, STRIPE_SECRET_KEY, …
3. Built-in defaults    fake, no keys
```

**Database first is deliberate.** Flipping a provider or rotating a key is an
operational act, often urgent, often out of hours, sometimes by someone without
deploy rights. Requiring a redeploy to stop sending real emails is how outages
get longer.

Two consequences to design for:

- **Secrets in the database** need the same care as secrets in env: never logged,
  never in an API response, write-only in the admin UI (show `sk_live_…••••`).
- **A missing key must fail closed and legibly.** The reference implementation
  503s the checkout endpoint with a message naming the setting to fix, rather
  than constructing an adapter that will fail later at the worst moment.

### 2.6 The factory is the only entry point

```python
adapter = get_payment_adapter(db=db, product_type="merch")
```

Routers never construct an adapter. That single choke point is where mode,
override, credentials, and — in this implementation — **product-type routing**
resolve. The reference needs the last one because some product categories cannot
use the mainstream processor at all, so payment routing is a function of *what
is in the cart*. In `fake` mode the routing is bypassed entirely: one fake
handles everything, because routing is the thing you are not testing.

---

## 3. Interface design across four families

Each family is `base.py` (ABC + DTOs) plus one module per implementation. What
generalises:

**Return DTOs, never provider objects.** A `stripe.PaymentIntent` leaking into a
router means the router now depends on Stripe. The adapter's job is to be the
only file that knows.

**Model the operations you need, not the provider's API.** `create_intent`,
`capture`, `refund`, `verify_webhook`. When a second provider arrives, the
interface should survive.

**Every family needs a stub for the provider you have not integrated yet.** The
reference ships an `easypost.py` shipping stub and a `stripe_tax.py` stub that
is deliberately *routed back to the fake at the factory* until the real thing is
registered. A stub that raises `NotImplementedError` at the factory is honest;
one that silently returns zeroes is a bug waiting for a launch.

**Rate the fake by fidelity, not simplicity.** The flat-rate shipping adapter
computes real zone-and-weight arithmetic. It is a "fake" only in that it does
not call a carrier — the numbers are real, so the cart total is real, so the UI
is testable.

---

## 4. Schema: store the provider's vocabulary

The instinct is to abstract: `payment.status ∈ (pending, paid, failed)`, provider
detail discarded. **Resist it.** The reference `Payment` row carries **49
columns**, most of them Stripe's own:

```
provider, provider_intent_id, provider_ref, charge_id, status,
amount_cents, currency,
card_brand, card_funding, card_country, card_exp_month, card_exp_year,
card_network, card_wallet, card_fingerprint,
risk_level, risk_score,
outcome_type, outcome_network_status, outcome_seller_message,
three_d_secure_used, three_d_secure_result,
receipt_url, receipt_number,
balance_transaction_id, stripe_fee_cents, net_cents,
application_fee_cents, funds_available_on, disputed,
display_metadata, last_provider_payload
```

Why this is right:

- **Support questions are provider-shaped.** "Why did this card fail?" is
  answered by `outcome_seller_message` and `outcome_network_status`, not by
  `status='failed'`.
- **Reconciliation needs the fee split.** `stripe_fee_cents` / `net_cents` /
  `funds_available_on` are what let you tie payouts to orders without exporting
  from a dashboard.
- **Fraud review needs the signals.** `risk_level`, `risk_score`,
  `card_fingerprint` (the same card across accounts), `three_d_secure_*`.
- **`last_provider_payload` is the escape hatch.** Keep the raw object. Every
  question you did not anticipate is answerable from it, and it costs a JSON
  column.

The abstraction still exists — it is `status`, and the adapter maps into it. But
**the abstraction is a projection over the detail, not a replacement for it.**
The cost is a wide table and a per-provider migration when you add one. That is
cheaper than being unable to answer a chargeback.

> **Corollary — reuse the adapter, not the schema.** When a second product
> needed online payment, it got its own `stripe_*` columns and its own webhook
> rather than reusing the order-bound `Payment` table. Different lifecycle,
> different reconciliation, different failure modes. Sharing the adapter is
> leverage; sharing the schema couples two products' release cycles.

### 4.1 The polymorphic cross-cutting table

For a concern that applies to *many* entity types — review, approval, transfer,
split — use **one table per concern with `(entity_type, entity_id)`**, not one
table per entity:

```
stripe_reviews
  entity_type VARCHAR(64), entity_id VARCHAR(36), content_hash, verdict, …
  INDEX (entity_type, entity_id, created_at)
  INDEX (entity_type, entity_id, content_hash)
```

Adding a reviewable entity becomes a new `entity_type` string rather than a new
table, a new model, a new migration and a new set of queries. The reference uses
this shape in five places.

You give up foreign-key integrity on `entity_id` — accept it deliberately, and
index the pair, because every query is "the rows for *this* entity".

---

## 4.5 The payment integration in detail

Generic advice about "adapters" stops being useful at some point. This is the
concrete shape of a card-payment integration, and the parts that are easy to get
wrong.

### 4.5.1 Idempotency keys are the whole safety mechanism

**Get this wrong and you take money twice.** Every mutating call carries a key;
the provider dedups against it for 24 hours and returns the *original* result
instead of acting again.

```python
def stripe_idempotency_key(*, site, object_id, op, attempt=1) -> str:
    return f"{site}:{object_id}:{op}:{attempt}"
```

Four properties, each load-bearing:

- **Deterministic.** The same logical operation on the same object must produce
  the same key. Any random component disables deduplication completely.
- **Scoped by site/tenant**, so two products sharing one provider account cannot
  collide on a shared object id.
- **Scoped by operation**, so `capture` and `refund` on one intent differ.
- **An explicit `attempt`**, which is the subtle one — see below.

**`attempt` distinguishes two things that look identical from inside a retry
loop:**

| Situation | Key | Why |
|---|---|---|
| Network blip, 5xx, workflow loop-back | **same** | You want the provider to dedup. Acting twice is the bug. |
| Previous attempt failed *permanently*, buyer retries with another card | **new** | You genuinely want a second provider object. |

A retry protocol (`docs/bpm.md` §5.7) increments a counter on **transient**
failure — so **do not feed that counter into the key.** Doing so re-enables
double-charging on exactly the path idempotency exists to protect.

**And key on your own request identity, not on the amount.** Two legitimate
partial refunds *of the same amount* will otherwise share a key: the provider
dedups the second into the first, and the buyer gets half their money. Pass the
id of *your* refund request.

> **A real bug, and how it hid.** The reference implementation had a correct
> deterministic helper **used nowhere**, beside a live one that appended
> `uuid.uuid4().hex[:8]` — with a docstring asserting that retries returned the
> same result. Every key was unique, so nothing ever dedup'd. Because the refund
> handler sits on a retry-protocol loop-back, a transient 5xx would have issued
> a **second real refund**, and the dedup guard downstream keyed off the
> provider's refund id — which a genuine duplicate carries a *new* value for.
>
> Two lessons. **A dead correct implementation beside a live incorrect one is
> worse than no implementation**, because the correct one satisfies review.
> And **no test can catch this by calling the function once** — you need two
> calls, or better, two separately-constructed adapters, and to assert they
> agree.

### 4.5.2 Intents versus hosted checkout

Two integration styles, and the choice drives everything downstream:

| | You host the form | Provider hosts checkout |
|---|---|---|
| Create | `payment_intents.create` | `checkout.sessions.create` |
| Card data | Never touches your server (tokenised client-side) | Never touches your server |
| 3-D Secure | You handle the `requires_action` round-trip | Provider handles it |
| Abandonment | Your problem | Session has its own expiry event |
| Correlation id | `pi_…` from the start | `cs_…` first, `pi_…` **later** |

That last row is a real trap. With hosted checkout the PaymentIntent may not
exist when the session is created, so your payment row stores the **session**
id and is rewritten to the intent id when the provider materialises one. **Any
lookup must try both**, or you miss precisely the events you care about — an
expired session is the case where the intent may never have existed at all.

Expand the intent at creation (`expand: ["payment_intent"]`) so you get it
inline where possible, and tolerate both shapes: expanded, it is an object;
unexpanded, a bare string.

### 4.5.3 One client per adapter instance, never a module global

```python
self._client = stripe.StripeClient(api_key=secret_key, base_addresses={...})
```

The SDK offers a module-level `stripe.api_key`. Don't use it. A per-instance
client means credentials cannot leak between sites in one process, or between
tests in one run — and it is what makes `base_addresses` redirection possible,
which is how the whole checkout path runs offline against a local API mock in
`test` mode.

### 4.5.4 Use `expand` to avoid N+1 round-trips

Fees and settlement live on the balance transaction, one hop past the charge:

```python
params={"expand": ["latest_charge.balance_transaction"]}
```

One call returns the intent, the charge, the card metadata, the risk signals and
the fee split — everything §4's wide `Payment` row wants. Without it you make
three calls and get to reconcile them yourself.

### 4.5.5 Webhook verification, and the object-access trap

```python
event = stripe.Webhook.construct_event(payload=raw_bytes, sig_header=sig,
                                       secret=self._webhook_secret)
```

Verify against the **raw body**. A framework that has already parsed and
re-serialised the JSON will produce a different byte string and every signature
will fail — this is the single most common webhook integration bug.

Then, a library detail worth stating because it is silent: the SDK's response
objects are dict-*like* through `[]`, but **not safe under `.get()` or attribute
access for missing keys**. Access with `[]` inside `try/except KeyError` for
optional paths. Reaching for `.get()` gets you `None` where you expected a
`KeyError`, and the bug surfaces three layers away.

Normalise into your own DTO at the boundary — `id`, `type`, the correlation id,
and the raw object — so nothing downstream depends on the provider's shape.

### 4.5.6 Refunds reverse more than the amount

A refund on a marketplace-style charge should also reverse the platform fee
proportionally, which the provider does *if you ask*. Refunds are also the most
common source of duplicate side effects, because they are the operation people
retry by hand when they are unsure whether the first one worked. Dedup on your
own refund row (`provider_refund_id`) **as well as** keying the API call
correctly — belt and braces, because these are the errors customers notice.

### 4.5.7 Connect / multi-party, if you need it

Destination charges (`transfer_data.destination` plus
`application_fee_amount`) settle the seller's share automatically, and a later
refund with `refund_application_fee=True` reverses proportionally. Onboarding is
its own lifecycle — account created, requirements outstanding, capabilities
enabled, deauthorised — driven by `account.updated` webhooks, and it deserves a
workflow rather than a status column. Transfers and reversals need their own
idempotency keys for the same reasons as §4.5.1.

---

## 5. Money movement and inventory

### 5.1 Three inventory numbers, not one

```
on_hand      physically present
reserved     held, not yet committed to an order
committed    allocated to an order that has not shipped
```

Sellable is `on_hand − reserved − committed`. **The overselling bug lives in the
gap between committing and releasing**: commit at checkout, and if the buyer
abandons, something must give it back. In the reference that release is a
workflow handler on the cancel path — which is exactly why an order workflow
that never reaches a terminal state holds stock forever (`docs/bpm.md` G23).

`allow_backorder` and `reorder_point` sit alongside, because "out of stock" is a
policy question, not an arithmetic one.

### 5.2 Payment is a lifecycle, not a boolean

Every money object gets a workflow: `order`, `payment_intent`, `refund`,
`refund_reversal`, `shipment`, `dispute`. The engine spec covers the mechanics;
three commerce-specific rules:

**A failed payment attempt is not a terminal event.** Providers emit
`payment_failed` per *declined attempt*, and the intent stays alive in
`requires_payment_method` — the buyer retries and usually succeeds. Treating
that signal as "cancel the order" cancels orders that are about to be paid.
Even a *hard* decline (`stolen_card`, `lost_card`, `pickup_card`) kills the
**card**, not the **order**.

**Abandonment is signalled by the provider, not by your clock.** A hosted
checkout session has its own expiry (24 hours by default). A local "cancel after
30 minutes" timer is shorter than the buyer's real window, so it cancels orders
while the payment page is still open and payable. Drive abandonment from the
provider's `session.expired` event; keep a local timer only as a backstop set
*beyond* the session lifetime.

**Handlers must re-derive safety from the order, never the trigger.** By the time
a handler runs — after a retry, a re-driven instance, a sweep — the order may
have moved on. Guard destructive work on current state:

```python
if order.payment_status in PAID_STATUSES:        return "already_paid"
if order.fulfillment_status in SHIPPED_STATUSES: return "already_shipped"
if order.state == "canceled":                    return "already_canceled"
```

Without that guard, a stale cancel signal releases the inventory of goods that
have already shipped.

### 5.3 Webhooks

Full rules in `docs/bpm.md` §7.2. Commerce specifics:

- **Verify the signature before anything else**, with the provider's own helper
  (HMAC + replay window). An unverified webhook is an unauthenticated write to
  your money tables.
- **Return 200 once verified**, even if your own processing failed. A provider
  that receives 500s retries with backoff and eventually disables the endpoint —
  so a bug in your handler becomes a permanent outage of *all* webhooks.
- **Map events to signals in one static dict.** One table to read when asking
  "what does the provider tell us, and what do we do about it".
- **Expect duplicates and reordering.** `succeeded` can arrive before the
  `checkout.session.completed` that created the record it refers to.
- **Correlate on more than one key.** An intent id may not exist yet when the
  session is created, so the row may hold a session id instead — resolve on
  both, or you will miss exactly the events that matter.

---

## 6. Transactional email

Email is where a storefront's quality is visible, and where it is usually worst.

- **Templates are code, in one module**, not strings scattered across routers.
- **Every send is logged to a table** (§2.2) — the fake writes there, and so does
  the real adapter. That table is your answer to "did the customer get it?"
- **Per-site identity** — from-address, reply-to, and display name resolved by a
  helper, so one codebase serving several brands cannot send a customer mail
  from the wrong one.
- **Sending is best-effort and must never roll back the write that triggered
  it.** Wrap it; log failures. Nobody's order should fail because a receipt
  didn't send.
- **Calendar attachments** (`.ics`) for anything with a time. Cheap, and it puts
  your event on the customer's calendar before a human has touched it.
- **Redaction** — a `redact.py` for anything that might carry personal data into
  logs or an admin surface.

**The idempotency trap.** Order confirmations are the classic double-send: the
inline path sends one, the workflow handler sends another. Use the log table as
the ledger — before sending, ask whether a message tagged with this
`(order_id, event)` already exists. **The email log is not just observability;
it is the deduplication key.**

---

## 7. Gotchas

**S1. A fake that only runs in tests will diverge.** Give it a production job —
persist it somewhere a human looks (§2.2). The email log is both the fake's
output and the real adapter's audit trail, so neither can rot unnoticed.

**S2. Fakes must refuse to construct in production**, in the fake's own
`__init__`, not in the factory. One environment variable is all that stands
between you and a shop that approves every card.

**S3. A stub that returns zeroes is worse than one that raises.** Route
unfinished integrations back to the fake at the factory, or fail loudly. Silent
plausible values reach production.

**S4. Store the provider's vocabulary** (§4). `status='failed'` cannot answer a
chargeback; `outcome_seller_message` can. Keep `last_provider_payload` for the
questions you have not thought of.

**S5. A failed payment attempt is retryable, not terminal** (§5.2). Modelling
`payment_failed` as "cancel the order" cancels orders that are about to succeed.
No decline code makes an intent terminal — a hard decline kills the card.

**S6. Your abandonment timer is probably shorter than the provider's session.**
Drive abandonment from the provider's expiry event; keep a local timer only as a
backstop beyond the session lifetime.

**S7. Re-derive safety from the object, never the trigger.** A signal describes
what was true when it was sent; a handler runs when it runs.

**S8. The email log is the deduplication key.** Two code paths will eventually
both send the confirmation. Check the ledger before sending.

**S9. Reuse the adapter, not the schema.** A second product wanting payments
gets its own columns and its own webhook. Sharing the adapter is leverage;
sharing the payment table couples release cycles.

**S10. Money is integer cents, everywhere, always.** No floats, no decimals at
the boundary. And name the unit in the column: `amount_cents`, not `amount`.

**S11. Secrets in the database need the same discipline as secrets in env.**
Never logged, never returned by an API, write-only in the admin UI.

**S12. Inventory is three numbers** (§5.1). One `quantity` column cannot express
"committed to an order that has not shipped", which is where overselling lives.

**S13. An idempotency key with any random component disables deduplication
entirely** (§4.5.1). The failure is not a stale cache — it is a second real
charge or refund. And no test catches it by calling the function once: assert
that two *separately constructed* adapters produce the same key.

**S14. A dead correct implementation beside a live incorrect one is worse than
no implementation.** The correct one satisfies review, so nobody looks at the
one actually wired up. If you write a helper, grep that it is used before you
consider it done.

**S15. Never feed a transient-retry counter into an idempotency key** (§4.5.1).
The retry protocol increments on network failures; that is exactly when the key
must stay the same. Only a *deliberate* re-do bumps it.

**S16. Verify webhooks against the RAW request body.** A framework that parses
and re-serialises the JSON changes the bytes, and every signature fails.

---

## 8. Build order

| Phase | Ships | Gate |
|---|---|---|
| **0** | Mode + config + factory + all four fakes | Whole checkout runs offline, no credentials |
| **1** | Catalogue, cart, inventory arithmetic | Sellable count is correct under concurrent carts |
| **2** | Checkout + order lifecycle as a workflow | Order reaches a terminal state on every path, including abandonment |
| **3** | Real payment adapter in `test` mode | Real SDK, real signatures, provider test credentials |
| **4** | Webhooks | A duplicate and an out-of-order event are both no-ops |
| **5** | Email + the log table + admin inbox | Confirmation sends exactly once under both code paths |
| **6** | Shipping + tax | Cart totals correct across zones |
| **7** | Refunds, disputes, reconciliation | A refund reverses the right fee split |
| **8** | `live` | — |

**Phase 0 before anything else.** Building the adapter layer after the routers
means every router already reaches for a provider SDK, and retrofitting the
choke point is a rewrite. It is a day's work up front and it is what makes every
later phase testable.

---

## 9. What to leave out

| Not built | Instead | Why |
|---|---|---|
| A payment abstraction over multiple processors | One real adapter + an interface | The second processor's differences are unknowable until you have one. Design the interface, implement once. |
| Provider tax integration | Per-jurisdiction flat rates behind the tax interface | Registering for tax collection is a legal project, not an engineering one. Keep the seam, defer the integration. |
| Carrier rate shopping | Flat rate by zone and weight | Real arithmetic, no account needed, correct totals from day one. |
| Subscriptions, marketplaces, split payouts | — | Each is its own domain. Adding them to a single-seller storefront early distorts the model. |
| A generic "payment method" abstraction | Cards, via one processor | Wallets already arrive through the same intent. Bank debits do not, and their lifecycle differs enough to model separately when it matters. |

The pattern, as elsewhere: **ship the 90% version of the feature and the 100%
version of the seam.** Tax rates can be approximate at first; the tax
*interface* cannot, because that is what you replace later without touching the
cart.
