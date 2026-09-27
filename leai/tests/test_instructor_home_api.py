import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from leai.models import AuditEvent, Course, CourseMembership, Institution, InstitutionMembership, InstructorAccount
from leai.tests.session_client import SessionClient


ROOT = "/datapipeline/api/v1/"


class InstructorHomeApiTests(TestCase):
    def setUp(self):
        self.client = SessionClient()
        self.institution = Institution.objects.create(slug="ucsc", name="UC Santa Cruz")
        self.other = Institution.objects.create(slug="ucd", name="UC Davis")
        user = get_user_model().objects.create_user(
            username="home-teacher", email="home-teacher@ucsc.edu", password="Test-Password-Only-2026!"
        )
        self.account = InstructorAccount.objects.create(
            user=user, email=user.email, display_name="Home Teacher"
        )
        self.membership = InstitutionMembership.objects.create(
            account=self.account, institution=self.institution, role="instructor"
        )
        login = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({"email": user.email, "password": "Test-Password-Only-2026!"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)

    def post_course(self, **overrides):
        body = {"institution_slug": "ucsc", "course_code": "cmpm-80h", "course_name": "Game Design"}
        body.update(overrides)
        return self.client.post(ROOT + "instructor_courses/", data=json.dumps(body), content_type="application/json")

    def test_me_lists_only_active_institutions_with_create_permission(self):
        InstitutionMembership.objects.create(
            account=self.account, institution=self.other, role="researcher", is_active=False
        )
        response = self.client.get(ROOT + "instructor_me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["institutions"], [
            {"slug": "ucsc", "name": "UC Santa Cruz", "can_create_courses": True}
        ])

    def test_profile_update_changes_only_display_name_and_audits_once(self):
        response = self.client.patch(
            ROOT + "instructor_me/",
            data=json.dumps({"display_name": "  Updated Teacher  "}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_name"], "Updated Teacher")
        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, "Updated Teacher")
        self.assertEqual(self.account.email, "home-teacher@ucsc.edu")
        audit = AuditEvent.objects.get(action="auth.profile_update")
        self.assertEqual(audit.actor_account, self.account)
        self.assertEqual(audit.bounded_metadata, {"changed_fields": ["display_name"]})

    def test_profile_update_rejects_email_and_blank_name_without_writes(self):
        for body in ({"email": "other@ucsc.edu"}, {"display_name": "  "}):
            with self.subTest(body=body):
                response = self.client.patch(
                    ROOT + "instructor_me/", data=json.dumps(body), content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, "Home Teacher")
        self.assertFalse(AuditEvent.objects.filter(action="auth.profile_update").exists())

    def test_course_creation_persists_course_owner_and_one_audit(self):
        response = self.post_course()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["course_code"], "cmpm-80h")
        course = Course.objects.get(public_id=response.json()["course_id"])
        self.assertEqual(course.institution, self.institution)
        self.assertEqual(course.name, "Game Design")
        self.assertEqual(
            CourseMembership.objects.get(course=course, institution_membership=self.membership).role,
            "owner",
        )
        self.assertEqual(self.client.get(ROOT + "instructor_courses/").json()["courses"][0]["course_id"], str(course.public_id))
        audit = AuditEvent.objects.get(action="course.create")
        self.assertEqual(audit.course, course)
        self.assertEqual(audit.actor_account, self.account)

    def test_course_creation_denies_researcher_and_other_institution(self):
        self.assertEqual(self.post_course(institution_slug="ucd").status_code, 403)
        self.membership.role = "researcher"
        self.membership.save(update_fields=["role"])
        self.assertEqual(self.post_course().status_code, 403)
        self.assertEqual(Course.objects.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(action="course.create").exists())

    def test_duplicate_and_invalid_course_codes_do_not_create_partial_memberships(self):
        self.assertEqual(self.post_course().status_code, 201)
        self.assertEqual(self.post_course().status_code, 409)
        self.assertEqual(self.post_course(course_code="../bad").status_code, 400)
        self.assertEqual(Course.objects.count(), 1)
        self.assertEqual(CourseMembership.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="course.create").count(), 1)

    def test_profile_and_creation_require_signed_in_session(self):
        anonymous = SessionClient()
        profile = anonymous.patch(
            ROOT + "instructor_me/",
            data=json.dumps({"display_name": "Intruder"}),
            content_type="application/json",
        )
        created = anonymous.post(
            ROOT + "instructor_courses/",
            data=json.dumps({"institution_slug": "ucsc", "course_code": "intruder", "course_name": "Intruder"}),
            content_type="application/json",
        )
        self.assertEqual(profile.status_code, 401)
        self.assertEqual(created.status_code, 401)
        self.assertEqual(Course.objects.count(), 0)
