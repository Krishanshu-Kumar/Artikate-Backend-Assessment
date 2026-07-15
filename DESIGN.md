# DESIGN.md — Section 2: Rate-Limited Async Job Queue

## The problem

Send transactional emails (order confirmations, OTPs, alerts) through a
third-party provider that allows 200 emails/minute. The system can receive
bursts of 2,000 requests in under 10 seconds during flash sales. The queue
must respect the rate limit, retry failed sends, and not lose jobs if a
worker crashes mid-run.

## Architecture choice: Celery + Redis|

**Decision: Celery + Redis.**

The rate limiter already requires Redis for its atomic operations, so using
Redis as the Celery broker too adds no new infrastructure. Celery's crash
recovery (`acks_late`, `task_reject_on_worker_lost`) is exactly built for
the "worker gets killed mid-task, don't lose the job" requirement in this
brief. Building a custom queue would mean re-implementing retry/backoff/
dead-lettering myself — solving a problem Celery has already solved, which
runs against the brief's own advice not to over-engineer.

## Rate limiter choice: sliding window (Redis sorted set)

Three options were on the table:

- **Option A — Token bucket (Redis DECR + TTL):** cheap, single key, avoids
  boundary bursts, but only gives an approximate window.
- **Option B — Sliding window (sorted set + ZREMRANGEBYSCORE):** exact count
  of requests in the trailing N seconds, slightly more Redis work per
  check (a small sorted set operation instead of a single DECR).
- **Option C — Fixed window (INCR + EXPIRE):** simplest, but has a real
  boundary problem — 200 requests right before a window resets plus 200
  right after means 400 in a couple of seconds, even though each window
  individually respected the limit.

**Decision: sliding window (Option B).**

Given the brief's explicit flash-sale burst scenario, the fixed-window
boundary problem is a real risk, not a theoretical one. A token bucket
avoids that but only approximates the window; a sliding window gives an
exact count with no approximation. That precision is worth the small extra
Redis cost here, because the constraint we're protecting against (the
provider's hard rate limit) is external and non-negotiable — approximate
enforcement risks real penalties from the provider.

### How it works

Each allowed request adds one entry to a Redis sorted set, scored by
timestamp. Checking whether a new request is allowed requires three logical
steps: drop entries older than the window, count what's left, and add the
new entry only if under the limit.

### Atomicity: Lua script

These three steps must happen as a single atomic unit. Redis's `MULTI`/
`EXEC` transactions can't work here because the final step (add the entry)
is *conditional* on the count read earlier in the same transaction —
Redis transactions can't branch on data read mid-transaction. A Lua script,
by contrast, runs its entire body as one atomic operation and can read,
decide, and write all within that single operation. This is what prevents
a race where two concurrent workers both read "199 requests so far" and
both proceed, pushing the true count to 201.

### Redis failure: fail open

If Redis is unreachable, the rate limiter **fails open** — it allows the
request through rather than blocking it. The reasoning: these are
transactional emails (OTPs, order confirmations), and losing them because
Redis had a brief outage is worse than the alternative risk, which is
briefly exceeding the provider's limit and getting a handful of 429s. A
fail-closed design would be safer against the rate limit itself, but at the
cost of blocking real user-facing communications during a Redis blip — a
trade-off I judged wasn't worth it for this use case. A payments or
authentication rate limiter would likely want the opposite choice
(fail closed), which is worth calling out explicitly rather than assuming
one fail-mode fits every use case.

## Retry and dead-letter handling

Failed sends retry with exponential backoff (2s, 4s, 8s, 16s, 32s, capped).
After 5 genuine failures, the job moves to a `DeadLetterJob` table rather
than being silently dropped, so it stays inspectable and can be manually
requeued (wired up as a Django admin action).

Retries caused by the rate limiter saying "not your turn yet" are tracked
separately from retries caused by actual send failures — conflating the
two would mean a job could get dead-lettered simply for having waited in
line during a burst, which isn't a failure at all.

## Crash safety: what happens if a worker is SIGKILL'd?

- **`acks_late=True`** — a task is only acknowledged after it finishes
  successfully, not the moment a worker picks it up. If the worker dies
  mid-task, the task was never acknowledged, so it becomes available again
  for another worker.
- **`task_reject_on_worker_lost=True`** — explicitly requeues a task if
  Celery detects the worker process itself has died, rather than leaving it
  in limbo.
- **`worker_prefetch_multiplier=1`** — a worker holds only the task it's
  actively running, not several prefetched ones. Without this, a killed
  worker could have multiple unacknowledged tasks in flight, all of which
  get redelivered and re-run — multiplying side effects rather than just
  retrying the one interrupted job.

