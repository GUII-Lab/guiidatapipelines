from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection

from datapipeline.database_schema import (
    DatabaseSchemaError,
    expected_database_schema,
    require_environment_database_schema,
)


class Command(BaseCommand):
    help = 'Prepare and verify the fixed LEAI database schema.'
    requires_system_checks = []

    def handle(self, *args, **options):
        environment = getattr(settings, 'LEAI_ENV', None)
        expected_schema = expected_database_schema(environment)
        if (
            expected_schema is None
            or getattr(settings, 'LEAI_DB_SCHEMA', None) != expected_schema
        ):
            raise CommandError('Database schema verification failed.')

        if environment == 'qa':
            try:
                with connection.cursor() as cursor:
                    cursor.execute('CREATE SCHEMA IF NOT EXISTS "leai_qa"')
            except DatabaseError:
                raise CommandError('Database schema verification failed.') from None

        try:
            require_environment_database_schema(environment)
        except DatabaseSchemaError:
            raise CommandError('Database schema verification failed.') from None
