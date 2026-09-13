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
    Institution,
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
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='UC Santa Cruz',
        )
        self.course = Course.objects.create(
            course_id='cmpm80k-sm26',
            course_name='CMPM 80K',
            instructor_name='Prof. Writer',
            password='legacy-password',
            institution=self.institution,
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

    def test_writer_creates_requested_event_id_and_rejects_collision(self):
        event_id = uuid.UUID('78ce94a4-a6f1-4ab0-99fe-acde0ebc9479')
        original = record_instructor_event(
            event_id=event_id,
            action=InstructorAuditEvent.ACTION_COURSE_CREATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=self.account,
            course=self.course,
            target_type='course',
            target_id=self.course.course_id,
            metadata={'institution_slug': self.institution.slug},
        )

        with self.assertRaises(ValidationError):
            record_instructor_event(
                event_id=event_id,
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                actor=self.account,
                course=self.course,
                target_type='course',
                target_id=self.course.course_id,
                metadata={'institution_slug': self.institution.slug},
            )

        original.refresh_from_db()
        self.assertEqual(original.event_id, event_id)
        self.assertEqual(original.target_id, self.course.course_id)
        self.assertEqual(InstructorAuditEvent.objects.filter(event_id=event_id).count(), 1)

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
            metadata={'changed_fields': ['name', 'survey_label']},
        )

        self.assertEqual(event.metadata, {
            'changed_fields': ['name', 'survey_label'],
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

    def test_writer_rejects_unrestricted_text_through_allowed_scalar_keys(self):
        invalid_cases = [
            (
                InstructorAuditEvent.ACTION_COURSE_CREATED,
                {'institution_slug': 'Bearer raw-token'},
            ),
            (
                InstructorAuditEvent.ACTION_COURSE_CREATED,
                {'institution_slug': 'other-institution'},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_CREATED,
                {'mode': 'student feedback excerpt'},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_CLONED,
                {'source_survey_id': 'TemporaryPass123!'},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_DELETED,
                {'responses_deleted': 'analysis output'},
            ),
            (
                InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED,
                {'scope_kind': 'system prompt'},
            ),
            (
                InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
                {'reason_code': 'Authorization: Bearer secret'},
            ),
        ]

        for action, metadata in invalid_cases:
            with self.subTest(action=action, metadata=metadata):
                with self.assertRaises(ValidationError):
                    record_instructor_event(
                        action=action,
                        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                        course=self.course,
                        metadata=metadata,
                    )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_unrestricted_or_mismatched_targets(self):
        invalid_targets = [
            (
                InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                'prompt',
                'student feedback excerpt',
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                'survey',
                'Bearer raw-token',
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                'survey',
                '42',
            ),
            (
                InstructorAuditEvent.ACTION_COURSE_CREATED,
                'course',
                'another-course',
            ),
            (
                InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED,
                'analysis_session',
                '58df1de8-264d-4c35-a0bf-3e90d75ee16c',
            ),
            (
                InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED,
                '',
                'raw-token',
            ),
        ]

        for action, target_type, target_id in invalid_targets:
            with self.subTest(
                action=action,
                target_type=target_type,
                target_id=target_id,
            ):
                with self.assertRaises(ValidationError):
                    record_instructor_event(
                        action=action,
                        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                        course=self.course,
                        target_type=target_type,
                        target_id=target_id,
                    )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_accepts_typed_fixed_scalar_and_target_values(self):
        course_event = record_instructor_event(
            action=InstructorAuditEvent.ACTION_COURSE_CREATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            course=self.course,
            target_type='course',
            target_id=self.course.course_id,
            metadata={'institution_slug': self.institution.slug},
        )
        survey_event = record_instructor_event(
            action=InstructorAuditEvent.ACTION_SURVEY_CLONED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            course=self.course,
            target_type='survey',
            target_id=42,
            metadata={'source_survey_id': 41},
        )
        analysis_event = record_instructor_event(
            action=InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            course=self.course,
            target_type='analysis_session',
            target_id=uuid.UUID('58df1de8-264d-4c35-a0bf-3e90d75ee16c'),
        )

        self.assertEqual(course_event.target_id, self.course.course_id)
        self.assertEqual(survey_event.target_id, '42')
        self.assertEqual(
            analysis_event.target_id,
            '58df1de8-264d-4c35-a0bf-3e90d75ee16c',
        )

    def test_writer_accepts_fixed_scalar_domains(self):
        valid_cases = [
            (
                InstructorAuditEvent.ACTION_SURVEY_CREATED,
                {'mode': 'general'},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_STATUS_CHANGED,
                {'status': 'closed'},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_DELETED,
                {'responses_deleted': 0},
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_RESPONSES_EXPORTED,
                {'row_count': 1},
            ),
            (
                InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED,
                {'scope_kind': 'week'},
            ),
            (
                InstructorAuditEvent.ACTION_PDF_INGEST_STARTED,
                {'file_count': 2},
            ),
            (
                InstructorAuditEvent.ACTION_PDF_INGEST_COMMITTED,
                {'student_count': 3, 'message_count': 4},
            ),
            (
                InstructorAuditEvent.ACTION_PDF_INGEST_REVERTED,
                {'deleted_count': 5},
            ),
            (
                InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
                {'reason_code': 'course_access_denied'},
            ),
        ]

        for action, metadata in valid_cases:
            with self.subTest(action=action, metadata=metadata):
                record_instructor_event(
                    action=action,
                    outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                    metadata=metadata,
                )

        self.assertEqual(InstructorAuditEvent.objects.count(), len(valid_cases))

    def test_writer_rejects_boolean_or_negative_counts(self):
        invalid_counts = [True, -1]

        for value in invalid_counts:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    record_instructor_event(
                        action=InstructorAuditEvent.ACTION_SURVEY_DELETED,
                        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                        metadata={'responses_deleted': value},
                    )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_rejects_well_formed_changed_fields_for_the_wrong_action(self):
        invalid_cases = [
            (
                InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED,
                'survey_label',
            ),
            (
                InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED,
                'banner_enabled',
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                'banner_enabled',
            ),
            (
                InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED,
                'name',
            ),
            (
                InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED,
                'title',
            ),
        ]

        for action, field_name in invalid_cases:
            with self.subTest(action=action, field_name=field_name):
                with self.assertRaises(ValidationError):
                    record_instructor_event(
                        action=action,
                        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                        metadata={'changed_fields': [field_name]},
                    )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_writer_accepts_action_specific_changed_fields(self):
        valid_cases = [
            (
                InstructorAuditEvent.ACTION_PROFILE_UPDATED,
                'email',
            ),
            (
                InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED,
                'banner_enabled',
            ),
            (
                InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED,
                'bot_display_name',
            ),
            (
                InstructorAuditEvent.ACTION_SURVEY_UPDATED,
                'survey_label',
            ),
            (
                InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED,
                'scope_kind',
            ),
            (
                InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED,
                'teams',
            ),
        ]

        for action, field_name in valid_cases:
            with self.subTest(action=action, field_name=field_name):
                record_instructor_event(
                    action=action,
                    outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                    metadata={'changed_fields': [field_name]},
                )

        self.assertEqual(InstructorAuditEvent.objects.count(), len(valid_cases))
