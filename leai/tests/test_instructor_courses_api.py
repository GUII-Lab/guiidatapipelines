import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from leai.tests.session_client import SessionClient

from leai.models import (
    Course,
    CourseAccessRestriction,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)


ROOT = "/datapipeline/api/v1/"


class InstructorCourseApiTests(TestCase):
    def setUp(self):
        self.client = SessionClient()
        self.ucsc = Institution.objects.create(slug="ucsc", name="UC Santa Cruz")
        self.ucd = Institution.objects.create(slug="ucd", name="UC Davis")
        user = get_user_model().objects.create_user(
            username="teacher", email="teacher@ucsc.edu", password="Test-Password-Only-2026!"
        )
        self.account = InstructorAccount.objects.create(
            user=user,
            email=user.email,
            display_name="Teacher",
            must_change_password=False,
        )
        self.membership = InstitutionMembership.objects.create(
            account=self.account,
            institution=self.ucsc,
            role="instructor",
        )
        self.own_course = Course.objects.create(
            institution=self.ucsc, course_code="winter", name="Winter"
        )
        CourseMembership.objects.create(
            course=self.own_course,
            institution_membership=self.membership,
            role="owner",
        )
        self.other_course = Course.objects.create(
            institution=self.ucd, course_code="spring", name="Spring"
        )
        login = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({"email": user.email, "password": "Test-Password-Only-2026!"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)

    def get(self, path):
        return self.client.get(ROOT + path)

    def test_course_list_returns_only_accessible_active_courses_with_actions(self):
        response = self.get("instructor_courses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(len(response.json()["courses"]), 1)
        course = response.json()["courses"][0]
        self.assertEqual(course["course_id"], str(self.own_course.public_id))
        self.assertEqual(course["course_name"], "Winter")
        self.assertEqual(course["institution_slug"], "ucsc")
        self.assertEqual(
            course["allowed_actions"],
            [
                "course.manage", "feedback.author", "feedback.publish",
                "responses.view", "responses.export", "analysis.use",
            ],
        )
        self.assertEqual(course["role"], "owner")

    def test_inaccessible_course_detail_is_nondisclosing_not_found(self):
        unassigned_same_institution = Course.objects.create(
            institution=self.ucsc, course_code="unassigned", name="Unassigned"
        )
        allowed = self.get(f"instructor_courses/{self.own_course.public_id}/")
        denied = self.get(f"instructor_courses/{self.other_course.public_id}/")
        same_institution_denied = self.get(
            f"instructor_courses/{unassigned_same_institution.public_id}/"
        )
        unknown = self.get("instructor_courses/11111111-1111-4111-8111-111111111111/")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(same_institution_denied.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(denied.json(), unknown.json())

    def test_platform_admin_keeps_explicit_course_attribution(self):
        self.account.platform_role = "platform_admin"
        self.account.save(update_fields=["platform_role"])
        response = self.get("instructor_courses/")
        rows = {row["course_name"]: row for row in response.json()["courses"]}
        self.assertEqual(rows["Winter"]["role"], "owner")
        self.assertEqual(rows["Spring"]["role"], "platform_admin")

    def test_legacy_password_change_flag_does_not_block_course_list(self):
        self.account.must_change_password = True
        self.account.save(update_fields=["must_change_password"])
        response = self.get("instructor_courses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["courses"]), 1)
        self.assertEqual(self.get("instructor_me/").status_code, 200)

    def test_researcher_inherits_course_unless_restricted(self):
        self.membership.role = "researcher"
        self.membership.save(update_fields=["role"])
        CourseMembership.objects.filter(institution_membership=self.membership).delete()
        extra = Course.objects.create(
            institution=self.ucsc, course_code="another", name="Another"
        )
        CourseAccessRestriction.objects.create(
            course=extra,
            institution_membership=self.membership,
            denied=True,
        )
        response = self.get("instructor_courses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["course_name"] for row in response.json()["courses"]], ["Winter"])
        self.assertEqual(response.json()["courses"][0]["role"], "researcher")

    def test_course_list_query_count_does_not_grow_per_course(self):
        with CaptureQueriesContext(connection) as one_course:
            self.get("instructor_courses/")
        for number in range(20):
            course = Course.objects.create(
                institution=self.ucsc,
                course_code=f"extra-{number}",
                name=f"Extra {number}",
            )
            CourseMembership.objects.create(
                course=course,
                institution_membership=self.membership,
                role="instructor",
            )
        with CaptureQueriesContext(connection) as many_courses:
            response = self.get("instructor_courses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["courses"]), 21)
        self.assertLessEqual(len(many_courses), len(one_course) + 2)
