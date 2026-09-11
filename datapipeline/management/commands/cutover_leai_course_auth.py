from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from datapipeline.models import Course, CourseMembership


class Command(BaseCommand):
    help = 'Disable legacy password login for one validated LEAI course.'

    def add_arguments(self, parser):
        parser.add_argument('--course-id', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        course_id = str(options.get('course_id') or '').strip()
        if not course_id:
            raise CommandError('--course-id must name one explicit course.')

        with transaction.atomic():
            try:
                course = (
                    Course.objects
                    .select_for_update()
                    .get(course_id=course_id)
                )
            except Course.DoesNotExist as exc:
                raise CommandError(f'Course {course_id} does not exist.') from exc

            if course.institution_id is None:
                raise CommandError(f'Course {course_id} has no institution.')
            if not course.institution.is_active:
                raise CommandError(
                    f'Course {course_id} institution '
                    f'{course.institution.slug} is inactive.'
                )

            owner = (
                course.memberships
                .filter(
                    role=CourseMembership.ROLE_OWNER,
                    is_active=True,
                )
                .select_related(
                    'institution_membership',
                    'institution_membership__institution',
                    'institution_membership__instructor',
                    'institution_membership__instructor__user',
                )
                .first()
            )
            if owner is None:
                raise CommandError(f'Course {course_id} has no active owner.')

            institution_membership = owner.institution_membership
            if institution_membership.institution_id != course.institution_id:
                raise CommandError(
                    'The active owner institution membership does not match '
                    f'course {course_id}.'
                )
            if not institution_membership.is_active:
                raise CommandError(
                    'The active owner institution membership is inactive.'
                )

            account = institution_membership.instructor
            if not account.is_active:
                raise CommandError('The active owner instructor account is inactive.')
            if not account.user.is_active:
                raise CommandError('The active owner Django user is inactive.')

            self.stdout.write(
                f'Course: {course.course_id} ({course.course_name})'
            )
            self.stdout.write(
                f'Institution: {course.institution.slug} '
                f'({course.institution.name})'
            )
            self.stdout.write(f'Owner: {account.email}')

            if options.get('dry_run'):
                self.stdout.write(
                    'Dry run: legacy password login would be disabled.'
                )
                return

            if not course.legacy_password_login_enabled:
                self.stdout.write(
                    f'Course {course_id} already uses instructor account authentication.'
                )
                return

            course.legacy_password_login_enabled = False
            course.save(update_fields=['legacy_password_login_enabled'])
            self.stdout.write(self.style.SUCCESS(
                f'Disabled legacy password login for {course_id}.'
            ))
