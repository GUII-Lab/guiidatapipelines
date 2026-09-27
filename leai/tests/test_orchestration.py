"""Contract tests for semantic proposals; no provider required."""
import importlib
import json
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from leai.services.response_flow import begin_flow


def proposal(updates=(), action="clarify", item="P1", reply="For example, the assignment goal or constraints. What did you consider?"):
    return {"updates": list(updates), "action": action, "item_id": item,
            "reply": reply}


def update(item="P1", sequence=3, quote="I use the task goal.", status="answered"):
    return {"item_id": item, "status": status, "evidence": [{"sequence": sequence, "quote": quote}],
            "supersede_sequences": [], "targets": [{"id": "decision_process", "status": "covered"}] if item == "P1" else []}


class OrchestrationContractTests(TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("leai.services.orchestration"),
                             "semantic orchestrator has not been implemented")
        self.engine = importlib.import_module("leai.services.orchestration")
        self.protocol = json.loads((Path(__file__).parents[1] / "fixtures/ulia_conversational.json").read_text())
        self.state = begin_flow(self.protocol)
        self.messages = [{"sequence": 3, "role": "student", "content": "I use the task goal."}]

    def apply(self, p, state=None, messages=None):
        return self.engine.preview_proposal(self.protocol, state or self.state, messages or self.messages, p)

    def test_clarification_does_not_record_answer_or_advance(self):
        state, reply = self.apply(proposal())
        self.assertEqual(state["answer_map"], {})
        self.assertEqual(state["item_index"], 0)
        self.assertIn("assignment goal", reply)

    def test_clarifying_an_earlier_question_reopens_conversation_not_download(self):
        self.state.update(item_index=11, phase="complete", pending_prompt=None,
                          presented_main_ids=[q["id"] for q in self.engine.items(self.protocol)])
        state, _ = self.apply(proposal())
        self.assertEqual(state["item_index"], 0)
        self.assertEqual(state["phase"], "answer")
        self.assertIsNone(state["pending_prompt"])

    def test_clarifying_other_item_does_not_keep_unrelated_probe(self):
        self.state.update(item_index=1, phase="probe", pending_prompt="Why that deadline?",
                          presented_main_ids=["P1", "P2"])
        state, _ = self.apply(proposal())
        self.assertEqual(state["phase"], "answer")
        self.assertIsNone(state["pending_prompt"])

    def test_main_question_needs_only_one_authoritative_target(self):
        p = proposal([update()], action="ask_main", item="P2", reply="Thanks.")
        state, reply = self.apply(p)
        self.assertEqual(state["item_index"], 1)
        self.assertIn("Do you consider what information", reply)

    def test_next_main_question_is_rendered_once_if_model_appends_it(self):
        stem = self.protocol['sections'][0]['items'][1]['prompt']
        _, reply = self.apply(proposal([update()], action='ask_main', item='P2',
                                       reply='You described your choice.\n\n' + stem))
        self.assertEqual(reply, 'You described your choice.\n\n' + stem)

    def test_embedded_main_question_is_rejected_for_repair_not_repeated(self):
        stem = self.protocol['sections'][0]['items'][1]['prompt']
        with self.assertRaisesRegex(ValueError, 'main question'):
            self.apply(proposal([update()], action='ask_main', item='P2',
                                reply='Next: ' + stem + ' Please answer that.'))

    def test_exact_main_is_rendered_from_protocol_and_multiple_answers_map(self):
        p = proposal([update(), update("P2")], action="ask_main", item="P2", reply="You mentioned the task goal. Does that capture what you meant?")
        state, reply = self.apply(p)
        self.assertEqual(state["answer_map"], {"P1": [3], "P2": [3]})
        self.assertIn("Do you consider what information the AI needs to perform the task?", reply)
        self.assertEqual(self.state["results"], {})

    def test_revision_removes_stale_evidence_and_coverage(self):
        self.state.update(results={"P1": {"rating": None, "status": "answered", "probes": 1}},
                          answer_map={"P1": [3]}, coverage_seen={"P1": ["decision_process"]})
        revised = update(sequence=5, quote="I rarely think about it.", status="partial")
        revised.update(supersede_sequences=[3], targets=[{"id": "decision_process", "status": "not_applicable"}])
        self.messages.append({"sequence": 5, "role": "student", "content": "I rarely think about it."})
        state, _ = self.apply(proposal([revised], action="follow_up", reply="What usually happens instead?"))
        self.assertEqual(state["answer_map"]["P1"], [5])
        self.assertEqual(state["coverage_seen"]["P1"], [])
        self.assertEqual(state["results"]["P1"]["probes"], 2)

    def test_nonexistent_or_assistant_evidence_is_rejected(self):
        for evidence in ([{"sequence": 99, "quote": "invented"}], [{"sequence": 3, "quote": "not in the message"}]):
            u = update(); u["evidence"] = evidence
            with self.assertRaises(ValueError):
                self.apply(proposal([u]))
        self.messages[0]["role"] = "assistant"
        with self.assertRaises(ValueError):
            self.apply(proposal([update()]))

    def test_internal_question_codes_cannot_leak(self):
        with self.assertRaises(ValueError):
            self.apply(proposal(reply="I updated M2. Continue above."))

    def test_probe_budget_never_resets_or_marks_partial_answer_complete(self):
        self.state["results"]["P1"] = {"rating": None, "status": "partial", "probes": 2}
        with self.assertRaises(ValueError):
            self.apply(proposal([update(status="partial")], action="follow_up"))

    def test_clarification_can_preserve_substantive_answer_in_same_message(self):
        state, _ = self.apply(proposal([update(status="partial")]))
        self.assertEqual(state["answer_map"]["P1"], [3])
        self.assertEqual(state["item_index"], 0)

    def test_unknown_is_not_automatically_a_refusal(self):
        u = update(status="unknown"); u["targets"] = []
        p = proposal([u], action="ask_main", item="P2", reply="We can move on.")
        state, _ = self.apply(p)
        self.assertEqual(state["results"]["P1"]["status"], "unknown")

    def test_cannot_skip_unpresented_main_questions(self):
        p = proposal([update()], action="ask_main", item="P3")
        with self.assertRaises(ValueError):
            self.apply(p)

    def test_shared_retracted_evidence_requires_all_affected_questions(self):
        self.state["answer_map"] = {"P1": [3], "P2": [3]}
        u = update(sequence=5, quote="That was wrong.", status="unknown")
        u.update(supersede_sequences=[3], targets=[])
        self.messages.append({"sequence": 5, "role": "student", "content": "That was wrong."})
        with self.assertRaises(ValueError):
            self.apply(proposal([u]))

    def test_retracted_quote_cannot_be_added_back_in_same_proposal(self):
        self.state['answer_map'] = {'P1': [3]}
        self.messages.append({'sequence': 5, 'role': 'student', 'content': 'That was wrong.'})
        u = update(sequence=5, quote='That was wrong.', status='unknown')
        u.update(supersede_sequences=[3], targets=[])
        u['evidence'].append({'sequence':3, 'quote':'I use the task goal.'})
        with self.assertRaises(ValueError):
            self.apply(proposal([u]))

    def test_historically_retired_quote_cannot_be_reintroduced(self):
        self.state['superseded_evidence'] = {'P1':[{'sequence':3,'start':0,'end':20}]}
        with self.assertRaises(ValueError):
            self.apply(proposal([update()]))

    def test_shared_retirement_must_remove_the_same_claim_from_other_item(self):
        self.state['answer_map'] = {'P1':[3], 'P2':[3]}
        self.messages.append({'sequence':5,'role':'student','content':'That was wrong.'})
        u=update(sequence=5,quote='That was wrong.',status='unknown')
        u.update(supersede_sequences=[3],targets=[])
        other=update('P2',sequence=5,quote='That was wrong.',status='unknown')
        with self.assertRaises(ValueError):
            self.apply(proposal([u,other]))

    def test_partial_retirement_preserves_explicitly_retained_unrelated_span(self):
        self.messages[0]['content']='I remove names. I include the rubric.'
        self.state['answer_map']={'P1':[3],'P2':[3]}
        self.state['evidence']={'P1':[{'sequence':3,'start':0,'end':37}],
                                'P2':[{'sequence':3,'start':16,'end':37}]}
        self.messages.append({'sequence':5,'role':'student','content':'I do not remove names.'})
        u=update(sequence=5,quote='I do not remove names.')
        u.update(supersede_sequences=[3])
        u['evidence'].append({'sequence':3,'quote':'I include the rubric.'})
        state,_=self.apply(proposal([u]))
        self.assertEqual(state['answer_map']['P1'],[3,5])
        self.assertEqual(state['evidence']['P2'],self.state['evidence']['P2'])
        self.assertEqual(state['superseded_evidence']['P1'],[{'sequence':3,'start':0,'end':16}])

    def test_existing_map_without_spans_keeps_original_answer_when_adding_detail(self):
        self.state['answer_map']={'P1':[3]}
        self.messages.append({'sequence':5,'role':'student','content':'I also remove names.'})
        u=update(sequence=5,quote='I also remove names.')
        state,_=self.apply(proposal([u]))
        self.assertEqual({r['sequence'] for r in state['evidence']['P1']},{3,5})

    def test_existing_extracted_answer_is_not_expanded_on_clarification(self):
        self.state['answer_map']={'P1':[3]}
        text='Earlier I said I remove names. That was wrong: I paste everything. What does that mean?'
        self.messages[0].update(content=text,attribution={'answer_text':'I paste everything.'})
        state,_=self.apply(proposal())
        refs=state['evidence']['P1']
        self.assertEqual([text[r['start']:r['end']] for r in refs],['I paste everything.'])

    def test_unrepresentable_existing_extraction_is_not_silently_expanded(self):
        self.state['answer_map']={'P1':[3]}
        for extracted in ['paraphrase not in original', '']:
            self.messages[0]['attribution']={'answer_text':extracted}
            with self.assertRaises(ValueError):
                self.apply(proposal())
