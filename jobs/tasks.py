import logging

from celery import shared_task

from .models import DeadLetterJob, EmailSendRecord
from .rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

MAX_FAILURE_RETRIES = 5
RETRY_BACKOFF_BASE = 2
RETRY_BACKOFF_MAX = 300
RATE_LIMIT_RETRY_DELAY = 1


def send_via_provider(to_address: str, subject: str, body: str) -> None:
    from .provider import EmailProviderClient
    EmailProviderClient().send(to_address, subject, body)


@shared_task(
    bind=True,
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def send_transactional_email(self, to_address: str, subject: str, body: str, job_id: str, failure_attempts: int = 0):
    """
    `failure_attempts` is passed explicitly through each retry as a task
    kwarg, rather than relying on Celery's built-in `request.retries`
    counter. That counter increments on every self.retry() call -- including
    ones triggered purely by the rate limiter saying "not your turn yet,"
    which happen routinely and legitimately under burst load. Letting those
    count against a retry ceiling means a job could get dead-lettered simply
    for having waited behind other jobs, which is not a failure at all.
    Separating "attempts caused by real send failures" from "waits caused by
    the rate limiter" is the actual fix.
    """
    limiter = SlidingWindowRateLimiter()
    if not limiter.try_acquire():
        raise self.retry(
            args=[to_address, subject, body, job_id],
            kwargs={"failure_attempts": failure_attempts},
            countdown=RATE_LIMIT_RETRY_DELAY,
        )

    try:
        send_via_provider(to_address, subject, body)
    except Exception as exc:
        logger.warning(
            "Email send failed for job %s (attempt %d): %s",
            job_id, failure_attempts + 1, exc,
        )
        if failure_attempts + 1 >= MAX_FAILURE_RETRIES:
            _send_to_dead_letter(job_id, to_address, subject, body, str(exc))
            return
        countdown = min(RETRY_BACKOFF_BASE * (2 ** failure_attempts), RETRY_BACKOFF_MAX)
        raise self.retry(
            args=[to_address, subject, body, job_id],
            kwargs={"failure_attempts": failure_attempts + 1},
            exc=exc,
            countdown=countdown,
        )

    EmailSendRecord.objects.update_or_create(
        job_id=job_id,
        defaults={"to_address": to_address, "status": "sent"},
    )


def _send_to_dead_letter(job_id, to_address, subject, body, error):
    DeadLetterJob.objects.create(
        job_id=job_id, to_address=to_address, subject=subject, body=body, last_error=error,
    )
    EmailSendRecord.objects.update_or_create(
        job_id=job_id,
        defaults={"to_address": to_address, "status": "dead_letter"},
    )
    logger.error("Job %s exhausted retries, moved to dead letter: %s", job_id, error)