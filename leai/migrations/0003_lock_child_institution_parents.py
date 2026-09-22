from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("leai", "0002_identity_integrity_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE OR REPLACE FUNCTION leai_enforce_course_institution_membership()
                RETURNS TRIGGER AS $$
                DECLARE
                    course_institution_id bigint;
                    membership_institution_id bigint;
                BEGIN
                    SELECT institution_id
                    INTO course_institution_id
                    FROM leai_course
                    WHERE id = NEW.course_id
                    FOR UPDATE;

                    SELECT institution_id
                    INTO membership_institution_id
                    FROM leai_institutionmembership
                    WHERE id = NEW.institution_membership_id
                    FOR UPDATE;

                    IF course_institution_id IS NULL
                       OR membership_institution_id IS NULL
                       OR course_institution_id <> membership_institution_id THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'Course membership institution must match course institution';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
            """,
            reverse_sql="""
                CREATE OR REPLACE FUNCTION leai_enforce_course_institution_membership()
                RETURNS TRIGGER AS $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM leai_course AS course
                        JOIN leai_institutionmembership AS institution_membership
                          ON institution_membership.id = NEW.institution_membership_id
                        WHERE course.id = NEW.course_id
                          AND course.institution_id = institution_membership.institution_id
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'Course membership institution must match course institution';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
            """,
        ),
    ]
