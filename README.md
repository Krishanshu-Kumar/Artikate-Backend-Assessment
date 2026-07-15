# Artikate-Backend-Assessment
# Artikate Studio — Backend Developer Assessment

Submission for the Backend Developer (Python / Django / Systems Engineering)
technical assessment. Covers Sections 1–4 (Section 5 screen recording not
included).

## What's in here

- `orders/`, `tenants/` — Section 1 (N+1 fix) and Section 3 (multi-tenant
  isolation)
- `jobs/` — Section 2 (Celery + Redis job queue, rate limiter)
- `ANSWERS.md` — written answers for all sections
- `DESIGN.md` — architecture decisions for Section 2

## Requirements

- Python 3.14
- A Redis instance (see setup below — either local or a free hosted one)

## Setup (should take under 5 minutes)

### 1. Clone and create a virtual environment

```bash
git clone <this-repo-url>
cd Artikate-Backend-Assessment
python -m venv venv
source venv/Scripts/activate    # Windows (Git Bash)
# source venv/bin/activate      # macOS/Linux
pip install -r requirements.txt
```

### 2. Get a Redis instance

The job queue and rate limiter (Section 2) need a real Redis instance —
Redis's atomic operations (Lua scripts, sorted sets) are core to how the
rate limiter works, so this isn't optional for that section.

Two options:

**Option A — free hosted Redis (fastest, no local install):**
1. Sign up at [upstash.com](https://upstash.com), create a free Redis
   database.
2. Copy its connection URL (starts with `rediss://`).
3. Create a `.env` file at the repo root:
   ```
   REDIS_URL="rediss://default:<password>@<host>.upstash.io:6379"
   ```

**Option B — local Redis via Docker:**
```bash
docker run -d --name redis -p 6379:6379 redis:7-alpine
```
Then in `.env`:
```
REDIS_URL="redis://localhost:6379/0"
```

If no `.env`/`REDIS_URL` is set, the app defaults to
`redis://localhost:6379/0`.

### 3. Run migrations and seed data

```bash
python manage.py migrate
python manage.py seed_data
```

This creates two tenants: "Acme Corp" (250 orders) and "Globex Inc"
(5 orders), each with order items — used to demonstrate both the N+1 fix
(Section 1) and tenant isolation (Section 3).

### 4. Run the Django dev server

```bash
python manage.py runserver
```

Server runs at `http://127.0.0.1:8000/`. django-silk profiler is available
at `http://127.0.0.1:8000/silk/`.

### 5. (Section 2 only) Run a Celery worker

In a separate terminal:
```bash
source venv/Scripts/activate
celery -A core worker -l info --pool=solo
```

**Note (Windows):** Celery's default worker pool uses `fork()`, which
isn't available on Windows — `--pool=solo` is required on Windows. On
macOS/Linux this flag can be omitted.

## Running the tests

```bash
pytest
```

Or individual sections:
```bash
pytest orders/test_tenant_isolation.py -v   # Section 3
pytest jobs/test_queue.py -v                # Section 2
```

Note: `jobs/test_queue.py` takes roughly a minute to run — it submits 500
jobs against a (scaled-down, for test speed) real rate limiter and confirms
none are lost, none exceed the limit, and a forced failure is retried
correctly. See the docstring at the top of that file for why the window is
scaled down for testing.

## Trying things out manually

**Section 1 — N+1 fix, with tenant header:**
```bash
curl -H "X-Tenant-Slug: acme" http://127.0.0.1:8000/api/orders/summary/
```
Check query counts before/after in django-silk at `/silk/requests/`.

**Section 2 — queue a test email job:**
```bash
python manage.py shell -c "
from jobs.tasks import send_transactional_email
r = send_transactional_email.delay('test@example.com', 'hi', 'test-body', 'test-job-1')
print(r.id)
"
```
Watch the Celery worker terminal to see it pick up and process the job.
Check the result:
```bash
python manage.py shell -c "
from jobs.models import EmailSendRecord
print(EmailSendRecord.objects.get(job_id='test-job-1').status)
"
```

**Section 3 — tenant isolation:**
```bash
curl -H "X-Tenant-Slug: acme" http://127.0.0.1:8000/api/orders/summary/    # 250 orders
curl -H "X-Tenant-Slug: globex" http://127.0.0.1:8000/api/orders/summary/  # 5 orders
```

## Written answers

See `ANSWERS.md` for the investigation log (Section 1), rate limiter and
SIGKILL reasoning (Section 2), async/thread-local failure modes
(Section 3), and the two written architecture questions answered
(Section 4). See `DESIGN.md` for the Section 2 architecture write-up.

## Notes / known gaps

- The Section 2 task provides at-least-once delivery, not exactly-once —
  see the SIGKILL discussion in `ANSWERS.md` for the specific trade-off and
  what a fully idempotent version would additionally need.
- The `EmailProviderClient` in `jobs/provider.py` is a stub with a
  simulated ~2% failure rate, standing in for a real provider SDK (e.g.
  SendGrid, Postmark).