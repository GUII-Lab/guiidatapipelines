"""Generate metadata-only evidence for the canonical LEAI schema."""

import argparse
import os
import sys
from pathlib import Path


def list_canonical_migrations(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM django_migrations WHERE app = %s ORDER BY name",
            ["leai"],
        )
        return [name for (name,) in cursor.fetchall()]


def count_forbidden_legacy_foreign_keys(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM pg_constraint relation
            JOIN pg_class canonical ON canonical.oid = relation.conrelid
            JOIN pg_namespace namespace ON namespace.oid = canonical.relnamespace
            JOIN pg_class legacy ON legacy.oid = relation.confrelid
            WHERE relation.contype = 'f'
              AND namespace.nspname = current_schema()
              AND left(canonical.relname, 5) = 'leai_'
              AND left(legacy.relname, 13) = 'datapipeline_'
            """
        )
        return cursor.fetchone()[0]


def schema_counts(connection):
    """Count canonical schema objects without reading application rows."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE item.relkind IN ('r', 'p')),
              COUNT(*) FILTER (WHERE item.relkind IN ('i', 'I'))
            FROM pg_class item
            JOIN pg_namespace namespace ON namespace.oid = item.relnamespace
            WHERE namespace.nspname = current_schema()
              AND left(item.relname, 5) = 'leai_'
            """
        )
        tables, indexes = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM pg_constraint constraint_row
            JOIN pg_class canonical ON canonical.oid = constraint_row.conrelid
            JOIN pg_namespace namespace ON namespace.oid = canonical.relnamespace
            WHERE namespace.nspname = current_schema()
              AND left(canonical.relname, 5) = 'leai_'
            """
        )
        constraints = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT canonical.relname, attribute.attname
            FROM pg_class canonical
            JOIN pg_namespace namespace ON namespace.oid = canonical.relnamespace
            JOIN pg_attribute attribute ON attribute.attrelid = canonical.oid
            JOIN pg_type column_type ON column_type.oid = attribute.atttypid
            WHERE namespace.nspname = current_schema()
              AND left(canonical.relname, 5) = 'leai_'
              AND canonical.relkind IN ('r', 'p')
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND column_type.typname = 'uuid'
            ORDER BY canonical.relname, attribute.attname
            """
        )
        uuid_columns = cursor.fetchall()
    return tables, constraints, indexes, uuid_columns


def render_schema_report(connection, verification_results=()):
    tables, constraints, indexes, uuid_columns = schema_counts(connection)
    migrations = list_canonical_migrations(connection)
    forbidden = count_forbidden_legacy_foreign_keys(connection)
    lines = [
        "# LEAI canonical schema foundation verification",
        "",
        "Metadata only; no application rows or credentials.",
        "",
        "## Schema objects",
        "",
        f"- Applied migrations ({len(migrations)}): {', '.join(migrations)}",
        f"- Canonical tables: {tables}",
        f"- Constraints: {constraints}",
        f"- Indexes: {indexes}",
        f"- Public UUID columns: {', '.join(f'{table}.{column}' for table, column in uuid_columns)}",
        f"- Forbidden legacy foreign keys: {forbidden}",
        "",
        "## Verification commands and results",
        "",
    ]
    lines.extend(f"- `{command}` — {result}" for command, result in verification_results)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-database", required=True)
    parser.add_argument(
        "--verification",
        nargs=2,
        action="append",
        metavar=("COMMAND", "RESULT"),
        default=[],
    )
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guiidatapipelines.settings")
    import django
    from django.db import connection

    django.setup()
    actual_database = connection.settings_dict["NAME"]
    if actual_database != args.expect_database:
        parser.error("Database does not match --expect-database")
    if len(list_canonical_migrations(connection)) != 9:
        parser.error("Expected exactly nine applied canonical migrations")
    if count_forbidden_legacy_foreign_keys(connection):
        parser.error("Canonical schema has forbidden legacy foreign keys")
    print(render_schema_report(connection, args.verification), end="")


if __name__ == "__main__":
    main()
