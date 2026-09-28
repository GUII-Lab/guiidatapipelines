import time

from django.core.management.base import BaseCommand

from leai.services.analysis_chat import process_feedback_chat_job
from leai.services.jobs import claim_next_domain_job


class Command(BaseCommand):
    help = "Process restart-safe LEAI domain jobs without printing private job content."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one available job and exit.")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        poll_seconds = max(0.25, min(options["poll_seconds"], 30.0))
        while True:
            job = claim_next_domain_job()
            if job is None:
                if options["once"]:
                    return
                time.sleep(poll_seconds)
                continue
            if job.job_type == "feedback_chat_turn":
                process_feedback_chat_job(job)
            if options["once"]:
                return
