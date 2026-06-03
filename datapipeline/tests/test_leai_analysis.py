"""Unit and integration tests for datapipeline.leai_analysis.

All DB-touching tests use Django's TestCase (transactions rolled back after
each test).  Pure-function tests use unittest.TestCase for speed.
LLM calls are mocked — no real network calls.
"""
from __future__ import annotations

import json
from unittest import mock
from unittest.mock import MagicMock, patch

from django.test import TestCase

from datapipeline import leai_analysis
from datapipeline.leai_analysis import (
    QUICKTAKE_SCHEMA,
    VERIFIER_SCHEMA,
    build_chat_corpus_block,
    build_quicktake_user_text,
    build_response_corpus,
    compute_team_completeness,
    default_chat_system_prompt,
    default_quicktake_system_prompt,
    filter_actions,
    filter_bullet_citations,
    generate_quicktake,
    merge_team_completeness,
    parse_inline_citations,
    run_chat_turn,
    validate_quote_spans,
    validate_form_sections,
    validate_team_health,
    validate_tensions,
    verify_claims,
)
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    LEAIChatMessage,
    LEAIChatSession,
    LEAIQuickTake,
    SessionTeamAssignment,
    SurveyTeam,
    SurveyTeamSnapshot,
    TeamConfiguration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_course(course_id="cs101", course_name="CS 101"):
    return Course.objects.create(
        course_id=course_id,
        course_name=course_name,
        instructor_name="Prof. Test",
        password="pw",
    )


def _make_survey(course, week_number, survey_label=""):
    return FeedbackGPT.objects.create(
        name=f"Survey w{week_number}",
        instructions="Be helpful.",
        course=course,
        week_number=week_number,
        survey_label=survey_label or f"Week {week_number}",
        public_id=f"pub{course.pk}{week_number}{survey_label[:2]}",
    )


def _make_msg(survey, session_id, content, sent_by="user-message"):
    return FeedbackMessage.objects.create(
        session_id=session_id,
        student_id="anon",
        sent_by=sent_by,
        content=content,
        gpt_used="test",
        gpt_id=survey.pk,
    )


# ---------------------------------------------------------------------------
# BuildResponseCorpusTest
# ---------------------------------------------------------------------------

class BuildResponseCorpusTest(TestCase):
    """Tests for build_response_corpus()."""

    def setUp(self):
        self.course = _make_course()
        self.w1 = _make_survey(self.course, week_number=1)
        self.w2 = _make_survey(self.course, week_number=2, survey_label="W2")

        # 3 sessions: 2 in week 1, 1 in week 2
        _make_msg(self.w1, "sess-a", "Lecture was too fast")
        _make_msg(self.w1, "sess-b", "Good examples")
        _make_msg(self.w2, "sess-c", "More practice problems please")

    def test_course_scope_returns_all_student_messages(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        self.assertEqual(len(corpus), 3)

    def test_week_scope_filters_to_one_week(self):
        corpus = build_response_corpus(
            self.course, scope_kind="week", scope_week_number=1
        )
        self.assertEqual(len(corpus), 2)
        for entry in corpus:
            self.assertEqual(entry["week_number"], 1)

    def test_responses_are_ordered_by_week_then_session(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        weeks = [e["week_number"] for e in corpus]
        # Week 1 entries come before week 2
        self.assertEqual(sorted(weeks), weeks)

    def test_ai_messages_are_excluded(self):
        # Add an AI message to week 1 survey
        _make_msg(self.w1, "sess-a", "AI response here", sent_by="ai")
        corpus = build_response_corpus(self.course, scope_kind="course")
        # Count should still be 3 (same as setUp — AI message excluded)
        self.assertEqual(len(corpus), 3)

    def test_student_messages_concatenated_per_session(self):
        # Add a second message to sess-a in week 1
        _make_msg(self.w1, "sess-a", "Also slides were unclear")
        corpus = build_response_corpus(self.course, scope_kind="course")
        # Find sess-a entry
        sess_a = next(e for e in corpus if e["session_id"] == "sess-a")
        self.assertIn(" | ", sess_a["text"])
        self.assertIn("Lecture was too fast", sess_a["text"])
        self.assertIn("Also slides were unclear", sess_a["text"])
        # Still 3 unique sessions
        self.assertEqual(len(corpus), 3)


# ---------------------------------------------------------------------------
# ParseInlineCitationsTest
# ---------------------------------------------------------------------------

class ParseInlineCitationsTest(TestCase):
    """Tests for parse_inline_citations()."""

    def test_single_citation(self):
        cleaned, cited = parse_inline_citations("Students struggled [R17] with pace.")
        self.assertIn("[1]", cleaned)
        self.assertNotIn("[R17]", cleaned)
        self.assertEqual(cited, ["R17"])

    def test_multiple_citations(self):
        text = "Theme A [R17] and also [R42] and finally [R71]."
        cleaned, cited = parse_inline_citations(text)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertIn("[3]", cleaned)
        self.assertNotIn("[R", cleaned)
        self.assertEqual(cited, ["R17", "R42", "R71"])

    def test_no_citations(self):
        text = "No citations here at all."
        cleaned, cited = parse_inline_citations(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(cited, [])

    def test_duplicate_citation_reuses_pill_index(self):
        # [R17] appears twice → both occurrences become [1], matching the
        # single entry in cited[] so the popover can resolve every pill.
        text = "First mention [R17] and again [R17]."
        cleaned, cited = parse_inline_citations(text)
        self.assertEqual(cleaned, "First mention [1] and again [1].")
        self.assertEqual(cited, ["R17"])

    def test_interleaved_repeats_keep_stable_pill_indices(self):
        # Pill numbers track unique R-ids, so [R17][R42][R17] → [1][2][1].
        text = "[R17] then [R42] then back to [R17]."
        cleaned, cited = parse_inline_citations(text)
        self.assertEqual(cleaned, "[1] then [2] then back to [1].")
        self.assertEqual(cited, ["R17", "R42"])

    def test_comma_separated_citations(self):
        text = "Students struggled [R5, R25, R35] with the project."
        cleaned, cited = parse_inline_citations(text)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertIn("[3]", cleaned)
        self.assertNotIn("[R", cleaned)
        self.assertEqual(cited, ["R5", "R25", "R35"])

    def test_bold_markdown_citations(self):
        text = "Response **R18** mentions rubric issues and **R36** mentions grading."
        cleaned, cited = parse_inline_citations(text)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertNotIn("**R", cleaned)
        self.assertEqual(cited, ["R18", "R36"])

    # -- valid_rids filter ----------------------------------------------------

    def test_valid_rids_drops_hallucinated_citations(self):
        # Corpus only has R5 and R10. The model invented R99.
        text = "Real claim [R5] and bogus claim [R99] and another real one [R10]."
        cleaned, cited = parse_inline_citations(text, valid_rids={"R5", "R10"})
        # Hallucinated rid is stripped entirely from the rendered text.
        self.assertNotIn("R99", cleaned)
        self.assertNotIn("[3]", cleaned)
        # Real rids keep stable, sequential pill numbers.
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertEqual(cited, ["R5", "R10"])

    def test_valid_rids_none_disables_filter(self):
        # Default behavior: no filter, every R-id becomes a pill.
        text = "Real [R5] and invented [R99]."
        cleaned, cited = parse_inline_citations(text, valid_rids=None)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertEqual(cited, ["R5", "R99"])

    def test_valid_rids_empty_set_drops_all_citations(self):
        # Edge case: empty corpus → every citation is invalid.
        text = "Claim one [R1] and two [R2]."
        cleaned, cited = parse_inline_citations(text, valid_rids=set())
        self.assertEqual(cited, [])
        self.assertNotIn("[R", cleaned)
        self.assertNotIn("[1]", cleaned)

    def test_valid_rids_preserves_repeat_after_filter(self):
        # Filter removes R99; repeated [R5] still collapses to a single pill.
        text = "[R5] then [R99] then [R5] again."
        cleaned, cited = parse_inline_citations(text, valid_rids={"R5"})
        self.assertEqual(cited, ["R5"])
        self.assertEqual(cleaned, "[1] then  then [1] again.")


# ---------------------------------------------------------------------------
# ValidateQuoteSpansTest
# ---------------------------------------------------------------------------

class ValidateQuoteSpansTest(TestCase):
    """Tests for validate_quote_spans() — quote-level hallucination guard."""

    def _corpus(self):
        return [
            {"rid": "R1", "text": "Friday standup is non-negotiable now.", "week_number": 7},
            {"rid": "R2", "text": "Two of us want to ship, two want to keep iterating.",
             "week_number": 7},
        ]

    def test_verbatim_quote_passes(self):
        bullets = [{
            "text": "Teams have settled on a Friday rhythm.",
            "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "Friday standup is non-negotiable now."}],
        }]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(len(cleaned[0]["quotes"]), 1)
        self.assertEqual(stats["quotes_verified"], 1)
        self.assertEqual(stats["quotes_dropped"], 0)

    def test_substring_quote_passes(self):
        # The model can quote a span shorter than the full response.
        bullets = [{
            "text": "Friday is the routine.", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "Friday standup is non-negotiable"}],
        }]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(len(cleaned[0]["quotes"]), 1)
        self.assertEqual(stats["quotes_verified"], 1)

    def test_normalized_whitespace_and_smart_quotes(self):
        # The model often re-emits with smart quotes or different spacing.
        corpus = [{"rid": "R1", "text": "I'm not sure if this is the right path.",
                   "week_number": 1}]
        bullets = [{
            "text": "Uncertainty.", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "I’m not sure   if this is the right path"}],
        }]
        cleaned, stats = validate_quote_spans(bullets, corpus)
        self.assertEqual(len(cleaned[0]["quotes"]), 1)

    def test_fabricated_quote_dropped(self):
        bullets = [{
            "text": "Teams are using AI tools.", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "We use Claude every single day."}],
        }]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(cleaned[0]["quotes"], [])
        self.assertEqual(stats["quotes_dropped"], 1)
        self.assertEqual(stats["quotes_verified"], 0)

    def test_quote_with_orphaned_rid_dropped(self):
        # rid the model invented or that was filtered earlier.
        bullets = [{
            "text": "Theme.", "cited_ids": ["R99"],
            "quotes": [{"rid": "R99", "text": "anything"}],
        }]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(cleaned[0]["quotes"], [])
        self.assertEqual(stats["quotes_orphaned"], 1)

    def test_mixed_real_and_fake_quotes_kept_selectively(self):
        bullets = [{
            "text": "Two-stance pattern.", "cited_ids": ["R2"],
            "quotes": [
                {"rid": "R2", "text": "Two of us want to ship"},
                {"rid": "R2", "text": "We invented this sentence."},
            ],
        }]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(len(cleaned[0]["quotes"]), 1)
        self.assertEqual(cleaned[0]["quotes"][0]["text"], "Two of us want to ship")
        self.assertEqual(stats["quotes_verified"], 1)
        self.assertEqual(stats["quotes_dropped"], 1)

    def test_missing_quotes_field_treated_as_empty(self):
        # Old-format bullet without `quotes` key still works.
        bullets = [{"text": "X", "cited_ids": ["R1"]}]
        cleaned, stats = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(cleaned[0]["quotes"], [])
        self.assertEqual(stats["quotes_total"], 0)

    def test_quote_with_empty_text_dropped(self):
        bullets = [{
            "text": "X", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": ""}],
        }]
        cleaned, _ = validate_quote_spans(bullets, self._corpus())
        self.assertEqual(cleaned[0]["quotes"], [])


