import io

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    LegacyCourseOwnershipReview,
)


class ProvisionInstructorCommandTests(TestCase):
    def run_command(self, *course_ids):
        stdout = io.StringIO()
        options = {
            'email': 'teacher@example.edu',
            'display_name': 'Prof. Test',
            'institution_slug': 'ucsc',
            'institution_name': 'UC Santa Cruz',
            'stdout': stdout,
        }
        if course_ids:
            options['course_id'] = list(course_ids)
        call_command('provision_leai_instructor', **options)
        return stdout.getvalue()

    def test_command_creates_manual_account_and_prints_temporary_password_once(self):
        output = self.run_command()

        account = InstructorAccount.objects.get(email='teacher@example.edu')
        self.assertTrue(account.user.check_password(
            output.split('Temporary password: ', 1)[1].splitlines()[0],
        ))
        self.assertEqual(output.count('Temporary password:'), 1)
        self.assertTrue(account.must_change_password)
        self.assertIsNone(account.email_verified_at)
        self.assertEqual(account.auth_provider, InstructorAccount.AUTH_MANUAL)
        membership = InstitutionMembership.objects.get(instructor=account)
        self.assertEqual(membership.institution.slug, 'ucsc')
        self.assertEqual(membership.institution.name, 'UC Santa Cruz')

    def test_command_links_reviewed_legacy_course_to_owner(self):
        course = Course.objects.create(
            course_id='legacy-course',
            course_name='Legacy Course',
            instructor_name='Prof. Test',
            password=make_password('shared-password'),
            legacy_password_login_enabled=True,
        )

        self.run_command(course.course_id)

        course.refresh_from_db()
        owner = CourseMembership.objects.get(course=course)
        review = LegacyCourseOwnershipReview.objects.get(course=course)
        self.assertEqual(course.institution.slug, 'ucsc')
        self.assertEqual(owner.role, CourseMembership.ROLE_OWNER)
        self.assertEqual(owner.institution_membership.instructor.email, 'teacher@example.edu')
        self.assertEqual(review.state, LegacyCourseOwnershipReview.STATE_LINKED)
        self.assertEqual(review.linked_membership, owner)
        self.assertIsNotNone(review.reviewed_at)
        self.assertTrue(course.legacy_password_login_enabled)

    def test_cross_institution_course_conflict_rolls_back_every_new_row(self):
        other = Institution.objects.create(slug='other', name='Other University')
        Course.objects.create(
            course_id='other-course',
            course_name='Other Course',
            instructor_name='Other Instructor',
            password=make_password('shared-password'),
            institution=other,
            legacy_password_login_enabled=True,
        )
        counts_before = {
            'users': get_user_model().objects.count(),
            'accounts': InstructorAccount.objects.count(),
            'institutions': Institution.objects.count(),
            'institution_memberships': InstitutionMembership.objects.count(),
            'course_memberships': CourseMembership.objects.count(),
        }

        with self.assertRaises(CommandError) as error:
            self.run_command('other-course')

        self.assertIn('belongs to institution other', str(error.exception))
        self.assertEqual(get_user_model().objects.count(), counts_before['users'])
        self.assertEqual(InstructorAccount.objects.count(), counts_before['accounts'])
        self.assertEqual(Institution.objects.count(), counts_before['institutions'])
        self.assertEqual(
            InstitutionMembership.objects.count(),
            counts_before['institution_memberships'],
        )
        self.assertEqual(CourseMembership.objects.count(), counts_before['course_memberships'])

    def test_existing_email_fails_without_resetting_existing_password(self):
        user = get_user_model().objects.create_user(
            username='teacher@example.edu',
            email='teacher@example.edu',
            password='ExistingPass123!',
        )
        InstructorAccount.objects.create(
            user=user,
            email='teacher@example.edu',
            display_name='Existing Instructor',
        )

        with self.assertRaises(CommandError) as error:
            self.run_command()

        self.assertIn('already exists', str(error.exception))
        user.refresh_from_db()
        self.assertTrue(user.check_password('ExistingPass123!'))
        self.assertEqual(InstructorAccount.objects.count(), 1)
