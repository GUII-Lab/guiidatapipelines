from datetime import timedelta

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from leai.models.jobs import DomainJob
from leai.services.jobs import (
    claim_domain_job,
    complete_domain_job,
    enqueue_domain_job,
    public_job_status,
    start_domain_job_thread,
)
from leai.tests.test_response_models import ResponseFixturesMixin


class DomainJobTests(ResponseFixturesMixin, TestCase):
    def test_feedback_chat_job_accepts_only_canonical_ids(self):
        occurrence_id = str(self.make_occurrence().public_id)
        job = enqueue_domain_job(
            job_type="feedback_chat_turn",
            course=self.course,
            actor=self.account,
            payload={"user_message_id": "17", "occurrence_ids": [occurrence_id]},
        )
        self.assertEqual(job.payload, {"user_message_id": "17", "occurrence_ids": [occurrence_id]})
        with self.assertRaises(ValueError):
            enqueue_domain_job(
                job_type="feedback_chat_turn",
                course=self.course,
                actor=self.account,
                payload={"user_message_id": "18", "occurrence_ids": [], "transcript": "private"},
            )

    def test_specific_job_claim_is_exclusive_and_expired_lease_can_be_reclaimed(self):
        job = enqueue_domain_job(
            job_type="feedback_chat_turn",
            course=self.course,
            actor=self.account,
            payload={"user_message_id": "17", "occurrence_ids": []},
        )
        first = claim_domain_job(job.public_id)
        self.assertEqual(first.pk, job.pk)
        first_token = first.lease_token
        self.assertIsNone(claim_domain_job(job.public_id))
        DomainJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        reclaimed = claim_domain_job(job.public_id)
        self.assertEqual(reclaimed.pk, job.pk)
        self.assertNotEqual(reclaimed.lease_token, first_token)
        self.assertFalse(complete_domain_job(job_id=job.pk, lease_token=first_token, result={"assistant_message_id": "22"}))
        self.assertTrue(complete_domain_job(job_id=job.pk, lease_token=reclaimed.lease_token, result={"assistant_message_id": "22"}))

    def test_public_status_omits_job_payload_and_lease_material(self):
        job = enqueue_domain_job(
            job_type="feedback_chat_turn",
            course=self.course,
            actor=self.account,
            payload={"user_message_id": "17", "occurrence_ids": []},
        )
        result = public_job_status(job)
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("payload", result)
        self.assertNotIn("lease_token", result)
        self.assertNotIn("user_message_id", str(result))

    @patch("leai.services.jobs.threading.Thread")
    def test_job_runs_on_a_daemon_thread_after_claiming_its_database_lease(self, thread_class):
        job = enqueue_domain_job(
            job_type="feedback_chat_turn",
            course=self.course,
            actor=self.account,
            payload={"user_message_id": "17", "occurrence_ids": []},
        )
        self.assertTrue(start_domain_job_thread(str(job.public_id)))
        thread_class.assert_called_once()
        kwargs = thread_class.call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["args"][0].pk, job.pk)
        thread_class.return_value.start.assert_called_once_with()
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
