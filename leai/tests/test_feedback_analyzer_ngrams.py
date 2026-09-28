from django.test import SimpleTestCase

from leai.services import feedback_analyzer


class FeedbackAnalyzerNgramTests(SimpleTestCase):
    def test_counts_student_text_and_pdf_answers_without_crossing_response_boundaries(self):
        records = [
            {
                "messages": [
                    {"role": "student", "content": "The clear instructions help."},
                    {"role": "assistant", "content": "Assistant-only wording must stay out."},
                    {"role": "student", "content": "Peer review works."},
                ],
                "pdf_answers": [],
            },
            {
                "messages": [],
                "pdf_answers": [{"value": "Clear instructions help."}],
            },
            {
                "messages": [{"role": "student", "content": "Peer review"}],
                "pdf_answers": [],
            },
        ]

        compute_ngrams = getattr(feedback_analyzer, "compute_ngrams", None)
        self.assertTrue(callable(compute_ngrams), "canonical n-gram counting is not implemented")
        terms = compute_ngrams(records, 2)

        self.assertEqual(
            terms,
            [
                {"term": "clear instructions", "count": 2},
                {"term": "instructions help", "count": 2},
                {"term": "peer review", "count": 2},
                {"term": "help peer", "count": 1},
                {"term": "review works", "count": 1},
            ],
        )
        self.assertNotIn("review the clear", [item["term"] for item in terms])
        self.assertNotIn("assistant only", [item["term"] for item in terms])

    def test_keyness_uses_the_legacy_log_likelihood_score_for_target_terms(self):
        compute_keyness = getattr(feedback_analyzer, "compute_keyness", None)
        self.assertTrue(callable(compute_keyness), "legacy-compatible keyness is not implemented")
        scores = compute_keyness(
            {"clear": 5, "other": 1},
            10,
            {"clear": 1, "other": 9},
            100,
        )

        self.assertGreater(scores["clear"], scores["other"])
        self.assertEqual(set(scores), {"clear", "other"})

    def test_term_matches_use_normalized_whole_token_phrases_and_keep_records_separate(self):
        records = [
            {"messages": [{"role": "student", "content": "Very clear, instructions."}], "pdf_answers": []},
            {"messages": [{"role": "student", "content": "The instructions are unclear."}], "pdf_answers": []},
            {"messages": [], "pdf_answers": [{"value": "Clear instructions"}]},
        ]

        response_matches_term = getattr(feedback_analyzer, "response_matches_term", None)
        self.assertTrue(callable(response_matches_term), "response term matching is not implemented")
        matches = response_matches_term(records, "clear instructions")

        self.assertEqual(len(matches), 2)
        self.assertIs(matches[0], records[0])
        self.assertIs(matches[1], records[2])

    def test_displayed_terms_fit_the_response_drilldown_limit(self):
        too_long = "a" * 101
        records = [{"messages": [{"role": "student", "content": f"{too_long} clear"}], "pdf_answers": []}]

        terms = feedback_analyzer.compute_ngrams(records, 1)

        self.assertEqual(terms, [{"term": "clear", "count": 1}])
        self.assertTrue(all(len(item["term"]) <= 100 for item in terms))
