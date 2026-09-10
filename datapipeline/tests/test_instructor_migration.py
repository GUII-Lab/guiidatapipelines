from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LegacyPasswordCompatibilityMigrationTests(TransactionTestCase):
    migrate_from = ('datapipeline', '0044_feedbackmessage_form_attribution')
    migrate_to = ('datapipeline', '0045_instructor_identity_foundation')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        old_course = old_apps.get_model('datapipeline', 'Course')
        old_course.objects.create(
            course_id='legacy-course',
            course_name='Legacy Course',
            instructor_name='Legacy Instructor',
            password='existing-password-hash',
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def test_existing_courses_keep_legacy_password_compatibility(self):
        course = self.apps.get_model('datapipeline', 'Course').objects.get(
            course_id='legacy-course',
        )

        self.assertTrue(course.legacy_password_login_enabled)

    def test_courses_created_after_migration_default_to_compatibility_off(self):
        course_model = self.apps.get_model('datapipeline', 'Course')
        course = course_model.objects.create(
            course_id='new-course',
            course_name='New Course',
            instructor_name='New Instructor',
            password='!',
        )

        self.assertFalse(course.legacy_password_login_enabled)