**Trade-off:** this gives "at-least-once" delivery, not "exactly-once." A
task can run twice if the worker dies after sending the email but before
the acknowledgement completes. The current implementation doesn't fully
guard against this — a more complete version would check for an existing
`sent` record before calling the provider again. Noted here as a known gap
rather than assumed away.

## Testing

`jobs/test_queue.py` submits 500 jobs through the real task/retry/dead-letter
code path (using Celery's eager mode plus a small helper that plays the
role of a worker's redelivery loop, since `.apply()` doesn't auto-loop
through retries the way a live worker does) and asserts:

1. No job is lost — every job reaches a final `sent` or `dead_letter` state.
2. The rate limit is never exceeded, checked against the exact timestamp
   Redis used to enforce each request, not a client-side re-measurement.
3. A forced transient failure is retried and recovers successfully.

The test scales the rate limiter's window down (20 requests / 3 seconds
instead of the real 200/60) purely so the suite finishes in a reasonable
time — it exercises the identical Lua script and code path either way.


## Section 4 — Written Architecture Review

*(Answering Question A and Question B, per the brief's "any two of three.")*

---

## **Question A — Django Admin Performance (Approx. 280 words)**

Adding an index on the primary key alone rarely fixes Django admin performance because the slowdown is often caused by query patterns rather than record lookup.

**1. N+1 queries from related models**

If `list_display` includes foreign keys or custom methods accessing related objects, Django may execute an additional query for every row displayed. I would inspect the SQL using Django Debug Toolbar or `connection.queries`. The fix is to enable `list_select_related` in `ModelAdmin` for `ForeignKey` relationships or override `get_queryset()` and use `select_related()` or `prefetch_related()` for many-to-many relationships. This reduces hundreds of queries to a single joined query.

**2. Expensive COUNT(*) for pagination**

The Django admin paginator executes `QuerySet.count()` to determine the total number of rows. On tables containing 500,000+ records, `COUNT(*)` can become expensive, especially with filters. I would replace the default paginator by setting the `paginator` attribute on the `ModelAdmin` to a custom paginator that avoids an exact count (for example, using PostgreSQL estimated counts). This sacrifices an exact total record count but significantly improves page load time.

**3. Slow ordering and searching**

Admin pages often apply `Meta.ordering` or `ModelAdmin.ordering`. If ordering uses a non-indexed field such as `created_at` or `name`, the database performs expensive sorting. Likewise, poorly chosen `search_fields` can trigger slow `icontains` queries across large text columns. I would either add appropriate database indexes for frequently ordered fields, simplify `ordering`, or change `search_fields` to indexed fields (or PostgreSQL full-text search where appropriate). If a computed value is displayed in `list_display`, I would annotate it in `get_queryset()` instead of calculating it per object.

These optimizations target the actual database workload rather than relying solely on primary-key indexing.

---

## **Question B — Pagination Trade-offs (Approx. 300 words)**

**Offset-based pagination** uses SQL `LIMIT` and `OFFSET` (e.g., page 5 = `LIMIT 100 OFFSET 400`). It is simple to implement and supports random page access, making it suitable for administrative dashboards where users may jump directly to page numbers.

However, at scale it has two major drawbacks. First, the database must scan and discard all preceding rows before returning the requested page, so large offsets become increasingly slow. Second, if records are inserted or deleted while a client is paginating, the results become inconsistent. A newly inserted row may cause duplicates on later pages, while deleted rows can cause records to be skipped.

**Cursor-based pagination** uses a stable ordering field (such as an indexed `created_at` timestamp or primary key) and returns a cursor representing the last item seen. Django REST Framework provides `CursorPagination` for this purpose. Instead of scanning discarded rows, the database performs an indexed range query (for example, `WHERE id > last_seen_id ORDER BY id LIMIT 20`), which scales efficiently even for millions of records.

For a mobile application implementing infinite scroll, cursor pagination is usually the better choice. New records appearing during scrolling do not shift previously viewed pages, providing a more consistent user experience. Database performance also remains stable because indexed range scans avoid increasingly expensive offsets.

The trade-off is flexibility. Cursor pagination does not support jumping directly to page 150 or calculating an exact page count. It also requires a unique, deterministic ordering column to prevent duplicate or missing results.

I would choose **offset pagination** for reporting interfaces or admin screens where numbered pages are important and datasets are moderate. I would choose **cursor pagination** for large APIs, social feeds, event logs, and mobile infinite-scroll interfaces where consistency and database efficiency are more important than random page navigation.