# ---------------------------------------------------------------------------
# FormModeSectionExtractionTest
# ---------------------------------------------------------------------------

class FormModeSectionExtractionTest(TestCase):
    """Phase 8: form-mode sessions are split per FormSchema section."""

    def setUp(self):
        self.course = _make_course("form-c", "Form Course")
        self.survey = FeedbackGPT.objects.create(
            name="Form Survey", instructions="Reflect.", course=self.course,
            week_number=6, survey_label="W6", public_id="formpub",
            mode="form",
        )
        # Simulate the AI agent's transcript: each "Area N of K — Title."
        # marker on an AI turn switches the current section.
        timeline = [
            ("assistant", "Area 1 of 3 — Key Concepts. Let's begin."),
            ("user", "Common pitfalls in analysis stood out to me."),
            ("user", "Also 5 Whys and triangulation."),
            ("assistant", "Area 2 of 3 — Methods in Practice. Walk me through one method."),
            ("user", "We tried affinity diagramming this week."),
            ("assistant", "Area 3 of 3 — Connection to Capstone. How does it apply?"),
            ("user", "It connects to our capstone interviews."),
        ]
        for role, content in timeline:
            sent_by = "user" if role == "user" else "assistant"
            _make_msg(self.survey, "form-sess", content, sent_by=sent_by)

    def test_corpus_entry_has_sections_field(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        self.assertEqual(len(corpus), 1)
        sections = corpus[0]["sections"]
        titles = [s["title"] for s in sections]
        self.assertEqual(titles, [
            "Key Concepts",
            "Methods in Practice",
            "Connection to Capstone",
        ])

    def test_user_messages_grouped_under_correct_section(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        sections = {s["title"]: s["text"] for s in corpus[0]["sections"]}
        self.assertIn("Common pitfalls", sections["Key Concepts"])
        self.assertIn("5 Whys", sections["Key Concepts"])
        self.assertIn("affinity diagramming", sections["Methods in Practice"])
        self.assertIn("capstone", sections["Connection to Capstone"])

    def test_prompt_block_renders_sections(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        block = build_chat_corpus_block(corpus)
        self.assertIn("· Key Concepts:", block)
        self.assertIn("· Methods in Practice:", block)
        self.assertIn("· Connection to Capstone:", block)

    def test_unmarked_messages_go_under_unsorted(self):
        # If a student types before any section header (chat noise or
        # accidental pre-prompt), the messages get bucketed into a synthetic
        # "Unsorted" so we never silently drop them.
        survey2 = FeedbackGPT.objects.create(
            name="Form Survey 2", instructions="X", course=self.course,
            week_number=7, survey_label="W7", public_id="form2",
            mode="form",
        )
        _make_msg(survey2, "s2", "hello before any section", sent_by="user")
        _make_msg(survey2, "s2", "Area 1 of 1 — First Area. Begin.",
                  sent_by="assistant")
        _make_msg(survey2, "s2", "after the header", sent_by="user")
        corpus = build_response_corpus(self.course, scope_kind="custom",
                                       scope_survey_ids=[survey2.pk])
        sections = {s["title"]: s["text"] for s in corpus[0]["sections"]}
        self.assertIn("Unsorted", sections)
        self.assertIn("hello before any section", sections["Unsorted"])
        self.assertIn("First Area", sections)
        self.assertIn("after the header", sections["First Area"])


# ---------------------------------------------------------------------------
# ValidateFormSectionsTest
# ---------------------------------------------------------------------------

class ValidateFormSectionsTest(TestCase):
    """Phase 8: validate_form_sections drops invented sections/quotes."""

    def _corpus(self):
        return [{
            "rid": "R1", "team_name": None, "week_number": 6,
            "text": "Common pitfalls in analysis. | Affinity diagramming worked.",
            "sections": [
                {"title": "Key Concepts", "text": "Common pitfalls in analysis."},
                {"title": "Methods", "text": "Affinity diagramming worked."},
            ],
        }]

    def test_real_section_with_verbatim_quote_kept(self):
        entries = [{
            "section_title": "Methods", "summary": "Students used affinity diagramming.",
            "quote": {"rid": "R1", "text": "Affinity diagramming worked"},
        }]
        self.assertEqual(len(validate_form_sections(entries, self._corpus())), 1)

    def test_invented_section_title_dropped(self):
        entries = [{
            "section_title": "Made-up Section", "summary": "Stuff.",
            "quote": {"rid": "R1", "text": "Common pitfalls"},
        }]
        self.assertEqual(validate_form_sections(entries, self._corpus()), [])

    def test_fabricated_quote_dropped(self):
        entries = [{
            "section_title": "Methods", "summary": "Stuff.",
            "quote": {"rid": "R1", "text": "Never said this exact thing."},
        }]
        self.assertEqual(validate_form_sections(entries, self._corpus()), [])


# ---------------------------------------------------------------------------
# ValidateTeamHealthTest
# ---------------------------------------------------------------------------

class ValidateTeamHealthTest(TestCase):
    """Tests for validate_team_health() — Phase 7."""

    def _corpus(self):
        return [
            {"rid": "R1", "text": "We argue in FigJam and vote on Friday.",
             "team_name": "Team Alpha", "week_number": 7},
            {"rid": "R2", "text": "We are stuck on scope since week 5.",
             "team_name": "Team Echo", "week_number": 7},
            {"rid": "R3", "text": "Untagged response", "team_name": None,
             "week_number": 7},
        ]

    def test_valid_team_entry_kept(self):
        entries = [{
            "team_name": "Team Alpha", "status": "healthy",
            "summary": "Healthy rhythm.",
            "quote": {"rid": "R1", "text": "argue in FigJam"},
        }]
        self.assertEqual(len(validate_team_health(entries, self._corpus())), 1)

    def test_unknown_team_name_dropped(self):
        entries = [{
            "team_name": "Team Made-up", "status": "watch",
            "summary": "Sus", "quote": {"rid": "R1", "text": "argue in FigJam"},
        }]
        self.assertEqual(validate_team_health(entries, self._corpus()), [])

    def test_fabricated_quote_dropped(self):
        entries = [{
            "team_name": "Team Echo", "status": "at_risk",
            "summary": "Stuck.",
            "quote": {"rid": "R2", "text": "Never said this exact thing."},
        }]
        self.assertEqual(validate_team_health(entries, self._corpus()), [])

    def test_no_response_status_skips_quote_check(self):
        # Teams with no submissions can't have a quote; allow the status
        # tag to stand even when the model attached a placeholder quote.
        entries = [{
            "team_name": "Team Alpha", "status": "no_response",
            "summary": "Nobody on this team submitted.",
            "quote": {"rid": "R1", "text": "irrelevant"},
        }]
        self.assertEqual(len(validate_team_health(entries, self._corpus())), 1)


# ---------------------------------------------------------------------------
# BuildResponseCorpusTeamAnnotationTest
# ---------------------------------------------------------------------------

class BuildResponseCorpusTeamAnnotationTest(TestCase):
    """Phase 7: corpus entries pick up `team_name` from SessionTeamAssignment."""

    def setUp(self):
        self.course = _make_course("teams-c", "Team Course")
        self.survey = _make_survey(self.course, week_number=1)
        # Team config + a single team with a snapshot tied to the survey.
        cfg = TeamConfiguration.objects.create(
            course=self.course, name="Project Teams", label_prefix="Team",
        )
        snapshot = SurveyTeamSnapshot.objects.create(
            survey=self.survey, source_configuration=cfg,
            name="Project Teams", label_prefix="Team",
        )
        self.team_alpha = SurveyTeam.objects.create(
            snapshot=snapshot, number=1, size=4, display_name="Alpha",
        )
        # session sa is on Team Alpha; session sb has no assignment.
        _make_msg(self.survey, "sa", "We met on Friday.")
        _make_msg(self.survey, "sb", "No team here.")
        SessionTeamAssignment.objects.create(
            session_id="sa", survey_team=self.team_alpha,
        )

    def test_team_name_attached_for_assigned_sessions(self):
        from datapipeline.leai_analysis import build_response_corpus
        corpus = build_response_corpus(course=self.course, scope_kind="course")
        by_sid = {e["session_id"]: e for e in corpus}
        self.assertEqual(by_sid["sa"]["team_name"], "Alpha")
        self.assertIsNone(by_sid["sb"]["team_name"])

    def test_prompt_includes_team_in_rid_line(self):
        from datapipeline.leai_analysis import (
            build_response_corpus, build_chat_corpus_block,
        )
        corpus = build_response_corpus(course=self.course, scope_kind="course")
        block = build_chat_corpus_block(corpus)
        # team-tagged response carries the team label inline
        self.assertIn("· Alpha", block)
        # untagged response has the plain rid prefix
        self.assertRegex(block, r"\[R\d+\] No team here\.")


# ---------------------------------------------------------------------------
# TeamCompletenessTest
# ---------------------------------------------------------------------------

class TeamCompletenessTest(TestCase):
    """compute_team_completeness + merge_team_completeness — deterministic
    per-team response counts that surface fully-silent teams."""

    def setUp(self):
        self.course = _make_course("complete-c", "Completeness Course")
        self.survey = _make_survey(self.course, week_number=1)
        cfg = TeamConfiguration.objects.create(
            course=self.course, name="Project Teams", label_prefix="Team",
        )
        self.snapshot = SurveyTeamSnapshot.objects.create(
            survey=self.survey, source_configuration=cfg,
            name="Project Teams", label_prefix="Team",
        )
        # Alpha: 4 members, 2 submit. Beta: 3 members, 0 submit (silent).
        self.alpha = SurveyTeam.objects.create(
            snapshot=self.snapshot, number=1, size=4, display_name="Alpha",
        )
        self.beta = SurveyTeam.objects.create(
            snapshot=self.snapshot, number=2, size=3, display_name="",
        )
        for sid in ("a1", "a2"):
            _make_msg(self.survey, sid, "Alpha checking in.")
            SessionTeamAssignment.objects.create(
                session_id=sid, survey_team=self.alpha,
            )

    def _completeness(self):
        corpus = build_response_corpus(course=self.course, scope_kind="course")
        surveys = FeedbackGPT.objects.filter(course=self.course)
        return compute_team_completeness(corpus, surveys)

    def test_counts_expected_submitted_missing(self):
        c = self._completeness()
        self.assertEqual(
            c["Alpha"], {"expected": 4, "submitted": 2, "missing": 2},
        )

    def test_fully_silent_team_is_present_with_zero_submitted(self):
        # Beta has no display_name → "Team 2"; nobody submitted, so it must
        # still appear — catching the silent team is the whole point.
        c = self._completeness()
        self.assertEqual(
            c["Team 2"], {"expected": 3, "submitted": 0, "missing": 3},
        )

    def test_merge_attaches_counts_and_appends_silent_team(self):
        c = self._completeness()
        # The model assessed Alpha only.
        team_health = [{
            "team_name": "Alpha", "status": "healthy",
            "summary": "Good rhythm.",
            "quote": {"rid": "R1", "text": "checking in"},
        }]
        merged = merge_team_completeness(team_health, c)
        by_name = {t["team_name"]: t for t in merged}
        # Alpha keeps its model status and gains the counts.
        self.assertEqual(by_name["Alpha"]["status"], "healthy")
        self.assertEqual(by_name["Alpha"]["missing"], 2)
        # The silent team is appended and flagged at_risk.
        self.assertIn("Team 2", by_name)
        self.assertEqual(by_name["Team 2"]["status"], "at_risk")
        self.assertEqual(by_name["Team 2"]["submitted"], 0)
        # Attention-first ordering: the silent at_risk team leads.
        self.assertEqual(merged[0]["team_name"], "Team 2")

    def test_missing_clamped_at_zero_when_overfull(self):
        # More submitters than the recorded size must not go negative.
        c = compute_team_completeness(
            corpus=[{"team_name": "Alpha"}] * 5,
            surveys_qs=FeedbackGPT.objects.filter(course=self.course),
        )
        self.assertEqual(c["Alpha"]["submitted"], 5)
        self.assertEqual(c["Alpha"]["missing"], 0)


# ---------------------------------------------------------------------------
# ValidateTensionsTest
# ---------------------------------------------------------------------------

class ValidateTensionsTest(TestCase):
    """Tests for validate_tensions() — both-sides-verified guard."""

    def _corpus(self):
        return [
            {"rid": "R1", "text": "We meet three times a week and it's exhausting.",
             "week_number": 7},
            {"rid": "R2", "text": "We don't meet enough; nobody's on the same page.",
             "week_number": 7},
        ]

    def _ok_tension(self):
        return {
            "title": "On meeting frequency",
            "sides": [
                {"stance": "Too many meetings", "count": 11,
                 "quote": {"rid": "R1", "text": "three times a week and it's exhausting"}},
                {"stance": "Not enough structure", "count": 9,
                 "quote": {"rid": "R2", "text": "We don't meet enough"}},
            ],
        }

    def test_fully_verified_tension_kept(self):
        result = validate_tensions([self._ok_tension()], self._corpus())
        self.assertEqual(len(result), 1)

    def test_one_fabricated_side_drops_whole_tension(self):
        t = self._ok_tension()
        t["sides"][1]["quote"]["text"] = "This was never said."
        result = validate_tensions([t], self._corpus())
        self.assertEqual(result, [])

    def test_one_orphan_rid_drops_whole_tension(self):
        t = self._ok_tension()
        t["sides"][0]["quote"]["rid"] = "R99"
        result = validate_tensions([t], self._corpus())
        self.assertEqual(result, [])

    def test_fewer_than_two_sides_dropped(self):
        t = {"title": "Single-side", "sides": [self._ok_tension()["sides"][0]]}
        result = validate_tensions([t], self._corpus())
        self.assertEqual(result, [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(validate_tensions([], self._corpus()), [])
        self.assertEqual(validate_tensions(None, self._corpus()), [])


# ---------------------------------------------------------------------------
# FilterBulletCitationsTest
# ---------------------------------------------------------------------------

class FilterBulletCitationsTest(TestCase):
    """Tests for filter_bullet_citations() — Quick Take rid hallucination guard."""

    def test_drops_hallucinated_rids_from_each_bullet(self):
        bullets = [
            {"text": "Theme A", "cited_ids": ["R5", "R99"]},
            {"text": "Theme B", "cited_ids": ["R10", "R11"]},
        ]
        cleaned = filter_bullet_citations(bullets, valid_rids={"R5", "R10", "R11"})
        self.assertEqual(cleaned[0]["cited_ids"], ["R5"])
        self.assertEqual(cleaned[1]["cited_ids"], ["R10", "R11"])
        # Text is untouched
        self.assertEqual(cleaned[0]["text"], "Theme A")

    def test_does_not_mutate_input(self):
        bullets = [{"text": "T", "cited_ids": ["R1", "R2"]}]
        filter_bullet_citations(bullets, valid_rids={"R1"})
        self.assertEqual(bullets[0]["cited_ids"], ["R1", "R2"])

    def test_bullet_with_all_hallucinated_rids_kept_with_empty_cited(self):
        # We keep the bullet (the claim text may still be reasonable) but
        # strip all rids so no broken pills render. The verifier will then
        # naturally flag it as unverified.
        bullets = [{"text": "Claim", "cited_ids": ["R98", "R99"]}]
        cleaned = filter_bullet_citations(bullets, valid_rids={"R1"})
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["cited_ids"], [])
        self.assertEqual(cleaned[0]["text"], "Claim")

    def test_preserves_other_bullet_fields(self):
        bullets = [{"text": "T", "cited_ids": ["R1"], "extra": "x"}]
        cleaned = filter_bullet_citations(bullets, valid_rids={"R1"})
        self.assertEqual(cleaned[0]["extra"], "x")

    def test_empty_input(self):
        self.assertEqual(filter_bullet_citations([], valid_rids={"R1"}), [])


class FilterActionsTest(TestCase):
    """Tests for filter_actions() — Phase 9 suggested-actions cleaner."""

    def test_drops_hallucinated_rids(self):
        actions = [
            {"text": "Add a worked example.", "rationale": "confusion",
             "priority": "high", "cited_ids": ["R5", "R99"]},
        ]
        cleaned = filter_actions(actions, valid_rids={"R5"})
        self.assertEqual(cleaned[0]["cited_ids"], ["R5"])

    def test_action_with_all_hallucinated_rids_kept_with_empty_cited(self):
        actions = [{"text": "Do X.", "rationale": "y", "priority": "low",
                    "cited_ids": ["R98", "R99"]}]
        cleaned = filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["cited_ids"], [])

    def test_blank_text_action_dropped(self):
        actions = [
            {"text": "   ", "rationale": "y", "priority": "high",
             "cited_ids": ["R1"]},
            {"text": "Real action.", "rationale": "y", "priority": "high",
             "cited_ids": ["R1"]},
        ]
        cleaned = filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["text"], "Real action.")

    def test_unknown_priority_coerced_to_medium(self):
        actions = [{"text": "X.", "rationale": "y", "priority": "URGENT!!",
                    "cited_ids": ["R1"]}]
        cleaned = filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(cleaned[0]["priority"], "medium")

    def test_priority_case_normalized(self):
        actions = [{"text": "X.", "rationale": "y", "priority": "High",
                    "cited_ids": ["R1"]}]
        cleaned = filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(cleaned[0]["priority"], "high")

    def test_missing_priority_defaults_to_medium(self):
        actions = [{"text": "X.", "rationale": "y", "cited_ids": ["R1"]}]
        cleaned = filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(cleaned[0]["priority"], "medium")

    def test_does_not_mutate_input(self):
        actions = [{"text": "X.", "rationale": "y", "priority": "low",
                    "cited_ids": ["R1", "R2"]}]
        filter_actions(actions, valid_rids={"R1"})
        self.assertEqual(actions[0]["cited_ids"], ["R1", "R2"])

    def test_empty_input(self):
        self.assertEqual(filter_actions([], valid_rids={"R1"}), [])
        self.assertEqual(filter_actions(None, valid_rids={"R1"}), [])


# ---------------------------------------------------------------------------
# PromptsAndSchemasTest
# ---------------------------------------------------------------------------

class PromptsAndSchemasTest(TestCase):
    """Tests for prompt functions, schema constants, and user-text builders."""

    def test_default_quicktake_prompt_is_nonempty(self):
        prompt = default_quicktake_system_prompt()
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 0)
        self.assertIn("response IDs", prompt)

    def test_chat_prompt_adds_team_mode_when_teams_in_corpus(self):
        corpus = [
            {"rid": "R1", "text": "x", "team_name": "Echo"},
            {"rid": "R2", "text": "y", "team_name": "Hotel"},
        ]
        prompt = default_chat_system_prompt(corpus)
        self.assertIn("TEAM-MODE SCOPE", prompt)
        self.assertIn("Echo", prompt)
        self.assertIn("Hotel", prompt)

    def test_chat_prompt_adds_form_mode_when_sections_in_corpus(self):
        corpus = [
            {"rid": "R1", "text": "x", "team_name": None,
             "sections": [{"title": "Key Concepts", "text": "..."},
                          {"title": "Methods", "text": "..."}]},
        ]
        prompt = default_chat_system_prompt(corpus)
        self.assertIn("FORM-MODE SCOPE", prompt)
        self.assertIn("Key Concepts", prompt)
        self.assertIn("Methods", prompt)

    def test_chat_prompt_plain_when_neither_mode_present(self):
        corpus = [{"rid": "R1", "text": "x", "team_name": None}]
        prompt = default_chat_system_prompt(corpus)
        self.assertNotIn("TEAM-MODE SCOPE", prompt)
        self.assertNotIn("FORM-MODE SCOPE", prompt)

    def test_chat_prompt_no_corpus_arg_is_backward_compatible(self):
        # Existing callers that pass no args should still get a useful prompt.
        prompt = default_chat_system_prompt()
        self.assertIn("cite", prompt.lower())
        self.assertNotIn("TEAM-MODE SCOPE", prompt)

    def test_default_chat_prompt_is_nonempty(self):
        prompt = default_chat_system_prompt()
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 0)
        self.assertIn("LEAI", prompt)

    def test_quicktake_schema_is_valid_json_schema(self):
        self.assertIsInstance(QUICKTAKE_SCHEMA, dict)
        self.assertIn("bullets", QUICKTAKE_SCHEMA.get("properties", {}))

    def test_quicktake_schema_requires_quotes_per_bullet(self):
        # Phase 3a: each bullet must emit verbatim quote spans so the UI
        # can show evidence without forcing instructors to drill into rids.
        bullet_item = QUICKTAKE_SCHEMA["properties"]["bullets"]["items"]
        self.assertIn("quotes", bullet_item["properties"])
        self.assertIn("quotes", bullet_item["required"])
        quote_item = bullet_item["properties"]["quotes"]["items"]
        self.assertEqual(set(quote_item["required"]), {"rid", "text"})

    def test_quicktake_schema_requires_sentiment_per_bullet(self):
        # Per-bullet valence drives the instructor UI's positive/negative
        # color coding. The model must always classify; the frontend treats
        # an absent or unknown value as neutral.
        bullet_item = QUICKTAKE_SCHEMA["properties"]["bullets"]["items"]
        self.assertIn("sentiment", bullet_item["properties"])
        self.assertIn("sentiment", bullet_item["required"])
        self.assertEqual(
            set(bullet_item["properties"]["sentiment"]["enum"]),
            {"positive", "negative", "mixed", "neutral"},
        )

    def test_quicktake_schema_omits_tensions_and_actions(self):
        # Tensions and suggested actions were removed from generation:
        # instructors prefer to choose their own actions, and the tinted
        # bullets (now carrying their full citation count) convey
        # disagreement and how many students are on each side directly.
        self.assertNotIn("tensions", QUICKTAKE_SCHEMA["properties"])
        self.assertNotIn("actions", QUICKTAKE_SCHEMA["properties"])
        self.assertNotIn("tensions", QUICKTAKE_SCHEMA["required"])
        self.assertNotIn("actions", QUICKTAKE_SCHEMA["required"])

    def test_quicktake_prompt_drops_tensions_actions_demands_all_rids(self):
        prompt = default_quicktake_system_prompt()
        # No longer instructs the model on tensions or actions.
        self.assertNotIn("TENSIONS", prompt)
        self.assertNotIn("ACTIONS", prompt)
        # cited_ids must now be exhaustive so the count reflects every
        # supporting student, not a 2–3 sample.
        self.assertIn("EVERY response ID", prompt)

    def test_verifier_schema_is_valid_json_schema(self):
        self.assertIsInstance(VERIFIER_SCHEMA, dict)
        self.assertIn("results", VERIFIER_SCHEMA.get("properties", {}))

    def test_build_quicktake_user_text_includes_all_responses(self):
        corpus = [
            {"rid": "R1", "survey_id": 1, "session_id": "s1",
             "week_number": 1, "text": "Lectures are great"},
            {"rid": "R2", "survey_id": 1, "session_id": "s2",
             "week_number": 1, "text": "Need more examples"},
        ]
        user_text = build_quicktake_user_text(
            course_name="Test Course", corpus=corpus, scope_label="Week 1"
        )
        self.assertIn("[R1]", user_text)
        self.assertIn("[R2]", user_text)
        self.assertIn("Lectures are great", user_text)
        self.assertIn("Need more examples", user_text)


