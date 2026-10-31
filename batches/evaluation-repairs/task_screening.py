"""Run and score four-condition screening for the two remaining configured tasks.

Uses the frozen production controller unchanged. The original pilot validator fixes
its task to Outlines; this adapter supplies an equally strict, predeclared task
matrix while retaining every seal, role, input, fingerprint and fresh-session check.
Formal repeats and the formal-protocol freeze flag are unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from common import configuration, save_json, sha256
from intervention import batch
from intervention.controller import Run
from intervention.protocol import CONDITIONS, PROTOCOL, canonical_hash, implementation_manifest

TASKS = ('dirty_equals', 'click')
STATUS = ROOT / 'docs/all-tasks-status.json'
LOGS = ROOT / '.local/all-tasks-screening'
ORIGINAL_PLAN = batch.plan


def expected(task):
    if task not in TASKS:
        raise ValueError('Only the two configured remaining tasks are authorized here')
    assert task in {t['name'] for t in configuration()['tasks']}
    return [{'task': task, 'repeat': 1, 'strategy': condition} for condition in CONDITIONS]


def manifest_path(task):
    return ROOT / f'batches/{task}-fourconditions-screening-{PROTOCOL}.json'


def load_manifest(task):
    path = manifest_path(task)
    data = json.loads(path.read_text())
    assert data['protocol_id'] == PROTOCOL and data['kind'] == 'pilot'
    assert data['purpose'] == 'four_condition_task_screening'
    assert data['batch_id'] == f'{task}-screening-{PROTOCOL}'
    assert data['task'] == task and data['plan'] == expected(task)
    assert data['expected_runs'] == 4 and 0 <= len(data['runs']) <= 4
    assert data['implementation_sha256'] == canonical_hash(implementation_manifest(ROOT))
    assert data['scoring_adapter_sha256'] == sha256(ROOT / 'batches/evaluation-repairs/upstream_test_filter_v1.py')
    return data


@contextmanager
def task_matrix(task):
    batch.plan = lambda kind: expected(task) if kind == 'pilot' else ORIGINAL_PLAN(kind)
    try:
        yield
    finally:
        batch.plan = ORIGINAL_PLAN


def verify(task):
    with task_matrix(task):
        return batch.verify_batch(ROOT, load_manifest(task))


def state(phase, **details):
    progress = {task: len(load_manifest(task)['runs']) for task in TASKS}
    data = {
        'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'protocol_id': PROTOCOL,
        'purpose': 'four_condition_task_screening',
        'status': phase,
        'task_completion': progress,
        'new_runs_sealed': sum(progress.values()),
        'new_runs_expected': 8,
        'prior_outlines_runs_scored': 4,
        'formal_24_started': False,
        'model': 'gpt-6-luna', 'reasoning_effort': 'high',
        'funding_admission': 'live_quota_thresholds',
        **details,
    }
    save_json(STATUS, data)
    print(json.dumps(data, ensure_ascii=False), flush=True)


def readiness(task, condition, attempt):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_readiness.py')],
                            cwd=ROOT, capture_output=True, text=True)
    (LOGS / f'{task}-{condition}-{attempt}-readiness.log').write_text(result.stdout + result.stderr)
    report = json.loads((ROOT / 'docs/start-readiness.json').read_text())
    if result.returncode or not report['ready']:
        state('blocked_before_next_run', task=task, strategy=condition,
              blockers=report['blockers'], hidden_evaluation_invoked=False,
              quota=report['checks']['experiment_start_quota'])
        raise RuntimeError('Start gate blocked; no automatic quota reset/resume')


async def run_remaining():
    for task in TASKS:
        data = load_manifest(task)
        for index, run_path in enumerate(data['runs']):
            seal = batch.verify_sealed(ROOT / run_path)
            assert all(seal[k] == value for k, value in data['plan'][index].items())
            assert seal['common_contract']['implementation_sha256'] == data['implementation_sha256']
        for sample in data['plan'][len(data['runs']):]:
            condition = sample['strategy']
            interrupted = [json.loads(p.read_text()) for p in (ROOT / 'runs').glob('*/summary.json')]
            interrupted = [s for s in interrupted if s.get('batch_id') == data['batch_id']
                           and s['strategy'] == condition and s['status'] != 'sealed']
            if len(interrupted) > 1 or any(s['status'] not in ('infrastructure_interrupted', 'funding_paused') for s in interrupted):
                state('retry_not_permitted', task=task, strategy=condition, hidden_evaluation_invoked=False)
                raise RuntimeError('Retry budget consumed or previous error is not infrastructure')
            for attempt in range(len(interrupted), 2):
                readiness(task, condition, attempt)
                run = Run(task, condition, json.loads((ROOT / 'configs/pilot.json').read_text()),
                          kind='pilot', repeat=1, batch_id=data['batch_id'])
                run.report.update(purpose='four_condition_task_screening',
                                  screening_manifest_sha256=sha256(manifest_path(task)),
                                  screening_driver_sha256=sha256(Path(__file__)))
                state('running', task=task, strategy=condition, attempt=attempt,
                      current_run=str(run.output.relative_to(ROOT)), hidden_evaluation_invoked=False)
                try:
                    path = await run.execute()
                except BaseException as error:
                    summary = json.loads((run.output / 'summary.json').read_text())
                    recoverable = summary['status'] == 'infrastructure_interrupted' and attempt == 0
                    state('infrastructure_retry_pending' if recoverable else 'run_interrupted',
                          task=task, strategy=condition, current_run=str(run.output.relative_to(ROOT)),
                          error=f'{type(error).__name__}: {error}', hidden_evaluation_invoked=False)
                    if not recoverable:
                        raise
                    # Service backoff is not an experiment duration or tool budget.
                    await asyncio.sleep(30)
                    continue
                seal = batch.verify_sealed(path)
                assert all(seal[k] == value for k, value in sample.items())
                data['runs'].append(str(path.relative_to(ROOT)))
                save_json(manifest_path(task), data)
                state('slot_sealed', task=task, strategy=condition,
                      current_run=str(path.relative_to(ROOT)), sessions_closed=seal['sessions_closed'],
                      merge_ok=seal['p0']['merge_ok'], hidden_evaluation_invoked=False)
                break
        verify(task)


def score_all():
    # Validate both complete task matrices before any new-task hidden evaluation.
    for task in TASKS:
        verify(task)
    state('unified_scoring', all_new_sessions_closed=True, hidden_evaluation_invoked=True)
    source = ROOT / 'batches/evaluation-repairs/upstream_test_filter_v1.py'
    for task in TASKS:
        spec = importlib.util.spec_from_file_location(f'grading_{task}', source)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with task_matrix(task):
            sys.argv = [str(source), '--manifest', str(manifest_path(task))]
            adapter.main()
        # The archived adapter wraps these globals; reset them between task batches.
        adapter.evaluator.archive = adapter.original_archive
        adapter.evaluator.score_one = adapter.original_score
    state('all_tasks_scored', all_new_sessions_closed=True, hidden_evaluation_invoked=True,
          evaluations=[str(manifest_path(t).with_suffix('.evaluation.json').relative_to(ROOT)) for t in TASKS])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--score-only', action='store_true')
    args = parser.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        for task in TASKS:
            path = manifest_path(task)
            if not path.exists():
                save_json(path, {'protocol_id': PROTOCOL, 'kind': 'pilot',
                                'purpose': 'four_condition_task_screening', 'task': task,
                                'batch_id': f'{task}-screening-{PROTOCOL}',
                                'expected_runs': 4, 'preregistered': False,
                                'plan': expected(task), 'runs': [],
                                'implementation_sha256': canonical_hash(implementation_manifest(ROOT)),
                                'scoring_adapter_sha256': sha256(ROOT / 'batches/evaluation-repairs/upstream_test_filter_v1.py')})
            load_manifest(task)
        state('prepared', hidden_evaluation_invoked=False)
        return
    if not args.score_only:
        asyncio.run(run_remaining())
    score_all()


if __name__ == '__main__':
    main()
