"""
Section 2 test.

Interpretation note (documented per assessment instructions for ambiguous
points): running the real 200/min limit against 500 jobs would make this
test take ~2.5 minutes of wall-clock time. We scale the limiter down
(20 requests / 3 seconds) for the test run only -- this exercises the exact
same Lua script and code path, just against a smaller window, so the
atomicity and correctness guarantees being tested are identical to
production. The production task always uses the real 200/60 values from
settings; only the test's limiter instance is scaled.
"""

import time
from collections import defaultdict

import pytest
import redis
from django.conf import settings

from jobs.models import DeadLetterJob, EmailSendRecord
from jobs.rate_limiter import SlidingWindowRateLimiter
from jobs import tasks as tasks_module

from celery.exceptions import Retry

TEST_LIMIT = 20
TEST_WINDOW = 3

def run_task_eagerly(task, args):
    """
    Celery's `.apply()` does not automatically loop through retries the way
    a real worker consuming from a broker would. When a task calls
    self.retry(), Celery raises a `Retry` exception carrying the next
    signature to run (`.sig`) -- normally the worker catches this and
    requeues. In eager/test mode there's no worker, so we do that catching
    and re-invoking ourselves here. This still exercises the exact same
    retry/backoff code path the task defines (self.retry, countdown,
    max_retries) -- we're just standing in for the worker's redelivery loop.
    """
    sig = task.subtask(args=args)
    while True:
        try:
            return sig.apply()
        except Retry as retry_exc:
            sig = retry_exc.sig




@pytest.fixture
def redis_client():
    client = redis.Redis.from_url(settings.REDIS_URL)
    yield client
    for key in client.scan_iter("test:*"):
        client.delete(key)


@pytest.fixture
def scaled_limiter(redis_client, monkeypatch):
    """Point the task at a limiter with a small, fast-to-test window."""
    limiter = SlidingWindowRateLimiter(
        redis_client=redis_client,
        key="test:email:rate_limit",
        limit=TEST_LIMIT,
        window_seconds=TEST_WINDOW,
    )
    monkeypatch.setattr(tasks_module, "SlidingWindowRateLimiter", lambda **kwargs: limiter)
    return limiter


@pytest.mark.django_db
def test_500_jobs_no_loss_rate_limit_respected_retry_works(scaled_limiter, monkeypatch, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

    acquire_timestamps = []
    original_try_acquire = scaled_limiter.try_acquire

    def spy_try_acquire():
        allowed = original_try_acquire()
        if allowed:
            acquire_timestamps.append(scaled_limiter.last_acquire_time)
        return allowed

    monkeypatch.setattr(scaled_limiter, "try_acquire", spy_try_acquire)

    attempt_counts = defaultdict(int)
    FLAKY_JOB_ID = "job-0007"

    def fake_send(to_address, subject, body):
        job_id = body
        attempt_counts[job_id] += 1
        if job_id == FLAKY_JOB_ID and attempt_counts[job_id] == 1:
            raise ConnectionError("simulated transient provider failure")

    monkeypatch.setattr(tasks_module, "send_via_provider", fake_send)

    NUM_JOBS = 500
    for i in range(NUM_JOBS):
        job_id = f"job-{i:04d}"
        run_task_eagerly(
            tasks_module.send_transactional_email,
            [f"user{i}@example.com", "Order confirmation", job_id, job_id],
        )

    total_records = EmailSendRecord.objects.count()
    assert total_records == NUM_JOBS, (
        f"expected {NUM_JOBS} terminal records, got {total_records} -- a job was lost"
    )
    assert EmailSendRecord.objects.filter(status="dead_letter").count() == 0, (
        "no job should have permanently failed in this test"
    )

    acquire_timestamps.sort()
    for t in acquire_timestamps:
        window_count = sum(1 for ts in acquire_timestamps if t - TEST_WINDOW < ts <= t)
        assert window_count <= TEST_LIMIT, (
            f"rate limit exceeded: {window_count} acquisitions in trailing {TEST_WINDOW}s window "
            f"(limit={TEST_LIMIT})"
        )

    assert attempt_counts[FLAKY_JOB_ID] >= 2, "flaky job should have been retried at least once"
    record = EmailSendRecord.objects.get(job_id=FLAKY_JOB_ID)
    assert record.status == "sent", "flaky job should have succeeded after retry, not dead-lettered"


@pytest.mark.django_db
def test_rate_limiter_denies_over_limit(redis_client):
    limiter = SlidingWindowRateLimiter(
        redis_client=redis_client, key="test:unit:rl", limit=5, window_seconds=2
    )
    results = [limiter.try_acquire() for _ in range(8)]
    assert results.count(True) == 5
    assert results.count(False) == 3


@pytest.mark.django_db
def test_dead_letter_after_max_retries(monkeypatch, redis_client, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

    limiter = SlidingWindowRateLimiter(
        redis_client=redis_client, key="test:dl:rl", limit=1000, window_seconds=60
    )
    monkeypatch.setattr(tasks_module, "SlidingWindowRateLimiter", lambda **kwargs: limiter)

    def always_fail(to_address, subject, body):
        raise ConnectionError("provider permanently down")

    monkeypatch.setattr(tasks_module, "send_via_provider", always_fail)

    job_id = "job-permanent-fail"
    run_task_eagerly(
        tasks_module.send_transactional_email,
        ["fail@example.com", "subject", job_id, job_id],
    )

    assert DeadLetterJob.objects.filter(job_id=job_id).exists()
    record = EmailSendRecord.objects.get(job_id=job_id)
    assert record.status == "dead_letter"