# ---------------------------------------------------------------------------
# VerifyClaimsTest
# ---------------------------------------------------------------------------

class VerifyClaimsTest(TestCase):
    """Tests for verify_claims() — mocks openai_client.run_structured."""

    def _mock_structured(self, parsed):
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {},
            "model": "gpt-5.1",
        }

    def test_verify_claims_returns_verdicts(self):
        corpus = [
            {"rid": "R1", "text": "Pace was too fast", "survey_id": 1,
             "session_id": "s1", "week_number": 1},
        ]
        bullets = [{"text": "Students found the pace too fast.", "cited_ids": ["R1"]}]

        expected_results = [
            {"bullet_index": 0, "source_id": "R1", "verdict": "supported"}
        ]
        mock_return = self._mock_structured({"results": expected_results})

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value=mock_return,
        ):
            results = verify_claims(corpus=corpus, bullets=bullets)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["verdict"], "supported")
        self.assertEqual(results[0]["source_id"], "R1")


# ---------------------------------------------------------------------------
# GenerateQuickTakeTest
# ---------------------------------------------------------------------------

class GenerateQuickTakeTest(TestCase):
    """Tests for generate_quicktake() — mocks LLM calls."""

    def setUp(self):
        self.course = _make_course("cse110", "CSE 110")
        self.survey = _make_survey(self.course, week_number=1)
        # Create >=20 sessions to satisfy the minimum threshold
        for i in range(25):
            _make_msg(self.survey, f"sess-{i}", f"Response from student {i}")

    def _mock_structured_result(self):
        bullets = [
            {"text": "Many students liked the labs [R1].", "cited_ids": ["R1"],
             "quotes": []},
        ]
        parsed = {"bullets": bullets, "tensions": [], "gaps": [], "team_health": [], "form_sections": []}
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {},
            "model": "gpt-5.1",
        }

    def test_generate_quicktake_creates_row(self):
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value=self._mock_structured_result(),
        ):
            qt = generate_quicktake(
                course=self.course,
                scope_key="course",
                scope_kind="course",
            )

        self.assertIsInstance(qt, LEAIQuickTake)
        self.assertEqual(qt.course, self.course)
        self.assertEqual(qt.scope_key, "course")
        self.assertIsInstance(qt.bullets, list)
        self.assertGreater(len(qt.bullets), 0)
        self.assertIsInstance(qt.model_name, str)

    def test_generate_quicktake_preserves_verbatim_quotes(self):
        # Mock the model to return the new schema (Phase 3a): each bullet
        # carries verbatim quote spans from the cited rid. The verbatim
        # text must be present in the corpus's stored response. Our setUp
        # uses "Response from student {i}" so a substring of that should
        # pass the validator.
        bullets = [{
            "text": "Several students reflected on the course.",
            "cited_ids": ["R1", "R2"],
            "quotes": [
                {"rid": "R1", "text": "Response from student 0"},
                {"rid": "R2", "text": "Response from student 1"},
                {"rid": "R1", "text": "I just made this up."},  # dropped
            ],
        }]
        parsed = {"bullets": bullets, "tensions": [], "gaps": [], "team_health": [], "form_sections": []}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": json.dumps(parsed),
                "parsed": parsed,
                "usage": {},
                "model": "gpt-5.1",
            },
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        # Verifiable quotes survive; fabricated one dropped.
        self.assertEqual(len(qt.bullets), 1)
        verified_texts = [q["text"] for q in qt.bullets[0]["quotes"]]
        self.assertIn("Response from student 0", verified_texts)
        self.assertIn("Response from student 1", verified_texts)
        self.assertNotIn("I just made this up.", verified_texts)

    def test_generate_quicktake_gates_singletons_and_strips_reused_quotes(self):
        # A multi-student theme and a single-student one-off that reuses the
        # theme's exact quote. After generation the theme must be flagged as
        # not isolated, the one-off as isolated, and the reused quote must
        # survive only on the first (theme) bullet — the two failure modes
        # the instructors reported, fixed at the persistence layer.
        bullets = [
            {"text": "Theme bullet", "cited_ids": ["R1", "R2", "R3"],
             "quotes": [{"rid": "R1", "text": "Response from student"}],
             "sentiment": "negative"},
            {"text": "One-off bullet", "cited_ids": ["R1"],
             "quotes": [{"rid": "R1", "text": "Response from student"}],
             "sentiment": "neutral"},
        ]
        parsed = {"bullets": bullets, "tensions": [], "gaps": [],
                  "team_health": [], "form_sections": []}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": json.dumps(parsed), "parsed": parsed,
                "usage": {}, "model": "gpt-5.1",
            },
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        self.assertEqual(len(qt.bullets), 2)
        theme, oneoff = qt.bullets[0], qt.bullets[1]
        # Support counts come from distinct cited_ids.
        self.assertEqual(theme["support_count"], 3)
        self.assertFalse(theme["is_isolated"])
        self.assertEqual(oneoff["support_count"], 1)
        self.assertTrue(oneoff["is_isolated"])
        # The reused quote is kept on the theme, stripped from the one-off.
        self.assertEqual(len(theme["quotes"]), 1)
        self.assertEqual(oneoff["quotes"], [])

    def test_generate_quicktake_splits_bundled_oneoffs(self):
        # End-to-end count-gate evasion: the model bundles two DISTINCT
        # single-student points into one compound bullet citing both rids
        # (support=2 -> looks like a theme). Per-citation verification passes
        # (each response backs its own half); the coherence pass is what flags
        # it as a bundle. After generation the compound "theme" must be gone,
        # replaced by two isolated one-offs — the lumping failure, fixed at the
        # persistence layer so a stronger corpus can't sneak it past the gate.
        gen_bullets = [{
            "text": "Students feel disconnected and misunderstand design.",
            "cited_ids": ["R1", "R2"],
            "quotes": [
                {"rid": "R1", "text": "Response from student 0"},
                {"rid": "R2", "text": "Response from student 1"},
            ],
            "sentiment": "negative",
        }]
        gen_parsed = {"bullets": gen_bullets, "tensions": [], "gaps": [],
                      "team_health": [], "form_sections": []}
        verify_parsed = {"results": [
            {"bullet_index": 0, "source_id": "R1", "verdict": "supported"},
            {"bullet_index": 0, "source_id": "R2", "verdict": "supported"},
        ]}
        coherence_parsed = {"results": [{"bullet_index": 0, "coherent": False}]}

        def _wrap(p):
            return {"response": json.dumps(p), "parsed": p, "usage": {},
                    "model": "gpt-5.1"}

        def _route(*args, **kwargs):
            schema = kwargs.get("schema_name")
            if schema == "verification_result":
                return _wrap(verify_parsed)
            if schema == "bullet_coherence":
                return _wrap(coherence_parsed)
            return _wrap(gen_parsed)

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=_route,
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        # The compound 2-citation "theme" is gone; two isolated one-offs remain.
        self.assertEqual(len(qt.bullets), 2)
        self.assertTrue(all(b["is_isolated"] for b in qt.bullets))
        self.assertTrue(all(b["support_count"] == 1 for b in qt.bullets))
        self.assertTrue(all(len(b["cited_ids"]) == 1 for b in qt.bullets))
        self.assertEqual(sorted(b["cited_ids"][0] for b in qt.bullets), ["R1", "R2"])

    def test_generate_quicktake_drops_hallucinated_rids_and_quotes(self):
        # Defense in depth: hallucinated rid first, then quote validator
        # never even sees it because filter_bullet_citations strips it.
        bullets = [{
            "text": "Claim with bogus citation.",
            "cited_ids": ["R1", "R999"],
            "quotes": [
                {"rid": "R1",   "text": "Response from student 0"},
                {"rid": "R999", "text": "Hallucinated rid and quote."},
            ],
        }]
        parsed = {"bullets": bullets, "tensions": [], "gaps": [], "team_health": [], "form_sections": []}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": json.dumps(parsed),
                "parsed": parsed,
                "usage": {},
                "model": "gpt-5.1",
            },
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        self.assertEqual(qt.bullets[0]["cited_ids"], ["R1"])
        rids_in_quotes = [q["rid"] for q in qt.bullets[0]["quotes"]]
        self.assertNotIn("R999", rids_in_quotes)
        self.assertEqual(rids_in_quotes, ["R1"])

    def test_generate_quicktake_drops_tensions_keeps_gaps(self):
        # The corpus from setUp has 25 sessions "Response from student {i}".
        # Tensions are fed but should be dropped (no longer generated); gaps
        # still persist. Build a tension whose quotes are real substrings so
        # the only reason it's gone is the removal, not quote validation.
        tension = {
            "title": "On enthusiasm",
            "sides": [
                {"stance": "Engaged", "count": 12,
                 "quote": {"rid": "R1", "text": "Response from student 0"}},
                {"stance": "Disengaged", "count": 7,
                 "quote": {"rid": "R2", "text": "Response from student 1"}},
            ],
        }
        fake_tension = {
            "title": "Fabricated",
            "sides": [
                {"stance": "A", "count": 1,
                 "quote": {"rid": "R1", "text": "never written"}},
                {"stance": "B", "count": 1,
                 "quote": {"rid": "R2", "text": "Response from student 1"}},
            ],
        }
        gaps = [{"topic": "Workload balance", "note": "Not mentioned by anyone."}]
        bullets = [{
            "text": "Synthesis bullet.", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "Response from student 0"}],
        }]
        parsed = {"bullets": bullets, "tensions": [tension, fake_tension], "gaps": gaps}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": json.dumps(parsed),
                "parsed": parsed,
                "usage": {},
                "model": "gpt-5.1",
            },
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        # Tensions are no longer generated — persisted empty regardless of
        # what the model returned.
        self.assertEqual(qt.tensions, [])
        # Gaps persist unchanged.
        self.assertEqual(qt.gaps, gaps)

    def test_generate_quicktake_drops_actions_keeps_response_count(self):
        # Suggested actions are no longer generated; even when the model
        # returns some, the persisted row stores an empty list. The corpus-
        # size snapshot is still captured.
        actions = [
            {"text": "Add a worked example before the next studio.",
             "rationale": "Several students were confused.",
             "priority": "HIGH", "cited_ids": ["R1", "R999"]},
            {"text": "Keep the current lab pacing.",
             "rationale": "Widely praised.",
             "priority": "weird", "cited_ids": ["R2"]},
        ]
        bullets = [{"text": "Synthesis.", "cited_ids": ["R1"],
                    "quotes": [{"rid": "R1", "text": "Response from student 0"}]}]
        parsed = {"bullets": bullets, "tensions": [], "gaps": [],
                  "team_health": [], "form_sections": [], "actions": actions}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": json.dumps(parsed),
                "parsed": parsed,
                "usage": {},
                "model": "gpt-5.1",
            },
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        self.assertEqual(qt.actions, [])
        self.assertEqual(qt.responses_count_at_generation, 25)

    def test_generate_quicktake_raises_on_insufficient_data(self):
        # Hard floor is < 5 responses; 4 should trigger ValueError. The
        # error message still mentions "20+ recommended" — that's the
        # soft threshold for reliable themes, not the hard floor.
        empty_course = _make_course("empty-c", "Empty Course")
        survey = _make_survey(empty_course, week_number=1, survey_label="EW1")
        for i in range(4):
            _make_msg(survey, f"e-sess-{i}", f"Short response {i}")

        with self.assertRaises(ValueError) as ctx:
            generate_quicktake(
                course=empty_course,
                scope_key="course",
                scope_kind="course",
            )
        self.assertIn("20", str(ctx.exception))


