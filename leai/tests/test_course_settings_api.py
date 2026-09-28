import json

from django.test import TestCase, override_settings

from leai.models import AuditEvent, CourseMembership, InstitutionMembership
from leai.tests.session_client import SessionClient
from leai.tests.test_response_models import ResponseFixturesMixin


ROOT = "/datapipeline/api/v1/"


@override_settings(ROOT_URLCONF="leai.tests.feedback_analyzer_urls")
class CourseBannerSettingsApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Test-Password-Only-2026!")
        self.account.user.save(update_fields=["password"])
        membership = InstitutionMembership.objects.create(
            account=self.account, institution=self.institution, role="instructor",
        )
        CourseMembership.objects.create(
            course=self.course, institution_membership=membership, role="owner",
        )
        response = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({"email": self.account.email, "password": "Test-Password-Only-2026!"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.url = ROOT + f"instructor_courses/{self.course.public_id}/banner-settings/"

    def body(self, **overrides):
        values = {
            "banner_enabled": True,
            "banner_text": "Welcome",
            "banner_dismissible": True,
            "banner_display_mode": "timed",
            "banner_duration_seconds": 30,
            "banner_split_enabled": True,
            "banner_split_mode": "percentage",
            "banner_split_value": 25,
            "expected_settings_version": 1,
        }
        values.update(overrides)
        return values

    def test_reads_and_saves_course_banner_settings_with_audit_and_version(self):
        before = self.client.get(self.url)
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["settings_version"], 1)
        changed = self.client.patch(
            self.url, data=json.dumps(self.body()), content_type="application/json",
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["banner_text"], "Welcome")
        self.assertEqual(changed.json()["settings_version"], 2)
        self.course.refresh_from_db()
        self.assertTrue(self.course.banner_enabled)
        self.assertEqual(self.course.banner_display_mode, "timed")
        self.assertEqual(AuditEvent.objects.filter(action="course.banner_settings.update").count(), 1)

    def test_conflict_and_invalid_banner_values_are_rejected(self):
        stale = self.client.patch(
            self.url, data=json.dumps(self.body(expected_settings_version=2)), content_type="application/json",
        )
        invalid = self.client.patch(
            self.url, data=json.dumps(self.body(banner_split_mode="count", banner_split_value=0)),
            content_type="application/json",
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["settings_version"], 1)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(AuditEvent.objects.filter(action="course.banner_settings.update").count(), 0)

    def test_course_settings_are_not_visible_without_course_management_access(self):
        self.client.logout()
        self.assertEqual(SessionClient().get(self.url).status_code, 401)

    def test_course_settings_hide_courses_without_membership(self):
        foreign_url = ROOT + f"instructor_courses/{self.other_course.public_id}/banner-settings/"
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
