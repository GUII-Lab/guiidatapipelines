"""Bounded synthetic fixed-prefix B/C ablation. No DeepEval or cloud uploads.

Run with the backend on PYTHONPATH. Results are local JSON, not a semantic quality score.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import subprocess

from datapipeline.orchestration_client import run_orchestration, OrchestrationUnavailable, INSTRUCTIONS
from datapipeline.openai_client import get_client
from leai.services.response_flow import begin_flow


def cases(protocol):
    base = begin_flow(protocol)
    opening = [{'sequence': 1, 'role': 'assistant', 'content': protocol['intro']},
               {'sequence': 2, 'role': 'assistant', 'content': protocol['sections'][0]['items'][0]['prompt']}]
    specifications = [
        ('sufficient', 'I choose information based on the assignment goal, include the rubric and leave out names and private details.', 'answered', 1, True),
        ('bare_yes', 'Yeah.', 'partial', 0, True),
        ('clarification', 'I do not get it. Could you give me an example of what you mean?', None, 0, False),
        ('decline', 'Can we leave this one out? I would rather keep that to myself.', 'declined', 1, True),
        ('negative', 'No, I just paste my assignment without thinking about which information belongs there.', 'answered', 1, True),
        ('mixed', 'I use the task goal to choose context. What do you mean by information the AI needs?', 'answered', 1, True),
    ]
    result = []
    for case_id, text, status, index, mapped in specifications:
        result.append({'id': case_id, 'state': deepcopy(base), 'messages': deepcopy(opening) + [
            {'sequence': 3, 'role': 'student', 'content': text}],
            'expect': {'status': status, 'index': index, 'mapped': mapped}})
    revised_state = deepcopy(base)
    revised_state.update(item_index=1, results={'P1': {'rating': None, 'status': 'answered', 'probes': 0}},
                         answer_map={'P1': [3]}, evidence_seen={'P1': True}, coverage_seen={'P1': ['decision_process']})
    result.append({'id': 'implicit_revision', 'state': revised_state, 'messages': deepcopy(opening) + [
        {'sequence': 3, 'role': 'student', 'content': 'I always remove names and choose only context relevant to the task.'},
        {'sequence': 4, 'role': 'assistant', 'content': protocol['sections'][0]['items'][1]['prompt']},
        {'sequence': 5, 'role': 'student', 'content': 'That was not true about removing names. I actually paste everything without checking relevance. But for what AI needs, I include the rubric and deadline.'}],
        'expect': {'revision': True}})
    return result


def check(case, result):
    state = result['state']; expected = case['expect']; failures = []
    if expected.get('revision'):
        if 3 in state['answer_map'].get('P1', []) or 5 not in state['answer_map'].get('P1', []):
            failures.append('old evidence was not superseded')
        if 5 not in state['answer_map'].get('P2', []):
            failures.append('current answer in mixed revision was lost')
    else:
        if state['item_index'] != expected['index']:
            failures.append('unexpected cursor')
        if state['results'].get('P1', {}).get('status') != expected['status']:
            failures.append('unexpected answer status')
        if bool(state['answer_map'].get('P1')) != expected['mapped']:
            failures.append('unexpected answer mapping')
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=2, choices=range(1, 4))
    parser.add_argument('--max-total-tokens', type=int, default=400000)
    args = parser.parse_args()
    # A missing provider configuration is a setup error, not 28 model failures.
    client = get_client()
    protocol = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/ulia_conversational.json').read_text())
    root = Path(__file__).resolve().parents[2]
    source_files = ('leai/evals/option1.py', 'leai/services/orchestration.py', 'datapipeline/orchestration_client.py')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'kind': 'fixed_prefix_not_persona_or_browser',
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'prompt_sha256': hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
              'working_source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_files},
              'protocol': protocol, 'synthetic_only': True, 'runs': [], 'semantic_review': 'human_required'}
    used = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        for case in cases(protocol):
            # Alternate arm order to reduce systematic warm-cache/order bias.
            for tools in ([False, True] if repeat % 2 == 0 else [True, False]):
                # Conservative reservation covers three requests, appended outputs and tool overhead.
                reserve = 3 * (len(json.dumps(case, ensure_ascii=False).encode()) + len(json.dumps(protocol).encode())
                               + len(INSTRUCTIONS.encode()) + 60000)
                if used + reserve > args.max_total_tokens:
                    report['stop_reason'] = 'conservative_token_budget'; break
                run = {'case': case['id'], 'repeat': repeat, 'arm': 'C_tools' if tools else 'B_context',
                       'input': case, 'failures': []}
                try:
                    result = run_orchestration(protocol, case['state'], case['messages'], tools_enabled=tools, client=client)
                    run.update(result)
                    run['failures'] = check(case, result)
                except OrchestrationUnavailable as error:
                    run.update(metrics=error.metrics, failures=['technical_failure'])
                counts = [call.get('total_tokens') for call in run['metrics']['calls']]
                used += sum(counts) if all(n is not None for n in counts) else reserve
                report['runs'].append(run)
                report['budget_charged_tokens'] = used
                args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
                print(json.dumps({'case': case['id'], 'arm': run['arm'], 'failures': run['failures'],
                                  'tokens': counts, 'ms': run['metrics']['turn_processing_ms']}), flush=True)
            if report.get('stop_reason'): break
        if report.get('stop_reason'): break
    report['summary'] = {}
    for arm in ['B_context', 'C_tools']:
        runs = [r for r in report['runs'] if r['arm'] == arm]
        times = [r['metrics']['turn_processing_ms'] for r in runs]
        report['summary'][arm] = {'runs': len(runs), 'structural_passes': sum(not r['failures'] for r in runs),
                                'median_processing_ms': statistics.median(times) if times else None,
                                'actual_tool_calls': sum(len(r['metrics']['tools']) for r in runs)}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report['summary']), flush=True)


if __name__ == '__main__':
    main()
