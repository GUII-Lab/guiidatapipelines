"""Install opt-in reference copies of Ulia's two complete reflection protocols."""

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leai.models import (
    Course, CourseMembership, InstructorAccount, QuestionSet, QuestionSetDraft,
    QuestionSetDraftVersion, QuestionSetRevision, SurveyOccurrence,
)
from leai.services.protocol import validate_protocol


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


class Command(BaseCommand):
    help = "Preview or install the two Ulia reference reflections into one active course."

    def add_arguments(self, parser):
        parser.add_argument("course_id", help="Exact course public UUID")
        parser.add_argument("owner_email", help="Exact instructor account email")
        parser.add_argument("--apply", action="store_true", help="Create the survey records")

    @transaction.atomic
    def handle(self, *args, **options):
        course = Course.objects.filter(public_id=options["course_id"], lifecycle_state="active").first()
        account = InstructorAccount.objects.filter(email__iexact=options["owner_email"], is_active=True).first()
        if course is None or account is None:
            raise CommandError("Active course or instructor account not found")
        if not CourseMembership.objects.filter(
            course=course, institution_membership__account=account,
            institution_membership__is_active=True, role__in=["owner", "instructor"],
        ).exists():
            raise CommandError("The selected account must be an active owner or instructor of this course")

        for filename in ("ulia_conversational.json", "ulia_likert.json"):
            protocol = validate_protocol(json.loads((FIXTURES / filename).read_text()))
            digest = hashlib.sha256(json.dumps(protocol, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
            existing = list(QuestionSet.objects.filter(course=course, owner=account, title=protocol["title"]))
            if len(existing) > 1:
                raise CommandError(f"Ambiguous existing question sets: {protocol['title']}")
            if existing:
                occurrences = SurveyOccurrence.objects.filter(revision__question_set=existing[0])
                if (occurrences.count() != 1 or occurrences.first().revision.content_hash != digest):
                    raise CommandError(f"Existing question set differs from reference: {protocol['title']}")
                self.stdout.write(f"Already installed: {protocol['title']} ({occurrences.first().public_id})")
                continue
            if not options["apply"]:
                self.stdout.write(f"Would install: {protocol['title']} (11 items)")
                continue
            with transaction.atomic():
                question_set = QuestionSet.objects.create(
                    course=course, owner=account, title=protocol["title"],
                    audience="individual", collection_style="guided",
                )
                draft = QuestionSetDraft.objects.create(
                    question_set=question_set, current_version=1,
                    canonical_body=protocol, updated_by=account,
                )
                draft_version = QuestionSetDraftVersion.objects.create(
                    draft=draft, version_number=1, content_hash=digest,
                    canonical_body=protocol, created_by=account,
                )
                revision = QuestionSetRevision.objects.create(
                    question_set=question_set, revision_number=1,
                    source_draft_version=draft_version, content_hash=digest,
                    compiled_protocol=protocol, compiler_version="structured-v1",
                    engine_version="structured-v1", created_by=account,
                )
                occurrence = SurveyOccurrence.objects.create(
                    revision=revision, course=course, created_by=account,
                    label=protocol["title"], provenance="native", management_mode="managed",
                )
            self.stdout.write(f"Installed: {protocol['title']} ({occurrence.public_id})")
