import secrets
import string
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from datapipeline.instructor_auth import (
    is_allowed_instructor_email,
    normalize_instructor_email,
)
from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    LegacyCourseOwnershipReview,
)


def _temporary_password():
    alphabet = string.ascii_letters + string.digits + '!@#$%'
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice('!@#$%'),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(16)]
    characters = required + remaining
    secrets.SystemRandom().shuffle(characters)
    return ''.join(characters)


class Command(BaseCommand):
    help = 'Provision a manually approved LEAI instructor and optional legacy-course ownership.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--display-name', default='')
        parser.add_argument('--institution-slug', default='ucsc')
        parser.add_argument(
            '--institution-name',
            default='University of California, Santa Cruz',
        )
        parser.add_argument('--course-id', action='append', default=[])

    def handle(self, *args, **options):
        email = normalize_instructor_email(options['email'])
        display_name = str(options['display_name'] or '').strip() or email.partition('@')[0]
        institution_slug = slugify(options['institution_slug'] or '')
        institution_name = str(options['institution_name'] or '').strip()
        course_ids = list(dict.fromkeys(
            str(value or '').strip() for value in (options.get('course_id') or [])
            if str(value or '').strip()
        ))

        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError('A valid instructor email is required.') from exc
        if not is_allowed_instructor_email(email):
            raise CommandError('A valid @ucsc.edu instructor email is required.')
        if not display_name:
            raise CommandError('A display name is required.')
        if not institution_slug or not institution_name:
            raise CommandError('Institution slug and name are required.')
        if InstructorAccount.objects.filter(email=email).exists():
            raise CommandError(f'Instructor account {email} already exists.')

        temporary_password = _temporary_password()
        try:
            with transaction.atomic():
                institution, _created = Institution.objects.get_or_create(
                    slug=institution_slug,
                    defaults={'name': institution_name},
                )
                if not institution.is_active:
                    raise CommandError(f'Institution {institution_slug} is inactive.')

                user = get_user_model().objects.create_user(
                    username=f'leai_{uuid.uuid4().hex}',
                    email=email,
                    password=temporary_password,
                )
                account = InstructorAccount.objects.create(
                    user=user,
                    email=email,
                    display_name=display_name,
                    auth_provider=InstructorAccount.AUTH_MANUAL,
                    must_change_password=True,
                )
                institution_membership = InstitutionMembership.objects.create(
                    institution=institution,
                    instructor=account,
                    role=InstitutionMembership.ROLE_MEMBER,
                )

                for course_id in course_ids:
                    try:
                        course = Course.objects.select_for_update().get(course_id=course_id)
                    except Course.DoesNotExist as exc:
                        raise CommandError(f'Course {course_id} does not exist.') from exc
                    if course.institution_id not in (None, institution.pk):
                        raise CommandError(
                            f'Course {course_id} belongs to institution '
                            f'{course.institution.slug}, not {institution.slug}.'
                        )
                    existing_owner = course.memberships.filter(
                        role=CourseMembership.ROLE_OWNER,
                        is_active=True,
                    ).select_related(
                        'institution_membership__instructor',
                    ).first()
                    if existing_owner is not None:
                        raise CommandError(
                            f'Course {course_id} already has active owner '
                            f'{existing_owner.institution_membership.instructor.email}.'
                        )
                    if course.institution_id is None:
                        course.institution = institution
                        course.save(update_fields=['institution'])

                    owner = CourseMembership.objects.create(
                        course=course,
                        institution_membership=institution_membership,
                        role=CourseMembership.ROLE_OWNER,
                    )
                    LegacyCourseOwnershipReview.objects.update_or_create(
                        course=course,
                        defaults={
                            'state': LegacyCourseOwnershipReview.STATE_LINKED,
                            'linked_membership': owner,
                            'notes': 'Manually reviewed during instructor provisioning.',
                            'reviewed_at': timezone.now(),
                        },
                    )
        except IntegrityError as exc:
            raise CommandError('Instructor provisioning conflicted with existing data.') from exc

        self.stdout.write(self.style.SUCCESS(f'Provisioned LEAI instructor: {email}'))
        self.stdout.write(f'Temporary password: {temporary_password}')
        self.stdout.write('The instructor must change this password at first login.')
