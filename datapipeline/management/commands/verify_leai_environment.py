import json

from django.conf import settings
from django.core import checks
from django.core.management.base import BaseCommand, CommandError

from datapipeline.database_schema import (
    DatabaseSchemaError,
    require_environment_database_schema,
)
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

        failure_level = (
            checks.WARNING
            if expected_environment in {'qa', 'production'}
            else checks.ERROR
        )
        blocking_issues = [
            issue
            for issue in checks.run_checks(include_deployment_checks=True)
            if issue.level >= failure_level
        ]
        if blocking_issues:
            issue_ids = ', '.join(sorted({issue.id for issue in blocking_issues}))
            raise CommandError(f'Deployment checks failed: {issue_ids}')

        try:
            database_schema = require_environment_database_schema(expected_environment)
        except DatabaseSchemaError:
            raise CommandError('Database schema verification failed.') from None

        identity = environment_identity(database_schema=database_schema)
        if options['as_json']:
            self.stdout.write(json.dumps(identity, sort_keys=True))
            return
        self.stdout.write(
            'LEAI environment verified: '
            f"{identity['environment']} "
            f"(build {identity['build_id']}, "
            f"email_enabled={str(identity['email_enabled']).lower()})"
        )