# ---------------------------------------------------------------------------
# ChunkedQuickTakeTest
# ---------------------------------------------------------------------------

class ChunkedQuickTakeTest(TestCase):
    """Tests for _run_chunked_quicktake — the map-reduce path.

    Triggered when the corpus exceeds QUICKTAKE_CHUNK_CHAR_LIMIT. We mock
    each chunk's run_structured call and the reducer call. The bug we
    just fixed (quotes/aux-fields lost through chunking) would have been
    caught here.
    """

    def setUp(self):
        self.course = _make_course("chunked-c", "Chunked Course")
        self.survey = _make_survey(self.course, week_number=1)
        # Build a corpus big enough to force chunking (>40k chars).
        # Each session contributes ~2k chars × 25 sessions = 50k chars.
        long_text = "x" * 2000
        for i in range(25):
            _make_msg(self.survey, f"sess-{i:02d}", f"{long_text} marker-{i}")

    def _chunk_response(self, bullets, **extra):
        parsed = {
            "bullets": bullets,
            "tensions": extra.get("tensions", []),
            "gaps": extra.get("gaps", []),
            "team_health": extra.get("team_health", []),
            "form_sections": extra.get("form_sections", []),
            "actions": extra.get("actions", []),
        }
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {},
            "model": "gpt-5.1",
        }

    def test_chunked_path_preserves_quotes_through_reducer(self):
        # Chunk 1: bullet citing R1 with a quote that's a substring of R1's text.
        chunk1 = self._chunk_response(
            [{"text": "Theme A.", "cited_ids": ["R1"],
              "quotes": [{"rid": "R1", "text": "marker-0"}]}],
        )
        # Chunk 2: bullet citing R20 with a quote substring of R20's text.
        chunk2 = self._chunk_response(
            [{"text": "Theme B.", "cited_ids": ["R20"],
              "quotes": [{"rid": "R20", "text": "marker-19"}]}],
        )
        # Reducer: returns merged bullets WITHOUT quotes (typical model
        # behavior — schema allows empty quotes). Our attach_quotes logic
        # must restore them from the rid map.
        reducer = self._chunk_response([
            {"text": "Merged theme A and B.", "cited_ids": ["R1", "R20"],
             "quotes": []},
        ])

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[chunk1, chunk2, reducer, self._chunk_response([]),
                         self._chunk_response([])],
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        # The merged bullet picked up its quotes from the chunk rid map.
        self.assertEqual(len(qt.bullets), 1)
        rids_in_quotes = [q["rid"] for q in qt.bullets[0]["quotes"]]
        self.assertEqual(sorted(rids_in_quotes), ["R1", "R20"])

    def test_chunked_path_aggregates_gaps_across_chunks(self):
        # Each chunk emits a distinct gap/section; aggregation dedupes by
        # topic/title and surfaces both. Tensions are fed in but, post-
        # removal, are no longer collected or stored.
        chunk1 = self._chunk_response(
            [{"text": "B1.", "cited_ids": ["R1"], "quotes": []}],
            tensions=[{"title": "Pace", "sides": [
                {"stance": "fast", "count": 5,
                 "quote": {"rid": "R1", "text": "marker-0"}},
                {"stance": "slow", "count": 4,
                 "quote": {"rid": "R1", "text": "marker-0"}},
            ]}],
            gaps=[{"topic": "Workload balance", "note": "not mentioned"}],
            form_sections=[{
                "section_title": "Key Concepts",
                "summary": "students touched on the key concepts.",
                "quote": {"rid": "R1", "text": "marker-0"},
            }],
        )
        chunk2 = self._chunk_response(
            [{"text": "B2.", "cited_ids": ["R20"], "quotes": []}],
            tensions=[{"title": "Pace", "sides": [  # same title — deduped
                {"stance": "fast", "count": 5,
                 "quote": {"rid": "R20", "text": "marker-19"}},
                {"stance": "slow", "count": 4,
                 "quote": {"rid": "R20", "text": "marker-19"}},
            ]}],
            gaps=[{"topic": "Office hours", "note": "no one mentioned them"}],
            form_sections=[{
                "section_title": "Methods",  # new section — kept
                "summary": "students touched on methods.",
                "quote": {"rid": "R20", "text": "marker-19"},
            }],
        )
        reducer = self._chunk_response([
            {"text": "Merged.", "cited_ids": ["R1", "R20"], "quotes": []},
        ])

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[chunk1, chunk2, reducer, self._chunk_response([]),
                         self._chunk_response([])],
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )

        # Tensions are no longer generated — always empty post-removal.
        self.assertEqual(qt.tensions, [])
        # Two distinct gaps still aggregate across chunks.
        self.assertEqual({g["topic"] for g in qt.gaps},
                         {"Workload balance", "Office hours"})

    def test_chunked_path_skips_invalid_form_section_titles(self):
        # The form_sections validator requires the title to appear in some
        # corpus entry's sections. This corpus is non-form, so any
        # form_sections entries the model emits should be dropped.
        chunk1 = self._chunk_response(
            [{"text": "B1.", "cited_ids": ["R1"], "quotes": []}],
            form_sections=[{
                "section_title": "Invented Section",
                "summary": "fake", "quote": {"rid": "R1", "text": "marker-0"},
            }],
        )
        chunk2 = self._chunk_response(
            [{"text": "B2.", "cited_ids": ["R20"], "quotes": []}],
        )
        reducer = self._chunk_response([
            {"text": "Merged.", "cited_ids": ["R1", "R20"], "quotes": []},
        ])
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[chunk1, chunk2, reducer, self._chunk_response([]),
                         self._chunk_response([])],
        ):
            qt = generate_quicktake(
                course=self.course, scope_key="course", scope_kind="course",
            )
        self.assertEqual(qt.form_sections, [])


