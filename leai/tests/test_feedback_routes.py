from django.test import SimpleTestCase
from django.urls import resolve


class FeedbackFeatureRouteTests(SimpleTestCase):
    def test_feedback_chat_routes_are_registered_under_the_versioned_api(self):
        routes = {
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/occurrences/": "leai-feedback-chat-occurrences",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/chats/": "leai-feedback-chats",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/chats/550e8400-e29b-41d4-a716-446655440001/": "leai-feedback-chat-detail",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/chats/550e8400-e29b-41d4-a716-446655440001/scope/": "leai-feedback-chat-scope",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/chats/550e8400-e29b-41d4-a716-446655440001/turns/": "leai-feedback-chat-turns",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/jobs/550e8400-e29b-41d4-a716-446655440002/": "leai-feedback-chat-job",
        }
        for route, name in routes.items():
            with self.subTest(route=route):
                self.assertEqual(resolve(f"/datapipeline/api/v1/{route}").url_name, name)

    def test_feedback_analyzer_routes_are_registered_under_the_versioned_api(self):
        routes = {
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/overview/": "leai-analysis-overview",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/ngrams/": "leai-analysis-ngrams",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/responses/": "leai-analysis-responses",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/responses/550e8400-e29b-41d4-a716-446655440001/": "leai-analysis-response-detail",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis/progress/": "leai-analysis-progress",
            "instructor_courses/550e8400-e29b-41d4-a716-446655440000/analysis-settings/": "leai-analysis-settings",
            "surveys/550e8400-e29b-41d4-a716-446655440000/sessions/550e8400-e29b-41d4-a716-446655440001/matching-signals/": "leai-matching-signals",
        }
        for route, name in routes.items():
            with self.subTest(route=route):
                self.assertEqual(resolve(f"/datapipeline/api/v1/{route}").url_name, name)

    def test_matching_signal_route_keeps_its_student_capability_csrf_exemption(self):
        match = resolve(
            "/datapipeline/api/v1/surveys/550e8400-e29b-41d4-a716-446655440000/"
            "sessions/550e8400-e29b-41d4-a716-446655440001/matching-signals/"
        )
        self.assertTrue(getattr(match.func, "csrf_exempt", False))
