from django.db import models

from django.db import models


class EmailSendRecord(models.Model):
    """One row per job_id -- lets tests/ops assert on final state (sent/dead_letter)."""

    job_id = models.CharField(max_length=64, unique=True)
    to_address = models.EmailField()
    status = models.CharField(
        max_length=20,
        choices=[("sent", "Sent"), ("dead_letter", "Dead Letter")],
    )
    updated_at = models.DateTimeField(auto_now=True)


class DeadLetterJob(models.Model):
    """Permanently-failed jobs, kept for manual inspection/replay."""

    job_id = models.CharField(max_length=64)
    to_address = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    last_error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