# ---------------------------------------------------------------------------
# RunChatTurnTest
# ---------------------------------------------------------------------------

class RunChatTurnTest(TestCase):
    """Tests for run_chat_turn() — mocks openai_client.run_chat."""

    def setUp(self):
        self.course = _make_course("chat-c", "Chat Course")
        self.survey = _make_survey(self.course, week_number=1)
        _make_msg(self.survey, "s1", "I enjoy the readings")

        self.session = LEAIChatSession.objects.create(
            course=self.course,
            title="Test session",
            scope_kind="course",
        )

    def _mock_chat_turn(self, answer="Here is the analysis [R1].", quotes=None):
        """Return a structured-output mock shaped like CHAT_TURN_SCHEMA."""
        parsed = {"answer": answer, "quotes": quotes or []}
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "gpt-5.1",
        }

    def test_run_chat_turn_saves_both_messages(self):
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[
                self._mock_chat_turn(
                    answer="Students found it helpful [R1].",
                    quotes=[{"rid": "R1", "text": "I enjoy the readings"}],
                ),
                {  # verifier call returns no results
                    "response": '{"results":[]}',
                    "parsed": {"results": []},
                    "usage": {},
                    "model": "gpt-5.1",
                },
            ],
        ):
            assistant_msg = run_chat_turn(
                session=self.session,
                user_text="What are the main themes?",
            )

        # Both messages saved
        messages = list(self.session.messages.order_by("created_at"))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "What are the main themes?")
        self.assertEqual(messages[1].role, "assistant")
        # Citations should have been parsed ([R1] → [1])
        self.assertNotIn("[R1]", messages[1].text)
        self.assertIn("[1]", messages[1].text)
        # cited should be a list of dicts with rid, verdict, quote_text keys
        self.assertIsInstance(messages[1].cited, list)
        self.assertEqual(len(messages[1].cited), 1)
        c = messages[1].cited[0]
        self.assertEqual(c['rid'], 'R1')
        self.assertIn('verdict', c)
        self.assertIn('pill_index', c)
        self.assertEqual(c['quote_text'], 'I enjoy the readings')

    def test_run_chat_turn_filters_hallucinated_rids(self):
        # Corpus has only one session → only R1 is a valid rid.
        # The model invents R99; the filter must strip it before saving so
        # the frontend never sees a pill that can't resolve.
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[
                self._mock_chat_turn(
                    answer="Real [R1] and made-up [R99] and again [R1].",
                    quotes=[
                        {"rid": "R1", "text": "I enjoy the readings"},
                        {"rid": "R99", "text": "fabricated"},
                    ],
                ),
                {
                    "response": '{"results":[]}',
                    "parsed": {"results": []},
                    "usage": {},
                    "model": "gpt-5.1",
                },
            ],
        ):
            assistant_msg = run_chat_turn(
                session=self.session,
                user_text="What are the themes?",
            )

        # Stored text drops the hallucinated bracket entirely.
        self.assertNotIn("R99", assistant_msg.text)
        self.assertNotIn("[2]", assistant_msg.text)
        # The real rid keeps its stable pill index across repeats.
        self.assertIn("[1]", assistant_msg.text)
        # cited[] only contains the real rid.
        rids = [c["rid"] for c in assistant_msg.cited]
        self.assertEqual(rids, ["R1"])

    def test_run_chat_turn_attaches_verified_quotes(self):
        # Two quotes from the model. One is verbatim (substring after
        # normalization) — kept. The other isn't in the cited rid — dropped.
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[
                self._mock_chat_turn(
                    answer="The pace was a concern [R1].",
                    quotes=[
                        {"rid": "R1", "text": "I enjoy the readings"},
                        {"rid": "R1", "text": "totally invented sentence"},
                    ],
                ),
                {"response": '{"results":[]}', "parsed": {"results": []},
                 "usage": {}, "model": "gpt-5.1"},
            ],
        ):
            assistant_msg = run_chat_turn(
                session=self.session, user_text="Pace?",
            )
        # The first verbatim quote wins; the fabricated one is silently dropped.
        self.assertEqual(assistant_msg.cited[0]["quote_text"],
                         "I enjoy the readings")

    def test_run_chat_turn_leaves_quote_empty_when_unverifiable(self):
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=[
                self._mock_chat_turn(
                    answer="Trust me bro [R1].",
                    quotes=[{"rid": "R1", "text": "never said"}],
                ),
                {"response": '{"results":[]}', "parsed": {"results": []},
                 "usage": {}, "model": "gpt-5.1"},
            ],
        ):
            assistant_msg = run_chat_turn(
                session=self.session, user_text="Sure?",
            )
        # No verified quote → empty string (frontend can fall back to
        # the full session text in the popover).
        self.assertEqual(assistant_msg.cited[0]["quote_text"], "")

    def test_run_chat_turn_rolls_back_on_llm_error(self):
        from datapipeline.openai_client import OpenAIClientError

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=OpenAIClientError("LLM failure", status_code=502),
        ):
            with self.assertRaises(OpenAIClientError):
                run_chat_turn(
                    session=self.session,
                    user_text="Trigger a failure",
                )

        # No messages should have been saved (transaction rolled back)
        count = LEAIChatMessage.objects.filter(session=self.session).count()
        self.assertEqual(count, 0)


