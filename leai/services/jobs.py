import threading
import uuid
from datetime import timedelta

from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from leai.models.jobs import DomainJob


LEASE_DURATION = timedelta(minutes=5)
MAX_ATTEMPTS = 5
MAX_ACTIVE_JOB_THREADS = 4
_active_job_slots = threading.BoundedSemaphore(MAX_ACTIVE_JOB_THREADS)


def _validate_payload(job_type, payload):
    if job_type != "feedback_chat_turn" or not isinstance(payload, dict):
        raise ValueError("unsupported job payload")
    if set(payload) != {"user_message_id", "occurrence_ids"}:
        raise ValueError("feedback chat jobs accept canonical IDs only")
    message_id = payload["user_message_id"]
    if not isinstance(message_id, str) or not message_id.isdigit() or int(message_id) < 1 or len(message_id) > 20:
        raise ValueError("user_message_id must be a positive canonical ID")
    occurrence_ids = payload["occurrence_ids"]
    if not isinstance(occurrence_ids, list) or len(occurrence_ids) > 20:
        raise ValueError("occurrence_ids must be a bounded list")
    canonical_ids = []
    for value in occurrence_ids:
        canonical_ids.append(str(uuid.UUID(value)))
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("occurrence_ids must be unique")
    return {"user_message_id": str(int(message_id)), "occurrence_ids": canonical_ids}


def enqueue_domain_job(*, job_type, course, actor, payload):
    clean_payload = _validate_payload(job_type, payload)
    return DomainJob.objects.create(
        job_type=job_type,
        course=course,
        actor_account=actor,
        payload=clean_payload,
    )


def claim_domain_job(job_id):
    """Claim one specific pending job or recover its expired web-thread lease."""
    with transaction.atomic():
        now = timezone.now()
        job = (
            DomainJob.objects.select_for_update(skip_locked=True)
            .filter(public_id=job_id)
            .filter(attempts__lt=MAX_ATTEMPTS)
            .filter(Q(status="pending") | Q(status="running", lease_expires_at__lte=now))
            .first()
        )
        if job is None:
            DomainJob.objects.filter(
                public_id=job_id,
                attempts__gte=MAX_ATTEMPTS,
            ).filter(Q(status="pending") | Q(status="running", lease_expires_at__lte=now)).update(
                status="failed",
                error_code="worker_unavailable",
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
            return None
        job.status = "running"
        job.attempts += 1
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + LEASE_DURATION
        job.save(update_fields=("status", "attempts", "lease_token", "lease_expires_at", "updated_at"))
    return job


def _run_domain_job(job):
    try:
        close_old_connections()
        if job.job_type == "feedback_chat_turn":
            from leai.services.analysis_chat import process_feedback_chat_job
            process_feedback_chat_job(job)
    finally:
        try:
            connection.close()
        finally:
            _active_job_slots.release()


def start_domain_job_thread(job_id):
    """Run a persisted job in a daemon thread owned by the existing web dyno."""
    if not _active_job_slots.acquire(blocking=False):
        return False
    try:
        job = claim_domain_job(job_id)
    except Exception:
        _active_job_slots.release()
        raise
    if job is None:
        _active_job_slots.release()
        return False
    try:
        thread = threading.Thread(
            target=_run_domain_job,
            args=(job,),
            name=f"leai-job-{job.public_id}",
            daemon=True,
        )
        thread.start()
    except RuntimeError:
        _active_job_slots.release()
        fail_domain_job(job_id=job.pk, lease_token=job.lease_token, error_code="thread_start_failed")
        return False
    return True


def _valid_result(result):
    if not isinstance(result, dict) or set(result) != {"assistant_message_id"}:
        raise ValueError("job result must contain only an assistant message ID")
    value = result["assistant_message_id"]
    if not isinstance(value, str) or not value.isdigit() or int(value) < 1 or len(value) > 20:
        raise ValueError("assistant_message_id must be a positive canonical ID")
    return {"assistant_message_id": str(int(value))}


def complete_domain_job(*, job_id, lease_token, result):
    clean_result = _valid_result(result)
    with transaction.atomic():
        job = DomainJob.objects.select_for_update().filter(pk=job_id, status="running", lease_token=lease_token).first()
        if job is None:
            return False
        job.status = "completed"
        job.result = clean_result
        job.error_code = None
        job.lease_token = None
        job.lease_expires_at = None
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "result", "error_code", "lease_token", "lease_expires_at", "completed_at", "updated_at"))
        return True


def fail_domain_job(*, job_id, lease_token, error_code="turn_failed"):
    if not isinstance(error_code, str) or not error_code.replace("_", "").isalnum() or len(error_code) > 64:
        error_code = "turn_failed"
    with transaction.atomic():
        job = DomainJob.objects.select_for_update().filter(pk=job_id, status="running", lease_token=lease_token).first()
        if job is None:
            return False
        job.status = "failed"
        job.error_code = error_code
        job.result = None
        job.lease_token = None
        job.lease_expires_at = None
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "error_code", "result", "lease_token", "lease_expires_at", "completed_at", "updated_at"))
        return True


def public_job_status(job):
    return {
        "id": str(job.public_id),
        "status": job.status,
        "error_code": job.error_code,
        "result": job.result if job.status == "completed" else None,
    }
