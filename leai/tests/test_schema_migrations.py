from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from leai.models import Institution
from scripts.report_leai_schema import (
    count_forbidden_legacy_foreign_keys,
    list_canonical_migrations,
    render_schema_report,
    validate_canonical_migrations,
)


class SchemaMigrationTests(TransactionTestCase):
    def test_report_rejects_missing_or_substituted_migration(self):
        expected = [
            "0001_identity_course",
            "0002_identity_integrity_guards",
            "0003_lock_child_institution_parents",
            "0004_authoring_core",
            "0005_authoring_publication",
            "0006_responses",
            "0007_mutation_receipt",
            "0008_analysis_governance",
            "0009_product_usage_event",
        ]
        self.assertIsNone(validate_canonical_migrations(expected))
        with self.assertRaises(ValueError):
            validate_canonical_migrations(expected[:-1])
        with self.assertRaises(ValueError):
            validate_canonical_migrations(expected[:-1] + ["0009_wrong_migration"])

    def test_report_contains_schema_evidence_but_no_private_data_rows(self):
        private_name = "private-instructor-course-data-should-not-leak"
        Institution.objects.create(slug="schema-report-probe", name=private_name)

        report = render_schema_report(connection)

        self.assertIn("0009_product_usage_event", report)
        self.assertIn("Canonical tables:", report)
        self.assertIn("Constraints:", report)
        self.assertIn("Indexes:", report)
        self.assertIn("Public UUID columns:", report)
        self.assertIn("Forbidden legacy foreign keys: 0", report)
        self.assertNotIn("Verification commands and results", report)
        self.assertNotIn(private_name, report)

    def test_detector_finds_a_canonical_foreign_key_into_a_legacy_table(self):
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE datapipeline_schema_probe (id bigint PRIMARY KEY)")
            cursor.execute(
                "CREATE TABLE leai_schema_probe ("
                "id bigint PRIMARY KEY, "
                "legacy_id bigint REFERENCES datapipeline_schema_probe(id))"
            )
        try:
            self.assertEqual(count_forbidden_legacy_foreign_keys(connection), 1)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE leai_schema_probe")
                cursor.execute("DROP TABLE datapipeline_schema_probe")

    def test_canonical_schema_has_no_legacy_foreign_keys(self):
        self.assertEqual(count_forbidden_legacy_foreign_keys(connection), 0)

    def test_fresh_database_keeps_the_nine_foundation_migrations_as_prefix(self):
        self.assertEqual(
            list_canonical_migrations(connection)[:9],
            [
                "0001_identity_course",
                "0002_identity_integrity_guards",
                "0003_lock_child_institution_parents",
                "0004_authoring_core",
                "0005_authoring_publication",
                "0006_responses",
                "0007_mutation_receipt",
                "0008_analysis_governance",
                "0009_product_usage_event",
            ],
        )

    def test_latest_expand_only_migration_reverses_in_disposable_database(self):
        current_leaf = MigrationExecutor(connection).loader.graph.leaf_nodes("leai")
        try:
            MigrationExecutor(connection).migrate([("leai", "0008_analysis_governance")])
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('leai_productusageevent')")
                self.assertIsNone(cursor.fetchone()[0])
        finally:
            MigrationExecutor(connection).migrate(current_leaf)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('leai_productusageevent')")
            self.assertIsNotNone(cursor.fetchone()[0])