class AnnotateSupportTest(TestCase):
    """Tests for annotate_support() — per-bullet student-count gating.

    The instructor reads cited_ids length as "how many students are on this
    side". A claim backed by a single student is an isolated one-off, not a
    theme — annotate_support tags each bullet with that count and an
    `is_isolated` flag the frontend uses to split themes from one-offs.
    """

    def test_support_count_is_number_of_distinct_cited_ids(self):
        bullets = [{"text": "Pacing felt rushed", "cited_ids": ["R1", "R2", "R3"]}]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["support_count"], 3)
        self.assertFalse(out[0]["is_isolated"])

    def test_duplicate_cited_ids_counted_once(self):
        bullets = [{"text": "X", "cited_ids": ["R5", "R5", "R7"]}]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["support_count"], 2)
        self.assertFalse(out[0]["is_isolated"])

    def test_single_student_claim_is_isolated(self):
        bullets = [{"text": "Felt disconnected from class", "cited_ids": ["R9"]}]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["support_count"], 1)
        self.assertTrue(out[0]["is_isolated"])

    def test_zero_citations_is_isolated(self):
        bullets = [{"text": "Unverifiable claim", "cited_ids": []}]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["support_count"], 0)
        self.assertTrue(out[0]["is_isolated"])

    def test_missing_cited_ids_field_is_isolated(self):
        bullets = [{"text": "No citation field at all"}]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["support_count"], 0)
        self.assertTrue(out[0]["is_isolated"])

    def test_isolated_max_override_widens_the_gate(self):
        bullets = [{"text": "Two students", "cited_ids": ["R1", "R2"]}]
        out = leai_analysis.annotate_support(bullets, isolated_max=2)
        self.assertEqual(out[0]["support_count"], 2)
        self.assertTrue(out[0]["is_isolated"])

    def test_preserves_other_fields(self):
        bullets = [{
            "text": "Labs helped",
            "cited_ids": ["R1", "R2"],
            "quotes": [{"rid": "R1", "text": "the labs helped"}],
            "sentiment": "positive",
        }]
        out = leai_analysis.annotate_support(bullets)
        self.assertEqual(out[0]["text"], "Labs helped")
        self.assertEqual(out[0]["sentiment"], "positive")
        self.assertEqual(out[0]["quotes"], [{"rid": "R1", "text": "the labs helped"}])

    def test_does_not_mutate_input(self):
        bullets = [{"text": "X", "cited_ids": ["R1"]}]
        leai_analysis.annotate_support(bullets)
        self.assertNotIn("support_count", bullets[0])
        self.assertNotIn("is_isolated", bullets[0])


