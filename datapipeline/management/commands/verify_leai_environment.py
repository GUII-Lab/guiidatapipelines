import json

from django.conf import settings
from django.core import checks
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection

from datapipeline.environment_views import environment_identity


class Command(BaseCommand):
    help = 'Verify the LEAI deployment environment and default database.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            '--expect',
            required=True,
            choices=('local', 'qa', 'production'),
        )
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        expected_environment = options['expect']
        if settings.LEAI_ENV != expected_environment:
            raise CommandError(
                f'Expected LEAI environment {expected_environment}; '
                'the active environment differs.'
            )

        errors = [
            issue
            for issue in checks.run_checks(include_deployment_checks=True)
            if issue.level >= checks.ERROR
        ]
        if errors:
            error_ids = ', '.join(sorted({issue.id for issue in errors}))
            raise CommandError(f'Deployment checks failed: {error_ids}')

        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                result = cursor.fetchone()
        except DatabaseError as exc:
            raise CommandError('Default database verification failed.') from exc
        if not result or result[0] != 1:
            raise CommandError('Default database verification failed.')

        identity = environment_identity()
        if options['as_json']:
            self.stdout.write(json.dumps(identity, sort_keys=True))
            return
        self.stdout.write(
            'LEAI environment verified: '
            f"{identity['environment']} "
            f"(build {identity['build_id']}, "
            f"email_enabled={str(identity['email_enabled']).lower()})"
        )
