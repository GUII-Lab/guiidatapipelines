import json

from django.test import Client, TestCase
from django.utils import timezone

from leai.models import (
    CourseMembership,
    InstitutionMembership,
    ResponseMessage,
)
from leai.tests.test_response_models import ResponseFixturesMixin
from leai.tests.session_client import SessionClient


ROOT = "/datapipeline/api/v1/"


class ResponseSearchApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Test-Password-Only-2026!")
        self.account.user.save(update_fields=["password"])
        self.membership = InstitutionMembership.objects.create(
            account=self.account,
            institution=self.institution,
            role="instructor",
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.membership,
            role="instructor",
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

    def search(self, course=None, query="capstone", **extra):
        course = course or self.course
        return self.client.post(
            ROOT + f"instructor_courses/{course.public_id}/responses/search/",
            data=json.dumps({"query": query, **extra}),
            content_type="application/json",
        )

    def add_message(self, *, course=None, status="completed", role="student", content="Capstone progress is clear"):
        occurrence = self.make_occurrence(course=course)
        if status == "completed":
            finished = timezone.now()
            session = self.make_student_session(
                occurrence=occurrence,
                status="completed",
                completed_at=finished,
                completion_snapshot={"completed_at": finished.isoformat()},
                research_consent=False,
            )
        else:
            session = self.make_student_session(occurrence=occurrence)
        message = ResponseMessage.objects.create(
            response_session=session,
            sequence=1,
            role=role,
            input_method="typed" if role == "student" else None,
            content=content,
        )
        return occurrence, session, message

    def test_search_returns_only_completed_student_messages_in_selected_course(self):
        occurrence, session, message = self.add_message(content="My CAPSTONE prototype improved.")
        self.add_message(status="active", content="capstone unfinished secret")
        self.add_message(role="assistant", content="capstone assistant secret")
        self.add_message(course=self.other_course, content="capstone other course secret")

        response = self.search(query="capstone")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["query"], "capstone")
        self.assertFalse(payload["has_more"])
        self.assertEqual(len(payload["results"]), 1)
        result = payload["results"][0]
        self.assertEqual(result["occurrence_label"], occurrence.label)
        self.assertEqual(result["response_id"], str(session.public_id))
        self.assertEqual(result["message_id"], message.pk)
        self.assertIn("CAPSTONE prototype", result["excerpt"])
        self.assertNotIn("secret", str(payload))

    def test_other_course_and_unknown_course_are_both_not_found(self):
        self.add_message(course=self.other_course, content="capstone private")
        denied = self.search(course=self.other_course)
        unknown = self.client.post(
            ROOT + "instructor_courses/11111111-1111-4111-8111-111111111111/responses/search/",
            data=json.dumps({"query": "capstone"}),
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.json(), unknown.json())

    def test_search_requires_auth_and_valid_bounded_query(self):
        url = ROOT + f"instructor_courses/{self.course.public_id}/responses/search/"
        self.assertEqual(SessionClient().post(url, data=json.dumps({"query": "capstone"}), content_type="application/json").status_code, 401)
        for params in ({}, {"query": "a"}, {"query": "x" * 101}, {"query": "good", "extra": "x"}):
            with self.subTest(params=params):
                response = self.client.post(
                    url,
                    data=json.dumps(params),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assertEqual(self.client.get(url + "?q=capstone").status_code, 405)

    def test_unicode_prefix_does_not_displace_match_excerpt(self):
        self.add_message(content="ß" * 100 + " capstone marker")
        result = self.search().json()["results"][0]
        self.assertIn("capstone", result["excerpt"])

    def test_course_permission_is_rechecked_on_every_search(self):
        self.add_message(content="capstone visible while assigned")
        self.assertEqual(self.search().status_code, 200)
        CourseMembership.objects.filter(
            course=self.course,
            institution_membership=self.membership,
        ).delete()
        response = self.search()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "not_found"})

    def test_ta_can_search_even_when_legacy_password_change_flag_is_set(self):
        self.add_message(content="capstone team feedback")
        CourseMembership.objects.filter(
            course=self.course,
            institution_membership=self.membership,
        ).update(role="ta")
        self.assertEqual(self.search().status_code, 200)
        self.account.must_change_password = True
        self.account.save(update_fields=["must_change_password"])
        response = self.search()
        self.assertEqual(response.status_code, 200)

    def test_result_limit_is_explicit_and_excerpts_are_bounded(self):
        occurrence = self.make_occurrence()
        finished = timezone.now()
        session = self.make_student_session(
            occurrence=occurrence,
            status="completed",
            completed_at=finished,
            completion_snapshot={"completed_at": finished.isoformat()},
        )
        for number in range(21):
            ResponseMessage.objects.create(
                response_session=session,
                sequence=number + 1,
                role="student",
                input_method="typed",
                content=f"capstone {number} " + "details " * 100,
            )
        response = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 20)
        self.assertTrue(response.json()["has_more"])
        self.assertTrue(all(len(row["excerpt"]) <= 240 for row in response.json()["results"]))
