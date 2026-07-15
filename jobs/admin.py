from django.contrib import admin

from .models import DeadLetterJob, EmailSendRecord


@admin.action(description="Requeue selected jobs for retry")
def requeue_jobs(modeladmin, request, queryset):
    from .tasks import send_transactional_email

    for job in queryset:
        send_transactional_email.delay(job.to_address, job.subject, job.body, job.job_id)


@admin.register(DeadLetterJob)
class DeadLetterJobAdmin(admin.ModelAdmin):
    list_display = ("job_id", "to_address", "subject", "created_at")
    readonly_fields = ("job_id", "to_address", "subject", "body", "last_error", "created_at")
    actions = [requeue_jobs]


@admin.register(EmailSendRecord)
class EmailSendRecordAdmin(admin.ModelAdmin):
    list_display = ("job_id", "to_address", "status", "updated_at")
    list_filter = ("status",)