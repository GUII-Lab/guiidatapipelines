import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from datapipeline.instructor_audit import (
    ALLOWED_METADATA_KEYS,
    record_instructor_event,
)
from datapipeline.models import (
    Course,
    InstructorAccount,
    InstructorAuditEvent,
    InstructorSession,
)


class InstructorAuditModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='teacher@example.edu',
            email='teacher@example.edu',
            password='TemporaryPass123!',
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email='teacher@example.edu',
            display_name='Prof. Test',
        )
        self.session = InstructorSession.objects.create(
            instructor=self.account,
            token_digest='a' * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.course = Course.objects.create(
            course_id='cmpm80k-sm26',
            course_name='CMPM 80K',
            instructor_name='Prof. Test',
            password='legacy-password',
        )

    def create_event(self, **overrides):
        values = {
            'actor': self.account,
            'session': self.session,
            'course': self.course,
            'course_id_snapshot': self.course.course_id,
            'action': InstructorAuditEvent.ACTION_COURSE_CREATED,
            'outcome': InstructorAuditEvent.OUTCOME_SUCCESS,
            'target_type': 'course',
            'target_id': self.course.course_id,
            'metadata': {'institution_slug': 'ucsc'},
        }
        values.update(overrides)
        return InstructorAuditEvent.objects.create(**values)

    def test_event_keeps_actor_course_and_stable_course_snapshot(self):
        event = self.create_event()

        self.assertIsInstance(event.event_id, uuid.UUID)
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.session, self.session)
        self.assertEqual(event.course, self.course)
        self.assertEqual(event.course_id_snapshot, 'cmpm80k-sm26')
        self.assertEqual(event.target_type, 'course')
        self.assertEqual(event.target_id, 'cmpm80k-sm26')
        self.assertEqual(event.metadata, {'institution_slug': 'ucsc'})

    def test_events_are_ordered_newest_first(self):
        older = self.create_event(target_id='older')
        newer = self.create_event(target_id='newer')
        InstructorAuditEvent.objects.filter(pk=older.pk).update(
            occurred_at=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(
            list(InstructorAuditEvent.objects.values_list('pk', flat=True)),
            [newer.pk, older.pk],
        )

    def test_action_must_be_a_declared_choice(self):
        event = InstructorAuditEvent(
            action='course.not_a_real_action',
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        )

        with self.assertRaisesMessage(ValidationError, 'not a valid choice'):
            event.full_clean()

    def test_outcome_must_be_a_declared_choice(self):
        event = InstructorAuditEvent(
            action=InstructorAuditEvent.ACTION_COURSE_CREATED,
            outcome='unknown',
        )

        with self.assertRaisesMessage(ValidationError, 'not a valid choice'):
            event.full_clean()

    def test_actor_is_protected_while_session_and_course_can_be_removed(self):
        event = self.create_event()

        with self.assertRaises(ProtectedError):
            self.account.delete()

        self.session.delete()
        self.course.delete()
        event.refresh_from_db()

        self.assertEqual(event.actor, self.account)
        self.assertIsNone(event.session)
        self.assertIsNone(event.course)
        self.assertEqual(event.course_id_snapshot, 'cmpm80k-sm26')

    def test_indexes_cover_expected_audit_queries(self):
        indexes = {
            index.name: index.fields
            for index in InstructorAuditEvent._meta.indexes
        }

        self.assertEqual(indexes, {
            'leai_audit_actor_time': ['actor', '-occurred_at'],
            'leai_audit_course_time': ['course', '-occurred_at'],
            'leai_audit_action_time': ['action', 'outcome', '-occurred_at'],
        })


class InstructorAuditWriterTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username='writer@example.edu',
            email='writer@example.edu',
            password='TemporaryPass123!',
        )
        self.account = InstructorAccount.objects.create(
            user=user,
            email='writer@example.edu',
            display_name='Prof. Writer',
        )
        self.course = Course.objects.create(
            course_id='cmpm80k-sm26',
            course_name='CMPM 80K',
            instructor_name='Prof. Writer',
            password='legacy-password',
        )

    def test_writer_exposes_allow_list_for_every_declared_action(self):
        self.assertEqual(
            set(ALLOWED_METADATA_KEYS),
            {value for value, _label in InstructorAuditEvent.ACTION_CHOICES},
        )

    def test_writer_rejects_unknown_metadata_keys(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                actor=self.account,
                metadata={'password': 'must-not-land'},
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_derives_course_snapshot(self):
        event = record_instructor_event(
            action=InstructorAuditEvent.ACTION_COURSE_CREATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=self.account,
            course=self.course,
            metadata={'institution_slug': 'ucsc'},
        )

        self.assertEqual(event.course_id_snapshot, self.course.course_id)

    def test_writer_rejects_non_dict_metadata(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                metadata=['institution_slug'],
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_nested_metadata_objects(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                metadata={'institution_slug': {'value': 'ucsc'}},
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_lists_outside_changed_fields(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                metadata={'institution_slug': ['ucsc']},
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_strings_longer_than_100_characters(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                metadata={'institution_slug': 'x' * 101},
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_accepts_bounded_changed_field_names(self):
        event = record_instructor_event(
            action=InstructorAuditEvent.ACTION_SURVEY_UPDATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            metadata={'changed_fields': ['title', 'survey_status']},
        )

        self.assertEqual(event.metadata, {
            'changed_fields': ['title', 'survey_status'],
        })

    def test_writer_rejects_non_field_names_in_changed_fields(self):
        invalid_values = [
            ['display name'],
            ['x' * 101],
            [['nested']],
            [{'nested': 'object'}],
        ]

        for changed_fields in invalid_values:
            with self.subTest(changed_fields=changed_fields):
                with self.assertRaises(ValidationError):
                    record_instructor_event(
                        action=InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                        metadata={'changed_fields': changed_fields},
                    )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_unbounded_changed_fields(self):
        with self.assertRaises(ValidationError):
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                metadata={
                    'changed_fields': [f'field_{index}' for index in range(101)],
                },
            )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_invalid_action_and_outcome(self):
        invalid_pairs = [
            ('not.a.real.action', InstructorAuditEvent.OUTCOME_SUCCESS),
            (InstructorAuditEvent.ACTION_COURSE_CREATED, 'not-an-outcome'),
        ]

        for action, outcome in invalid_pairs:
            with self.subTest(action=action, outcome=outcome):
                with self.assertRaises(ValidationError):
                    record_instructor_event(action=action, outcome=outcome)

        self.assertFalse(InstructorAuditEvent.objects.exists())