class DedupeBulletQuotesTest(TestCase):
    """Tests for dedupe_bullet_quotes() — one vivid quote can't anchor every line.

    The reported failure mode: a single student's striking quote (same rid +
    same span) gets reused as evidence across many bullets. A given quote
    should appear under only the first bullet that uses it; later repeats are
    dropped so one voice can't masquerade as the whole class.
    """

    def test_repeated_quote_kept_only_in_first_bullet(self):
        q = {"rid": "R5", "text": "design is just pretty interfaces"}
        bullets = [
            {"text": "Bullet A", "cited_ids": ["R5"], "quotes": [dict(q)]},
            {"text": "Bullet B", "cited_ids": ["R5"], "quotes": [dict(q)]},
        ]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(len(out[0]["quotes"]), 1)
        self.assertEqual(out[1]["quotes"], [])

    def test_same_text_different_rid_both_kept(self):
        # Two different students saying the same thing is corroboration,
        # not the reuse bug — keep both.
        bullets = [
            {"text": "A", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "too fast"}]},
            {"text": "B", "cited_ids": ["R7"], "quotes": [{"rid": "R7", "text": "too fast"}]},
        ]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(len(out[0]["quotes"]), 1)
        self.assertEqual(len(out[1]["quotes"]), 1)

    def test_normalization_ignores_case_and_whitespace(self):
        bullets = [
            {"text": "A", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "Design  Is Pretty"}]},
            {"text": "B", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "design is pretty"}]},
        ]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(len(out[0]["quotes"]), 1)
        self.assertEqual(out[1]["quotes"], [])

    def test_partial_dedupe_within_a_bullet_keeps_the_fresh_quote(self):
        bullets = [
            {"text": "A", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "q1"}]},
            {"text": "B", "cited_ids": ["R5", "R7"], "quotes": [
                {"rid": "R5", "text": "q1"},
                {"rid": "R7", "text": "q2"},
            ]},
        ]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(out[1]["quotes"], [{"rid": "R7", "text": "q2"}])

    def test_missing_quotes_field_is_safe(self):
        bullets = [{"text": "A", "cited_ids": ["R5"]}]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(out[0].get("quotes", []), [])

    def test_preserves_fields_and_bullet_order(self):
        bullets = [
            {"text": "First", "cited_ids": ["R1"], "sentiment": "negative",
             "quotes": [{"rid": "R1", "text": "a"}]},
            {"text": "Second", "cited_ids": ["R2"], "sentiment": "positive",
             "quotes": [{"rid": "R2", "text": "b"}]},
        ]
        out = leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual([b["text"] for b in out], ["First", "Second"])
        self.assertEqual(out[0]["sentiment"], "negative")
        self.assertEqual(out[1]["sentiment"], "positive")

    def test_does_not_mutate_input(self):
        bullets = [
            {"text": "A", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "dup"}]},
            {"text": "B", "cited_ids": ["R5"], "quotes": [{"rid": "R5", "text": "dup"}]},
        ]
        leai_analysis.dedupe_bullet_quotes(bullets)
        self.assertEqual(len(bullets[1]["quotes"]), 1)


