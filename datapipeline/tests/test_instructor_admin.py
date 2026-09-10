from unittest.mock import Mock

from django.contrib import admin
from django.test import RequestFactory, SimpleTestCase

from datapipeline.models import (
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    InstructorAuditEvent,
    InstructorSession,
)


class InstructorAdminRegistrationTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get('/admin/')
        self.request.user = Mock()
        self.request.user.has_perm.return_value = True

    def test_all_identity_and_audit_models_are_registered(self):
        expected_models = {
            Institution,
            InstructorAccount,
            InstitutionMembership,
            InstructorSession,
            CourseMembership,
            InstructorAuditEvent,
        }

        self.assertTrue(expected_models.issubset(admin.site._registry))

    def test_audit_admin_is_immutable_and_newest_first(self):
        audit_admin = admin.site._registry[InstructorAuditEvent]
        event = InstructorAuditEvent()

        self.assertFalse(audit_admin.has_add_permission(self.request))
        self.assertFalse(
            audit_admin.has_change_permission(self.request, event),
        )
        self.assertFalse(
            audit_admin.has_delete_permission(self.request, event),
        )
        self.assertEqual(
            audit_admin.get_ordering(self.request),
            ('-occurred_at', '-id'),
        )
        self.assertEqual(
            set(audit_admin.get_readonly_fields(self.request, event)),
            {field.name for field in InstructorAuditEvent._meta.fields},
        )

    def test_audit_admin_exposes_safe_columns_filters_and_search(self):
        audit_admin = admin.site._registry[InstructorAuditEvent]

        self.assertEqual(audit_admin.list_display, (
            'event_id',
            'occurred_at',
            'actor',
            'action',
            'outcome',
            'course_id_snapshot',
            'target_type',
            'target_id',
        ))
        self.assertTrue({
            'action',
            'outcome',
            'course',
            'occurred_at',
        }.issubset(set(audit_admin.list_filter)))
        self.assertTrue({
            'actor__email',
            'course_id_snapshot',
            'target_id',
            '=event_id',
        }.issubset(set(audit_admin.search_fields)))
        self.assertTrue(audit_admin.list_select_related)

    def test_session_admin_never_displays_token_digest(self):
        session_admin = admin.site._registry[InstructorSession]
        session = InstructorSession()

        self.assertNotIn('token_digest', session_admin.list_display)
        self.assertNotIn('token_digest', session_admin.search_fields)
        self.assertIn('token_digest', session_admin.exclude)
        self.assertFalse(session_admin.has_add_permission(self.request))
        self.assertFalse(
            session_admin.has_change_permission(self.request, session),
        )
        self.assertFalse(
            session_admin.has_delete_permission(self.request, session),
        )
        self.assertEqual(session_admin.list_display, (
            'id',
            'instructor_email',
            'created_at',
            'expires_at',
            'revoked_at',
            'last_used_at',
        ))
        self.assertTrue(session_admin.list_select_related)
