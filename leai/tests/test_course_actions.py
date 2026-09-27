from django.contrib.auth import get_user_model
from django.test import TestCase

from leai.models import (
    Course,
    CourseAccessRestriction,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)
from leai.services.actions import ACTIONS, allowed_course_actions, has_course_action


class CourseActionTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(slug="ucsc", name="UC Santa Cruz")
        self.course = Course.objects.create(
            institution=self.institution,
            course_code="winter",
            name="Winter course",
        )

    def make_account(self, suffix, institution_role="instructor", platform_role="member"):
        user = get_user_model().objects.create_user(
            username=suffix,
            email=f"{suffix}@ucsc.edu",
        )
        account = InstructorAccount.objects.create(
            user=user,
            email=user.email,
            display_name=suffix,
            platform_role=platform_role,
            must_change_password=False,
        )
        membership = InstitutionMembership.objects.create(
            account=account,
            institution=self.institution,
            role=institution_role,
        )
        return account, membership

    def test_closed_action_vocabulary_and_role_matrix(self):
        expected_all = (
            "course.manage",
            "feedback.author",
            "feedback.publish",
            "responses.view",
            "responses.export",
            "analysis.use",
        )
        self.assertEqual(ACTIONS, expected_all)
        for role in ("owner", "instructor", "ta"):
            with self.subTest(role=role):
                account, membership = self.make_account(role)
                CourseMembership.objects.create(
                    course=self.course,
                    institution_membership=membership,
                    role=role,
                )
                expected = expected_all if role != "ta" else (
                    "feedback.author", "responses.view", "analysis.use"
                )
                self.assertEqual(allowed_course_actions(account, self.course), expected)
                self.assertEqual(
                    has_course_action(account, self.course, "responses.view"), True
                )

    def test_researcher_inherits_unless_restricted_but_explicit_grant_survives(self):
        account, membership = self.make_account("researcher", institution_role="researcher")
        self.assertEqual(allowed_course_actions(account, self.course), ACTIONS)
        CourseAccessRestriction.objects.create(
            course=self.course,
            institution_membership=membership,
            denied=True,
        )
        self.assertEqual(allowed_course_actions(account, self.course), ())
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=membership,
            role="instructor",
        )
        self.assertEqual(allowed_course_actions(account, self.course), ACTIONS)

    def test_restriction_does_not_remove_an_explicit_ta_grant(self):
        account, membership = self.make_account("ta-researcher", institution_role="researcher")
        CourseAccessRestriction.objects.create(
            course=self.course,
            institution_membership=membership,
            denied=True,
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=membership,
            role="ta",
        )
        self.assertEqual(
            allowed_course_actions(account, self.course),
            ("feedback.author", "responses.view", "analysis.use"),
        )

    def test_platform_admin_bypasses_restriction_but_not_inactive_course(self):
        account, membership = self.make_account(
            "admin", institution_role="researcher", platform_role="platform_admin"
        )
        CourseAccessRestriction.objects.create(
            course=self.course,
            institution_membership=membership,
            denied=True,
        )
        self.assertEqual(allowed_course_actions(account, self.course), ACTIONS)
        self.course.lifecycle_state = "completed"
        self.course.save(update_fields=["lifecycle_state"])
        self.assertEqual(allowed_course_actions(account, self.course), ())

    def test_password_change_flag_is_ignored_but_inactive_accounts_are_denied(self):
        account, membership = self.make_account("inactive")
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=membership,
            role="owner",
        )
        account.must_change_password = True
        account.save(update_fields=["must_change_password"])
        self.assertEqual(allowed_course_actions(account, self.course), ACTIONS)
        account.is_active = False
        account.save(update_fields=["is_active"])
        self.assertEqual(allowed_course_actions(account, self.course), ())

    def test_inactive_institution_membership_and_django_user_are_denied(self):
        account, membership = self.make_account("lapsed")
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=membership,
            role="owner",
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        self.assertEqual(allowed_course_actions(account, self.course), ())
        membership.is_active = True
        membership.save(update_fields=["is_active"])
        account.user.is_active = False
        account.user.save(update_fields=["is_active"])
        self.assertEqual(allowed_course_actions(account, self.course), ())

    def test_unrelated_institution_and_unknown_actions_fail_closed(self):
        account, _ = self.make_account("outsider")
        other = Institution.objects.create(slug="ucd", name="UC Davis")
        other_course = Course.objects.create(
            institution=other, course_code="spring", name="Spring course"
        )
        self.assertEqual(allowed_course_actions(account, other_course), ())
        self.assertFalse(has_course_action(account, other_course, "responses.view"))
        self.assertFalse(has_course_action(account, self.course, "unknown.action"))
