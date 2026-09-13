import getpass
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from datapipeline.models import InstructorAccount
from datapipeline.qa_seed import (
    QA_INSTRUCTOR_EMAILS,
    QASeedError,
    seed_qa_data,
)


class Command(BaseCommand):
    help = 'Create or reset the deterministic synthetic LEAI QA dataset.'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true')
        parser.add_argument('--confirm', default='')
        parser.add_argument('--json', action='store_true', dest='as_json')
        parser.add_argument('--primary-password-env', default='')
        parser.add_argument('--reviewer-password-env', default='')

    def _password(self, *, email, environment_name, required):
        if environment_name:
            value = os.environ.get(environment_name)
            if not value:
                raise CommandError(
                    f'Credential environment variable is missing for {email}.'
                )
            return value
        if required:
            value = getpass.getpass(f'Password for {email}: ')
            if not value:
                raise CommandError(f'A runtime credential is required for {email}.')
            return value
        return None

    def handle(self, *args, **options):
        if getattr(settings, 'LEAI_ENV', None) != 'qa':
            raise CommandError('LEAI QA seed is available only when LEAI_ENV is qa.')
        reset = options['reset']
        if reset and options['confirm'] != 'qa':
            raise CommandError('QA reset requires exact confirmation: --confirm qa.')

        passwords = {}
        environment_options = (
            options['primary_password_env'],
            options['reviewer_password_env'],
        )
        for email, environment_name in zip(
            QA_INSTRUCTOR_EMAILS,
            environment_options,
            strict=True,
        ):
            required = reset or not InstructorAccount.objects.filter(email=email).exists()
            value = self._password(
                email=email,
                environment_name=environment_name,
                required=required,
            )
            if value is not None:
                passwords[email] = value

        try:
            result = seed_qa_data(
                reset=reset,
                confirm=options['confirm'] or None,
                instructor_passwords=passwords or None,
            )
        except QASeedError as exc:
            raise CommandError(str(exc)) from exc

        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True, separators=(',', ':')))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded LEAI QA data version {result['seed_version']} with exact counts."
            )
        )
        for key in sorted(key for key in result if key != 'seed_version'):
            self.stdout.write(f'{key}: {result[key]}')
