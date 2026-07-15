# ANSWERS.md — Artikate Studio Backend Assessment

## Section 1 — Incident Investigation Log

**Symptom**: `/api/orders/summary/` times out (30s+) for tenants with 200+
orders. No code change was made to the view itself; the regression
appeared after a routine deployment.

**Step 1 — Ruled out the obvious first: was the view actually unchanged?**
Checked git history for the file — confirmed no changes to `orders/views.py`
in the deploy in question. This ruled out a direct logic bug in the view
and pointed toward something the view depends on changing underneath it.

**Step 2 — Checked what *did* change in that deployment.**
The deployment introduced a new `OrderItem` model with a ForeignKey to
`Order`. This is the kind of change that's easy to dismiss as "just a new
table" but is exactly the type of change that breaks an existing view
silently: if any code touches a new relation inside a loop, it doesn't
throw an error, it just adds queries — and the failure mode is a slow
timeout, not a crash, which makes it dangerous.

**Step 3 — Formed a hypothesis before touching the debugger.**
Hypothesis: the summary view iterates over `Order.objects.all()` and, for
each order, separately queries into the new `OrderItem` relation (e.g.
`order.items.count()` and `order.items.all()`). If true, query count would
scale linearly with order count — which matches the reported symptom
exactly (only tenants with 200+ orders are affected; smaller tenants
wouldn't notice).

**Step 4 — Verified with django-silk rather than guessing.**
Installed and wired up django-silk, hit the endpoint via curl with a
seeded tenant (250 orders), and inspected the request detail page.

    Result: 501 queries, 994ms total, 186ms spent purely on database queries.

This matches the hypothesis almost exactly: 1 base query for the orders
themselves, plus 2 queries per order (one `.count()`, one `.all()` on the
`items` relation) — 1 + (250 × 2) = 501.

**Root cause category**: N+1 query problem. Specifically, the view accesses
a related model (`OrderItem`) inside a per-row loop without using
`prefetch_related`, so Django issues one additional query per related
lookup per row instead of batching them into a single query up front.

**Why this specific bug wasn't caught pre-deployment**: it only manifests
as a timeout at higher row counts — a tenant with 5 orders (like the test
tenant used in initial QA) would see 11 queries, imperceptibly fast. The
regression is a function of data volume, not code correctness, which is
why it passed review and testing but failed in production for larger
tenants.


### Section 2 — Worker SIGKILL Behavior

Section 2 — Rate-Limited Async Job Queue

Why Celery + Redis (not Django-Q or custom)

I chose Celery with a Redis broker. Redis was already needed for the rate
limiter, so using it as the Celery broker too meant no extra infrastructure.
Celery's retry and crash-recovery tools (acks_late, reject_on_worker_lost)
are mature and battle-tested — building that myself would mean re-solving a
problem Celery already solves well. Django-Q is lighter weight, but its
crash-recovery story is less proven, and this task specifically needs strong
guarantees around "worker crashes mid-job."

Why a sliding window rate limiter (not token bucket or fixed window)

I used a Redis sorted set with ZREMRANGEBYSCORE (sliding window log).


A fixed window (e.g. reset every 60 seconds) has a boundary problem:
200 requests right before the window resets + 200 right after means 400
in a couple seconds, even though each window looked fine on its own. Given
the brief's flash-sale burst scenario, this felt risky.
A token bucket avoids that problem and is cheaper, but a sliding
window gives an exact count of requests in the last N seconds with no
approximation, which felt safer for a hard third-party limit.


How it works: each allowed request adds an entry to a Redis sorted set,
scored by timestamp. To check if a new request is allowed, a single Lua
script: drops entries older than the window, counts what's left, and adds
the new entry only if under the limit. All of this runs as one atomic Redis
operation — no separate "check, then write" steps that could race under
concurrent workers.

Redis failure: the limiter fails open — if Redis is unreachable, it
lets the email through rather than blocking it. Losing a handful of
transactional emails (OTPs, order confirmations) because Redis was
temporarily down felt worse than briefly exceeding the provider's rate
limit, which is a soft cost (maybe a 429), not a lost message.

Retry and dead-letter handling

Failed sends retry with exponential backoff (2s, 4s, 8s, 16s, 32s). After 5
real failures, the job is moved to a DeadLetterJob table instead of being
dropped, so it can be inspected or manually retried later (there's a
"Requeue" action wired up in Django admin for this).

One bug I hit while testing: I originally used Celery's built-in retry
counter to decide when a job had failed "too many times." But that counter
also increments every time the rate limiter says "not your turn yet" —
which happens constantly and is completely normal under burst load. This
meant jobs could get wrongly dead-lettered just for waiting in line, not
for actually failing. Fix: I track real send failures separately from
rate-limit waits, so only genuine provider failures count toward the
dead-letter threshold.

What happens if a worker is SIGKILL'd mid-task?

By default, Celery acknowledges a task the moment a worker picks it up —
before running it. So if the worker is killed while the task is running,
the task is already marked "done" and is lost forever.

I set acks_late=True, which delays the acknowledgement until after the
task finishes successfully. If the worker dies mid-task, the task was never
acknowledged, so Redis makes it available again for another worker to pick
up. I also set task_reject_on_worker_lost=True, which explicitly requeues
a task if Celery detects the worker process itself has died. And
worker_prefetch_multiplier=1 means a worker only ever holds the one task
it's actively running — without this, a killed worker could have several
unacknowledged tasks in flight, and all of them would get redelivered and
re-run, multiplying side effects instead of just retrying the one
interrupted job.

Trade-off: this means a task can run more than once — if the worker
dies after sending the email but before the acknowledgement goes
through, the job gets redelivered and the email could be sent twice. This
is Celery's "at-least-once" guarantee, not "exactly-once." My task isn't
currently idempotent against this specific edge case — a more complete fix
would check EmailSendRecord for an existing "sent" row before calling the
provider again. I'm noting this as a known gap rather than assuming it away.

Testing

jobs/test_queue.py submits 500 jobs and checks three things:


No job is lost — every job ends up with a final sent status (none
silently disappear).
The rate limit is never exceeded — I check every point in time
against how many requests were allowed in the trailing window.
A forced failure is retried and recovers — one job is set up to fail
its first send attempt, then succeed; the test confirms it retried and
ended up sent, not dead-lettered.


Note: the test runs against a scaled-down limit (20 requests / 3 seconds)
instead of the real 200/60, purely so the test finishes in a reasonable
time. It's the same code path and same atomicity guarantees, just a smaller
window.

## Section 3 — Multi-Tenant Data Isolation

### Approach

Automatic tenant scoping is enforced through three pieces working together:

1. **`TenantMiddleware`** (`tenants/middleware.py`) — runs on every incoming
   request, before any view code executes. It resolves the current tenant
   from either a subdomain (e.g. `acme.example.com` → `acme`) or an
   `X-Tenant-Slug` header (used for local development, since subdomains
   aren't practical to test against `127.0.0.1`). It binds the resolved
   tenant to thread-local storage for the lifetime of the request, and
   clears it in a `finally` block so it can never leak into a later request
   that happens to reuse the same worker thread — even if the view raises
   an exception.

2. **Thread-local context** (`tenants/context.py`) — a small wrapper around
   Python's `threading.local()`, exposing `set_current_tenant()`,
   `get_current_tenant()`, and `clear_current_tenant()`.

3. **`TenantManager`** (`orders/models.py`) — overrides `get_queryset()` on
   Django's base `Manager`. Every call through `Order.objects` — whether
   `.all()`, `.filter()`, `.get()`, anything — is automatically intersected
   with `.filter(tenant=current_tenant)` before it ever reaches the
   database. A developer calling `Order.objects.all()` with zero awareness
   of tenancy still only ever sees their own tenant's rows, because the
   filtering happens one layer beneath their code, not inside it.

   If no tenant is set in context (middleware never ran — e.g. a management
   command or a bug), the manager returns `qs.none()`: zero rows, rather
   than all rows across every tenant. This is a deliberate fail-closed
   default — the safer failure mode for a system where a leak means one
   customer's data appears inside another customer's account.

   A second manager, `all_objects = models.Manager()`, is kept as an
   explicit, clearly-named escape hatch for legitimate cross-tenant needs
   (Django admin, data migrations, internal scripts). Its unusual name is
   intentional — it should never be reached for by accident.

### Tests

`orders/test_tenant_isolation.py` proves, among other things:
- Tenant A's queryset never contains Tenant B's orders, even by direct
  primary-key lookup (`.filter(id=other_tenants_order.id)` returns nothing)
- `.objects.all()` specifically — the call site with the least visual
  indication that scoping is happening — cannot return cross-tenant rows
- With no tenant context set at all, `.objects.all()` returns zero rows,
  not everything
- `all_objects` still provides full cross-tenant access when explicitly
  requested, confirming the escape hatch works as designed
- Switching the tenant in context between calls changes the visible data,
  confirming the manager reads live context per-query rather than caching
  a queryset built under a stale tenant

### Failure modes of thread-local tenant scoping under async views

`threading.local()` binds state to the specific OS thread executing the
code. Django's traditional WSGI request/thread model works safely with
this, because one request is handled start-to-finish by exactly one
thread. Under ASGI and `async def` views, that guarantee disappears: a
coroutine can suspend at an `await` and resume execution on a *different*
thread from the async event loop's thread pool. If the tenant was set on
thread 1 but the coroutine resumes on thread 3, `get_current_tenant()` on
thread 3 either returns `None` (silently un-scoping every subsequent
query) or, worse, returns a *stale* tenant left behind by a previous,
unrelated request that happened to run earlier on that same thread. Both
failure modes are silent — no exception, no warning — the query simply
executes with the wrong scope.

The correct primitive for this is `contextvars.ContextVar`. Unlike
thread-locals, a `ContextVar`'s value is automatically copied into the
context of each new asyncio task at creation time, and asyncio explicitly
propagates that context across `await` boundaries within the same logical
task — so the value correctly follows the request's logical flow of
execution regardless of which physical thread happens to resume it.

The concrete change: replace the `threading.local()` object in
`tenants/context.py` with:

    tenant_var: contextvars.ContextVar = contextvars.ContextVar(
        'current_tenant', default=None
    )

and update `get_current_tenant`/`set_current_tenant` to call
`tenant_var.get()` / `tenant_var.set()` instead of touching the
thread-local object. This is a drop-in replacement at the storage layer —
neither `TenantMiddleware` nor `TenantManager` need to change, since they
only interact with the two functions, not the storage mechanism directly.