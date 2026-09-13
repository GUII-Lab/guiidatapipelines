from django.conf import settings
from django.db import DatabaseError, connection


_EXPECTED_DATABASE_SCHEMAS = {
    'local': 'public',
    'qa': 'leai_qa',
    'production': 'public',
}


class DatabaseSchemaError(RuntimeError):
    pass


def expected_database_schema(environment) -> str | None:
    return _EXPECTED_DATABASE_SCHEMAS.get(environment)


def active_database_schema() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_schema()')
            row = cursor.fetchone()
    except DatabaseError:
        raise DatabaseSchemaError('Database schema verification failed.') from None
    if not row or not isinstance(row[0], str) or not row[0]:
        raise DatabaseSchemaError('Database schema verification failed.')
    return row[0]


def require_environment_database_schema(expected_environment) -> str:
    expected_schema = expected_database_schema(expected_environment)
    configured_environment = getattr(settings, 'LEAI_ENV', None)
    configured_schema = getattr(settings, 'LEAI_DB_SCHEMA', None)
    if (
        expected_schema is None
        or configured_environment != expected_environment
        or configured_schema != expected_schema
    ):
        raise DatabaseSchemaError('Database schema verification failed.')

    active_schema = active_database_schema()
    if active_schema != expected_schema:
        raise DatabaseSchemaError('Database schema verification failed.')
    return active_schema
