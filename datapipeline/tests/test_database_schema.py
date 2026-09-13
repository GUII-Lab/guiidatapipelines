import traceback
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import SimpleTestCase, override_settings


class DatabaseSchemaTests(SimpleTestCase):
    databases = {'default'}

    def schema_module(self):
        try:
            from datapipeline import database_schema
        except ImportError:
            self.fail('database schema helper must exist')
        return database_schema

    def schema_cursor(self, result=None):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = result
        return cursor

    def test_expected_schema_maps_only_known_environments(self):
        schema = self.schema_module()

        self.assertEqual(schema.expected_database_schema('local'), 'public')
        self.assertEqual(schema.expected_database_schema('qa'), 'leai_qa')
        self.assertEqual(schema.expected_database_schema('production'), 'public')
        self.assertIsNone(schema.expected_database_schema('staging'))

    def test_active_schema_reads_only_the_current_schema_identity(self):
        schema = self.schema_module()
        cursor = self.schema_cursor(('leai_qa',))
        with patch.object(schema.connection, 'cursor', return_value=cursor):
            active = schema.active_database_schema()

        self.assertEqual(active, 'leai_qa')
        cursor.execute.assert_called_once_with('SELECT current_schema()')

    @override_settings(LEAI_ENV='qa', LEAI_DB_SCHEMA='leai_qa')
    def test_require_schema_accepts_exact_configured_and_active_match(self):
        schema = self.schema_module()
        cursor = self.schema_cursor(('leai_qa',))
        with patch.object(schema.connection, 'cursor', return_value=cursor):
            active = schema.require_environment_database_schema('qa')

        self.assertEqual(active, 'leai_qa')
        cursor.execute.assert_called_once_with('SELECT current_schema()')

    @override_settings(LEAI_ENV='qa', LEAI_DB_SCHEMA='public')
    def test_require_schema_rejects_configured_mismatch_before_a_query(self):
        schema = self.schema_module()
        with patch.object(schema.connection, 'cursor') as cursor:
            with self.assertRaisesRegex(
                schema.DatabaseSchemaError,
                'Database schema',
            ):
                schema.require_environment_database_schema('qa')

        cursor.assert_not_called()

    @override_settings(LEAI_ENV='qa', LEAI_DB_SCHEMA='leai_qa')
    def test_require_schema_rejects_missing_public_and_multiple_active_schemas(self):
        schema = self.schema_module()
        for active in (None, 'public', 'leai_qa,public'):
            with self.subTest(active=active):
                cursor = self.schema_cursor(
                    None if active is None else (active,),
                )
                with patch.object(schema.connection, 'cursor', return_value=cursor):
                    with self.assertRaisesRegex(
                        schema.DatabaseSchemaError,
                        'Database schema',
                    ):
                        schema.require_environment_database_schema('qa')
                cursor.execute.assert_called_once_with('SELECT current_schema()')

    def test_active_schema_suppresses_secret_bearing_database_errors(self):
        schema = self.schema_module()
        with patch.object(
            schema.connection,
            'cursor',
            side_effect=DatabaseError('DATABASE_URL=postgres://secret-bearing-error'),
        ):
            with self.assertRaises(schema.DatabaseSchemaError) as raised:
                schema.active_database_schema()

        error = raised.exception
        rendered_traceback = ''.join(traceback.format_exception(error))
        self.assertEqual(str(error), 'Database schema verification failed.')
        self.assertIsNone(error.__cause__)
        self.assertIs(error.__suppress_context__, True)
        self.assertNotIn('secret-bearing-error', rendered_traceback)


class PrepareLeaiDatabaseCommandTests(SimpleTestCase):
    databases = {'default'}

    def schema_module(self):
        try:
            from datapipeline import database_schema
        except ImportError:
            self.fail('database schema helper must exist')
        return database_schema

    @override_settings(LEAI_ENV='qa', LEAI_DB_SCHEMA='leai_qa')
    def test_prepare_qa_creates_only_fixed_schema_then_verifies_identity(self):
        self.schema_module()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        with patch(
            'datapipeline.management.commands.prepare_leai_database.connection.cursor',
            return_value=cursor,
            create=True,
        ), patch(
            'datapipeline.management.commands.prepare_leai_database.require_environment_database_schema',
            return_value='leai_qa',
            create=True,
        ) as schema_guard:
            call_command('prepare_leai_database')

        cursor.execute.assert_called_once_with('CREATE SCHEMA IF NOT EXISTS "leai_qa"')
        schema_guard.assert_called_once_with('qa')

    @override_settings(LEAI_ENV='production', LEAI_DB_SCHEMA='public')
    def test_prepare_production_creates_nothing_and_verifies_public(self):
        self.schema_module()
        with patch(
            'datapipeline.management.commands.prepare_leai_database.connection.cursor',
            create=True,
        ) as cursor, patch(
            'datapipeline.management.commands.prepare_leai_database.require_environment_database_schema',
            return_value='public',
            create=True,
        ) as schema_guard:
            call_command('prepare_leai_database')

        cursor.assert_not_called()
        schema_guard.assert_called_once_with('production')

    @override_settings(LEAI_ENV='qa', LEAI_DB_SCHEMA='public')
    def test_prepare_rejects_invalid_configuration_before_sql(self):
        self.schema_module()
        with patch(
            'datapipeline.management.commands.prepare_leai_database.connection.cursor',
            create=True,
        ) as cursor:
            with self.assertRaisesRegex(CommandError, 'Database schema verification failed'):
                call_command('prepare_leai_database')

        cursor.assert_not_called()
