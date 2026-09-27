"""The two full Ulia examples can be installed deliberately, never on migrate."""

from io import StringIO
import json
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from leai.models import CourseMembership, InstitutionMembership, QuestionSet, SurveyOccurrence
from leai.tests.test_response_models import ResponseFixturesMixin


ROOT = Path(__file__).resolve().parents[1] / "fixtures"


class UliaReferenceCommandTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        membership = InstitutionMembership.objects.create(
            account=self.account, institution=self.institution, role="instructor",
        )
        CourseMembership.objects.create(course=self.course, institution_membership=membership, role="owner")

    def test_dry_run_then_install_two_distinct_frozen_surveys_idempotently(self):
        arguments = (str(self.course.public_id), self.account.email)
        call_command("install_ulia_reflections", *arguments, stdout=StringIO())
        self.assertEqual(SurveyOccurrence.objects.count(), 0)

        call_command("install_ulia_reflections", *arguments, apply=True, stdout=StringIO())
        self.assertEqual(SurveyOccurrence.objects.count(), 2)
        self.assertEqual(QuestionSet.objects.count(), 2)
        for filename in ("ulia_conversational.json", "ulia_likert.json"):
            protocol = json.loads((ROOT / filename).read_text())
            occurrence = SurveyOccurrence.objects.get(label=protocol["title"])
            self.assertEqual(occurrence.revision.compiled_protocol, protocol)
            self.assertEqual(occurrence.revision.source_draft_version.canonical_body, protocol)

        call_command("install_ulia_reflections", *arguments, apply=True, stdout=StringIO())
        self.assertEqual(SurveyOccurrence.objects.count(), 2)

    def test_conflicting_second_reference_does_not_partially_install_first(self):
        QuestionSet.objects.create(
            course=self.course, owner=self.account, title="Structured Reflection — rate and reflect",
            audience="individual", collection_style="guided",
        )
        with self.assertRaises(CommandError):
            call_command("install_ulia_reflections", str(self.course.public_id), self.account.email,
                         apply=True, stdout=StringIO())
        self.assertFalse(QuestionSet.objects.filter(title="Structured Reflection — conversational").exists())