class EnforceCoherenceTest(TestCase):
    """Tests for enforce_coherence() — split flagged bundles into one-offs.

    The model evades the per-claim count gate by merging two DISTINCT
    single-student points into one bullet citing both rids (support=2 -> reads
    as a class-wide theme), phrased "some students feel X and [some] feel Y"
    so each citation backs its own half and PASSES per-citation verification.
    check_bullet_coherence flags such a bullet as incoherent; enforce_coherence
    then splits each flagged multi-citation bullet into one single-student
    one-off per cited response, re-indexing verification to the new positions.
    Bullets not flagged are kept untouched.
    """

    def _verif(self, *triples):
        return [
            {"bullet_index": bi, "source_id": rid, "verdict": v}
            for (bi, rid, v) in triples
        ]

    def test_flagged_bundle_splits_into_two_isolated_oneoffs(self):
        bullets = [{
            "text": "Students feel disconnected and think design is only aesthetics.",
            "cited_ids": ["R1", "R2"],
            "quotes": [
                {"rid": "R1", "text": "design is just about making pretty interfaces"},
                {"rid": "R2", "text": "I feel disconnected from this class"},
            ],
            "sentiment": "negative",
        }]
        # Per-citation verification PASSES both (each backs its own half) — the
        # split must hinge on the coherence flag alone, not on the verdicts.
        verification = self._verif((0, "R1", "supported"), (0, "R2", "supported"))
        new_bullets, new_verif = leai_analysis.enforce_coherence(
            bullets, {0}, verification,
        )

        self.assertEqual(len(new_bullets), 2)
        for b in new_bullets:
            self.assertTrue(b["is_isolated"])
            self.assertEqual(b["support_count"], 1)
            self.assertEqual(len(b["cited_ids"]), 1)
            self.assertEqual(b["sentiment"], "negative")  # inherited
        # Each one-off's claim is the student's own verbatim words.
        texts = [b["text"] for b in new_bullets]
        self.assertIn("design is just about making pretty interfaces", texts)
        self.assertIn("I feel disconnected from this class", texts)
        # Verification is re-indexed to the new split bullet positions.
        self.assertEqual(
            {(v["bullet_index"], v["source_id"]) for v in new_verif},
            {(0, "R1"), (1, "R2")},
        )

    def test_unflagged_pair_kept_as_theme(self):
        bullets = [{
            "text": "Readings are too long and dense.",
            "cited_ids": ["R1", "R2"],
            "quotes": [
                {"rid": "R1", "text": "readings are too long"},
                {"rid": "R2", "text": "papers are super dense"},
            ],
            "sentiment": "negative",
        }]
        verification = self._verif((0, "R1", "supported"), (0, "R2", "supported"))
        new_bullets, _ = leai_analysis.enforce_coherence(bullets, set(), verification)

        self.assertEqual(len(new_bullets), 1)
        self.assertFalse(new_bullets[0]["is_isolated"])
        self.assertEqual(new_bullets[0]["support_count"], 2)

    def test_three_way_bundle_splits_into_three(self):
        bullets = [{
            "text": "Various unrelated gripes.",
            "cited_ids": ["R1", "R2", "R3"],
            "quotes": [
                {"rid": "R1", "text": "the room is freezing cold"},
                {"rid": "R2", "text": "parking is too expensive"},
                {"rid": "R3", "text": "the textbook is overpriced"},
            ],
            "sentiment": "negative",
        }]
        new_bullets, _ = leai_analysis.enforce_coherence(bullets, {0}, [])

        self.assertEqual(len(new_bullets), 3)
        self.assertTrue(
            all(b["is_isolated"] and b["support_count"] == 1 for b in new_bullets)
        )

    def test_no_flags_behaves_like_annotate_support(self):
        # Graceful degradation: no incoherent flags -> never split; count by
        # distinct cited_ids and flag isolation by threshold, exactly like
        # annotate_support. (A strict superset, never a regression.)
        bullets = [
            {"text": "Theme", "cited_ids": ["R1", "R2", "R3"], "quotes": []},
            {"text": "One-off", "cited_ids": ["R9"], "quotes": []},
        ]
        new_bullets, new_verif = leai_analysis.enforce_coherence(bullets, set(), [])

        self.assertEqual(len(new_bullets), 2)
        self.assertEqual(new_bullets[0]["support_count"], 3)
        self.assertFalse(new_bullets[0]["is_isolated"])
        self.assertTrue(new_bullets[1]["is_isolated"])
        self.assertEqual(new_verif, [])

    def test_flagged_single_citation_bullet_never_splits(self):
        # A bundle needs >=2 citations; a flagged single-citation bullet stays.
        bullets = [{
            "text": "A lone point.", "cited_ids": ["R1"],
            "quotes": [{"rid": "R1", "text": "a lone point"}], "sentiment": "neutral",
        }]
        new_bullets, _ = leai_analysis.enforce_coherence(bullets, {0}, [])

        self.assertEqual(len(new_bullets), 1)
        self.assertTrue(new_bullets[0]["is_isolated"])
        self.assertEqual(new_bullets[0]["support_count"], 1)

    def test_kept_bullet_verdicts_reindexed_after_earlier_split(self):
        # Bullet 0 (a bundle) splits into 2 one-offs, shifting bullet 1 (a real
        # theme) to position 2. Its verdicts must follow it so the frontend's
        # citation-dropping still lines up.
        bullets = [
            {"text": "Disconnected and aesthetics.", "cited_ids": ["R1", "R2"],
             "quotes": [{"rid": "R1", "text": "q1"}, {"rid": "R2", "text": "q2"}],
             "sentiment": "negative"},
            {"text": "Pace too fast.", "cited_ids": ["R3", "R4"],
             "quotes": [{"rid": "R3", "text": "q3"}], "sentiment": "negative"},
        ]
        verification = self._verif(
            (0, "R1", "supported"), (0, "R2", "supported"),
            (1, "R3", "supported"), (1, "R4", "supported"),
        )
        new_bullets, new_verif = leai_analysis.enforce_coherence(
            bullets, {0}, verification,
        )

        self.assertEqual(len(new_bullets), 3)  # 2 split one-offs + 1 theme
        self.assertFalse(new_bullets[2]["is_isolated"])  # the real theme
        theme_verdicts = {
            v["source_id"] for v in new_verif if v["bullet_index"] == 2
        }
        self.assertEqual(theme_verdicts, {"R3", "R4"})

    def test_does_not_mutate_input(self):
        bullets = [{
            "text": "x", "cited_ids": ["R1", "R2"], "quotes": [], "sentiment": "negative",
        }]
        leai_analysis.enforce_coherence(bullets, {0}, [])
        self.assertEqual(bullets[0]["cited_ids"], ["R1", "R2"])
        self.assertNotIn("support_count", bullets[0])


class CheckBulletCoherenceTest(TestCase):
    """Tests for check_bullet_coherence() — mocks openai_client.run_structured."""

    def _mock(self, parsed):
        return {"response": json.dumps(parsed), "parsed": parsed,
                "usage": {}, "model": "gpt-5.1"}

    def test_returns_results_for_multi_citation_bullets(self):
        corpus = [
            {"rid": "R1", "text": "design is just pretty interfaces",
             "survey_id": 1, "session_id": "s1", "week_number": 1},
            {"rid": "R2", "text": "I feel disconnected",
             "survey_id": 1, "session_id": "s2", "week_number": 1},
        ]
        bullets = [{"text": "disconnected and design is aesthetics",
                    "cited_ids": ["R1", "R2"]}]
        parsed = {"results": [{"bullet_index": 0, "coherent": False}]}
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value=self._mock(parsed),
        ) as m:
            results = leai_analysis.check_bullet_coherence(
                corpus=corpus, bullets=bullets,
            )

        self.assertEqual(results, [{"bullet_index": 0, "coherent": False}])
        self.assertTrue(m.called)

    def test_skips_model_when_no_multi_citation_bullets(self):
        corpus = [{"rid": "R1", "text": "x", "survey_id": 1,
                   "session_id": "s1", "week_number": 1}]
        bullets = [{"text": "single", "cited_ids": ["R1"]}]
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
        ) as m:
            results = leai_analysis.check_bullet_coherence(
                corpus=corpus, bullets=bullets,
            )

        self.assertEqual(results, [])
        self.assertFalse(m.called)

    def test_graceful_on_failure(self):
        corpus = [
            {"rid": "R1", "text": "a", "survey_id": 1, "session_id": "s1", "week_number": 1},
            {"rid": "R2", "text": "b", "survey_id": 1, "session_id": "s2", "week_number": 1},
        ]
        bullets = [{"text": "x and y", "cited_ids": ["R1", "R2"]}]
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            side_effect=Exception("boom"),
        ):
            results = leai_analysis.check_bullet_coherence(
                corpus=corpus, bullets=bullets,
            )

        self.assertEqual(results, [])
