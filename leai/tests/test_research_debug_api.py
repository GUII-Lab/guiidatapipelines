import json
from pathlib import Path

from django.test import Client, TestCase

from leai.models import InstitutionMembership
from leai.tests.test_response_models import ResponseFixturesMixin
from leai.tests.session_client import SessionClient


ROOT = "/datapipeline/api/v1/"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ResearchDebugApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Research-Debug-Only-2026!")
        self.account.user.save(update_fields=["password"])
        self.membership = InstitutionMembership.objects.create(
            account=self.account,
            institution=self.institution,
            role="researcher",
        )
        login = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({
                "email": self.account.email,
                "password": "Research-Debug-Only-2026!",
            }),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)
        self.occurrence = self.make_occurrence(
            compiled_protocol=json.loads((FIXTURES / "ulia_conversational.json").read_text()),
        )
        self.started = self.client.post(
            ROOT + f"surveys/{self.occurrence.public_id}/sessions/",
            data=json.dumps({"terms_consent": True, "research_consent": False}),
            content_type="application/json",
        ).json()

    def researcher_headers(self):
        return {}  # The browser supplies its HttpOnly cookie, never a JS token.

    def settings_url(self):
        return ROOT + f"instructor_courses/{self.course.public_id}/debug-settings/"

    def student_debug_url(self):
        return ROOT + f"surveys/{self.occurrence.public_id}/sessions/{self.started['session_id']}/debug/"

    def test_researcher_controls_persisted_course_debug_gate_and_reads_debug_state(self):
        settings_url = self.settings_url()
        self.assertEqual(self.client.get(settings_url, **self.researcher_headers()).json(), {
            "debug_enabled": False,
            "settings_version": 1,
        })
        changed = self.client.patch(
            settings_url,
            data=json.dumps({"debug_enabled": True, "expected_settings_version": 1}),
            content_type="application/json",
            **self.researcher_headers(),
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json(), {"debug_enabled": True, "settings_version": 2})
        self.assertTrue(self.course.__class__.objects.get(pk=self.course.pk).student_debug_enabled)

        access_url = ROOT + f"surveys/{self.occurrence.public_id}/debug-access/"
        self.assertEqual(self.client.get(access_url, **self.researcher_headers()).json(), {"enabled": True})
        debug = self.client.get(self.student_debug_url(), **self.researcher_headers())
        self.assertEqual(debug.status_code, 200)
        self.assertEqual(debug.json()["session_id"], self.started["session_id"])

    def test_debug_is_denied_to_students_and_instructors_even_when_enabled(self):
        self.course.student_debug_enabled = True
        self.course.save(update_fields=["student_debug_enabled"])
        student_result = self.client.get(
            self.student_debug_url(),
            HTTP_AUTHORIZATION=f"Bearer {self.started['token']}",
        )
        self.assertEqual(student_result.status_code, 401)

        self.membership.role = "instructor"
        self.membership.save(update_fields=["role"])
        instructor_result = self.client.get(self.student_debug_url(), **self.researcher_headers())
        self.assertEqual(instructor_result.status_code, 404)
        settings_result = self.client.get(self.settings_url(), **self.researcher_headers())
        self.assertEqual(settings_result.status_code, 404)

    def test_researcher_setting_rejects_stale_version_and_invalid_mutations(self):
        self.course.student_debug_enabled = True
        self.course.settings_version = 2
        self.course.save(update_fields=["student_debug_enabled", "settings_version"])
        url = self.settings_url()
        stale = self.client.patch(
            url,
            data=json.dumps({"debug_enabled": False, "expected_settings_version": 1}),
            content_type="application/json",
            **self.researcher_headers(),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json(), {"error": "settings_conflict"})
        invalid = self.client.patch(
            url,
            data=json.dumps({"debug_enabled": "yes", "expected_settings_version": 2}),
            content_type="application/json",
            **self.researcher_headers(),
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(self.course.__class__.objects.get(pk=self.course.pk).student_debug_enabled)
