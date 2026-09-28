import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve

from leai.models import CourseMembership, InstitutionMembership
from leai.tests.session_client import SessionClient
from leai.tests.test_response_models import ResponseFixturesMixin


ROOT = "/datapipeline/api/v1/"
COURSE_PATH = "instructor_courses/{course_id}/analysis/certificates/verify/"


@override_settings(ROOT_URLCONF="guiidatapipelines.urls")
class CompletionCertificateVerifyRouteTests(SimpleTestCase):
    def test_verification_route_is_registered_under_v1(self):
        path = ROOT + COURSE_PATH.format(
            course_id="550e8400-e29b-41d4-a716-446655440000",
        )
        try:
            match = resolve(path)
        except Resolver404:
            match = None

        self.assertIsNotNone(match, "completion certificate verification route is missing")
        self.assertEqual(match.url_name, "leai-analysis-certificate-verify")


@override_settings(ROOT_URLCONF="guiidatapipelines.urls")
class CompletionCertificateVerifyApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.environment_patcher = patch(
            "leai.api.environment.verified_environment_identity",
            return_value={"environment": "local", "schema_identity": "public"},
        )
        self.environment_patcher.start()
        self.addCleanup(self.environment_patcher.stop)

        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Test-Password-Only-2026!")
        self.account.user.save(update_fields=["password"])
        membership = InstitutionMembership.objects.create(
            account=self.account,
            institution=self.institution,
            role="instructor",
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=membership,
            role="owner",
        )
        login = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({
                "email": self.account.email,
                "password": "Test-Password-Only-2026!",
            }),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)

    def verify_url(self, course=None):
        return ROOT + COURSE_PATH.format(course_id=(course or self.course).public_id)

    def verify(self, occurrence, codes, *, course=None):
        return self.client.post(
            self.verify_url(course),
            data=json.dumps({"occurrence_id": str(occurrence.public_id), "codes": codes}),
            content_type="application/json",
        )

    def enable_certificates(self, occurrence):
        occurrence.completion_certificate_enabled = True
        occurrence.save(update_fields=["completion_certificate_enabled"])

    def test_returns_boolean_per_code_in_input_order_without_private_fields(self):
        occurrence = self.make_occurrence()
        self.enable_certificates(occurrence)
        code = "ABCD-EFGH-JKLM-NPQR"
        session = self.make_student_session(
            occurrence=occurrence,
            certificate_code=code,
        )

        response = self.verify(
            occurrence,
            [" abcd efgh jklm npqr ", "WXYZ-WXYZ-WXYZ-WXYZ", code, "bad"],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"results": [True, False, True, False]})
        body = response.content.decode()
        self.assertNotIn(code, body)
        self.assertNotIn(str(session.public_id), body)
        self.assertNotIn(self.account.email, body)

    def test_accepts_one_hundred_codes_and_preserves_duplicate_positions(self):
        occurrence = self.make_occurrence()
        self.enable_certificates(occurrence)
        self.make_student_session(
            occurrence=occurrence,
            certificate_code="ABCD-EFGH-JKLM-NPQR",
        )

        response = self.verify(occurrence, ["ABCD-EFGH-JKLM-NPQR"] * 100)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": [True] * 100})

    def test_valid_code_is_scoped_to_the_exact_occurrence(self):
        first = self.make_occurrence()
        second = self.make_occurrence()
        self.enable_certificates(first)
        self.enable_certificates(second)
        self.make_student_session(occurrence=second, certificate_code="ABCD-EFGH-JKLM-NPQR")

        response = self.verify(first, ["ABCD-EFGH-JKLM-NPQR"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": [False]})

    def test_foreign_occurrence_is_indistinguishable_from_unknown_occurrence(self):
        local = self.make_occurrence()
        foreign = self.make_occurrence(course=self.other_course)
        self.enable_certificates(local)
        self.enable_certificates(foreign)
        foreign_code = "ABCD-EFGH-JKLM-NPQR"
        self.make_student_session(occurrence=foreign, certificate_code=foreign_code)
        foreign_code_at_local_scope = self.verify(local, [foreign_code])
        self.assertEqual(foreign_code_at_local_scope.status_code, 200)
        self.assertEqual(foreign_code_at_local_scope.json(), {"results": [False]})
        unknown_id = "11111111-1111-4111-8111-111111111111"

        foreign_response = self.client.post(
            self.verify_url(),
            data=json.dumps({"occurrence_id": str(foreign.public_id), "codes": ["ABCD-EFGH-JKLM-NPQR"]}),
            content_type="application/json",
        )
        unknown_response = self.client.post(
            self.verify_url(),
            data=json.dumps({"occurrence_id": unknown_id, "codes": ["ABCD-EFGH-JKLM-NPQR"]}),
            content_type="application/json",
        )

        self.assertEqual(foreign_response.status_code, 404)
        self.assertEqual(foreign_response.json(), {"error": "not_found"})
        self.assertEqual(unknown_response.status_code, 404)
        self.assertEqual(unknown_response.json(), foreign_response.json())

    def test_disabled_certificate_occurrence_is_rejected_without_results(self):
        occurrence = self.make_occurrence()

        response = self.verify(occurrence, ["ABCD-EFGH-JKLM-NPQR"])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "certificates_disabled"})

    def test_verification_requires_course_response_permission(self):
        occurrence = self.make_occurrence()
        self.enable_certificates(occurrence)
        CourseMembership.objects.all().delete()

        response = self.verify(occurrence, ["ABCD-EFGH-JKLM-NPQR"])

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "not_found"})

    def test_request_validation_authentication_and_method(self):
        occurrence = self.make_occurrence()
        self.enable_certificates(occurrence)
        url = self.verify_url()

        self.assertEqual(SessionClient().post(
            url,
            data=json.dumps({"occurrence_id": str(occurrence.public_id), "codes": []}),
            content_type="application/json",
        ).status_code, 401)
        self.assertEqual(self.client.get(url).status_code, 405)
        invalid_body = self.client.post(url, data="[]", content_type="application/json")
        self.assertEqual(invalid_body.status_code, 400)
        self.assertEqual(invalid_body.json(), {"error": "invalid_request"})
        too_many = self.verify(occurrence, ["ABCD-EFGH-JKLM-NPQR"] * 101)
        self.assertEqual(too_many.status_code, 400)
        self.assertEqual(too_many.json(), {"error": "invalid_request"})
