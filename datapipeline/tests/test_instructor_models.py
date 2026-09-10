from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)


class InstructorRelationshipModelTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.ucsc = Institution.objects.create(slug="ucsc", name="UC Santa Cruz")
        self.other = Institution.objects.create(slug="other", name="Other University")

        self.account = InstructorAccount.objects.create(
            user=user_model.objects.create_user(
                username="teacher@example.edu",
                email="teacher@example.edu",
                password="TemporaryPass123!",
            ),
            email="teacher@example.edu",
            display_name="Prof. Test",
        )
        self.ucsc_membership = InstitutionMembership.objects.create(
            institution=self.ucsc,
            instructor=self.account,
        )
        self.other_membership = InstitutionMembership.objects.create(
            institution=self.other,
            instructor=self.account,
        )
        self.course = Course.objects.create(
            course_id="existing-course",
            course_name="Existing Course",
            instructor_name="Prof. Test",
            password=make_password("legacy-password"),
            institution=self.ucsc,
        )

    def test_course_membership_rejects_cross_institution_membership(self):
        membership = CourseMembership(
            course=self.course,
            institution_membership=self.other_membership,
            role=CourseMembership.ROLE_INSTRUCTOR,
        )

        with self.assertRaises(ValidationError):
            membership.full_clean()

    def test_course_allows_only_one_active_owner(self):
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.ucsc_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        second_account = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="second@example.edu",
                email="second@example.edu",
                password="TemporaryPass123!",
            ),
            email="second@example.edu",
            display_name="Second Instructor",
        )
        second_institution_membership = InstitutionMembership.objects.create(
            institution=self.ucsc,
            instructor=second_account,
        )

        with self.assertRaises(ValidationError):
            CourseMembership.objects.create(
                course=self.course,
                institution_membership=second_institution_membership,
                role=CourseMembership.ROLE_OWNER,
            )

    def test_database_constraint_rejects_second_active_owner(self):
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.ucsc_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        second_account = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="db-owner@example.edu",
                email="db-owner@example.edu",
                password="TemporaryPass123!",
            ),
            email="db-owner@example.edu",
            display_name="Database Owner",
        )
        second_institution_membership = InstitutionMembership.objects.create(
            institution=self.ucsc,
            instructor=second_account,
        )
        second_owner = CourseMembership(
            course=self.course,
            institution_membership=second_institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseMembership.objects.bulk_create([second_owner])

    def test_inactive_owner_does_not_block_replacement_owner(self):
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.ucsc_membership,
            role=CourseMembership.ROLE_OWNER,
            is_active=False,
        )
        second_account = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="replacement@example.edu",
                email="replacement@example.edu",
                password="TemporaryPass123!",
            ),
            email="replacement@example.edu",
            display_name="Replacement Instructor",
        )
        second_institution_membership = InstitutionMembership.objects.create(
            institution=self.ucsc,
            instructor=second_account,
        )

        replacement = CourseMembership.objects.create(
            course=self.course,
            institution_membership=second_institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )

        self.assertTrue(replacement.is_active)

    def test_new_course_disables_legacy_password_login(self):
        course = Course.objects.create(
            course_id="new-course",
            course_name="New Course",
            instructor_name="Instructor",
            password=make_password(None),
        )

        self.assertFalse(course.legacy_password_login_enabled)

    def test_manual_account_is_not_email_verified(self):
        self.assertIsNone(self.account.email_verified_at)
        self.assertEqual(self.account.auth_provider, InstructorAccount.AUTH_MANUAL)
        self.assertTrue(self.account.must_change_password)
