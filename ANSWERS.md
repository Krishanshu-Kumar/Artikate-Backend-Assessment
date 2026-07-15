# ANSWERS.md — Artikate Studio Backend Assessment

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