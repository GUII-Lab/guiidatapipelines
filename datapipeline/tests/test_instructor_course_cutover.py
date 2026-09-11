from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)


class InstructorCourseCutoverCommandTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='UC Santa Cruz',
        )
        user = get_user_model().objects.create_user(
            username='owner@example.edu',
            email='owner@example.edu',
            password='TemporaryPass123!',
        )
        self.account = InstructorAccount.objects.create(
            user=user,
            email=user.email,
            display_name='Prof. Owner',
            must_change_password=False,
        )
        self.institution_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=self.account,
        )
        self.course = Course.objects.create(
            course_id='course-a',
            course_name='Course A',
            instructor_name='Prof. Owner',
            password='legacy-password',
            institution=self.institution,
            legacy_password_login_enabled=True,
        )
        self.owner = CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        self.other_course = Course.objects.create(
            course_id='course-b',
            course_name='Course B',
            instructor_name='Legacy Owner',
            password='other-password',
            legacy_password_login_enabled=True,
        )

    def run_cutover(self, *extra_args):
        stdout = StringIO()
        call_command(
            'cutover_leai_course_auth',
            '--course-id',
            self.course.course_id,
            *extra_args,
            stdout=stdout,
        )
        return stdout.getvalue()

    def test_dry_run_resolves_owner_and_changes_nothing(self):
        output = self.run_cutover('--dry-run')

        self.course.refresh_from_db()
        self.other_course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)
        self.assertTrue(self.other_course.legacy_password_login_enabled)
        self.assertIn('Course: course-a (Course A)', output)
        self.assertIn('Institution: ucsc (UC Santa Cruz)', output)
        self.assertIn('Owner: owner@example.edu', output)
        self.assertIn('Dry run', output)

    def test_course_without_institution_is_blocked(self):
        self.course.institution = None
        self.course.save(update_fields=['institution'])

        with self.assertRaisesRegex(CommandError, 'has no institution'):
            self.run_cutover()

        self.course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)

    def test_missing_owner_is_blocked(self):
        self.owner.delete()

        with self.assertRaisesRegex(CommandError, 'active owner'):
            self.run_cutover()

        self.course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)

    def test_inactive_owner_is_blocked(self):
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])

        with self.assertRaisesRegex(CommandError, 'active owner'):
            self.run_cutover()

        self.course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)

    def test_inactive_institution_membership_is_blocked(self):
        self.institution_membership.is_active = False
        self.institution_membership.save(update_fields=['is_active'])

        with self.assertRaisesRegex(CommandError, 'institution membership'):
            self.run_cutover()

        self.course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)

    def test_inactive_institution_account_or_user_is_blocked(self):
        states = [
            ('institution', self.institution, 'is_active'),
            ('instructor account', self.account, 'is_active'),
            ('Django user', self.account.user, 'is_active'),
        ]
        for label, instance, field_name in states:
            with self.subTest(label=label):
                setattr(instance, field_name, False)
                instance.save(update_fields=[field_name])
                with self.assertRaisesRegex(CommandError, label):
                    self.run_cutover()
                setattr(instance, field_name, True)
                instance.save(update_fields=[field_name])

        self.course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)

    def test_valid_cutover_changes_only_the_requested_course_flag(self):
        original_course = {
            'course_name': self.course.course_name,
            'instructor_name': self.course.instructor_name,
            'password': self.course.password,
            'institution_id': self.course.institution_id,
        }
        output = self.run_cutover()

        self.course.refresh_from_db()
        self.other_course.refresh_from_db()
        self.assertFalse(self.course.legacy_password_login_enabled)
        self.assertTrue(self.other_course.legacy_password_login_enabled)
        for field_name, expected in original_course.items():
            self.assertEqual(getattr(self.course, field_name), expected)
        self.assertIn('Disabled legacy password login for course-a', output)

    def test_repeated_cutover_is_idempotent(self):
        self.run_cutover()
        output = self.run_cutover()

        self.course.refresh_from_db()
        self.assertFalse(self.course.legacy_password_login_enabled)
        self.assertIn('already uses instructor account authentication', output)

    def test_unknown_course_is_rejected_without_implicit_selection(self):
        stdout = StringIO()
        with self.assertRaisesRegex(CommandError, 'Course missing-course does not exist'):
            call_command(
                'cutover_leai_course_auth',
                '--course-id',
                'missing-course',
                stdout=stdout,
            )

        self.course.refresh_from_db()
        self.other_course.refresh_from_db()
        self.assertTrue(self.course.legacy_password_login_enabled)
        self.assertTrue(self.other_course.legacy_password_login_enabled)
