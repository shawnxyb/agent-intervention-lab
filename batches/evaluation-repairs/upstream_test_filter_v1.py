"""Replay a sealed batch using CooperBench's own candidate-test-file filter.

Keeps the frozen controller/source fingerprint and all original candidate bytes.
The evaluator payload alone uses the upstream source-only grading projection.
Run from the project root with .venv/bin/python and --manifest <pilot.json>.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import evaluate_candidates as evaluator
from common import save_json, sha256
from intervention.batch import verify_batch

SOURCE = ROOT / 'vendor/CooperBench/src/cooperbench/eval/sandbox.py'
text = SOURCE.read_text()
tree = ast.parse(text)
constants = {'_TEST_DIR_RE', '_TEST_FILE_RE'}
functions = {'_is_test_path', '_filter_test_files'}
nodes = [n for n in tree.body if (
    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)
) or (isinstance(n, ast.FunctionDef) and n.name in functions)]
assert len(nodes) == 4
namespace = {'re': re}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
filter_tests = namespace['_filter_test_files']
is_test_path = namespace['_is_test_path']
original_archive = evaluator.archive
original_score = evaluator.score_one


def projected(patch):
    result = filter_tests(patch.decode()).encode()
    blocks = [b for b in re.split(b'(?=^diff --git )', patch, flags=re.MULTILINE) if b]
    expected = b''.join(b for b in blocks if not is_test_path(b.splitlines()[0].decode()))
    assert result == expected, 'Upstream filter changed a non-test diff'
    return result


def payload_archive(payload):
    if 'patches/candidate.patch' in payload:
        payload = {**payload, 'patches/candidate.patch': projected(payload['patches/candidate.patch'])}
    return original_archive(payload)


def score_one(client, run, summary, stage, feature, **kwargs):
    candidate = (run / f'{stage}.patch').read_bytes()
    effective = projected(candidate)
    record = original_score(client, run, summary, stage, feature, **kwargs)
    record.update(
        evaluation_revision='upstream_test_filter_v1',
        evaluated_patch_sha256=hashlib.sha256(effective).hexdigest(),
        candidate_tests_filtered=(candidate != effective),
        upstream_filter_source='vendor/CooperBench/src/cooperbench/eval/sandbox.py',
        upstream_filter_source_sha256=sha256(SOURCE),
        scoring_adapter_sha256=sha256(Path(__file__)),
        original_candidate_unchanged=(sha256(run / f'{stage}.patch') == record['patch_sha256']),
    )
    assert record['original_candidate_unchanged']
    destination = run / record['attempt_path']
    (destination / 'evaluated.patch').write_bytes(effective)
    save_json(destination / 'result.json', record)
    save_json(run / 'evaluation' / f'{stage}-feature{feature}' / 'result.json', record)
    return record


def main():
    manifest_path = Path(sys.argv[sys.argv.index('--manifest') + 1]).resolve()
    manifest = json.loads(manifest_path.read_text())
    runs, summaries = verify_batch(ROOT, manifest)
    for run, summary in zip(runs, summaries, strict=True):
        if summary['p0']['merge_ok'] is False:
            continue
        projected((run / 'p0.patch').read_bytes())
    aggregate = manifest_path.with_suffix('.evaluation.json')
    old_aggregate = manifest_path.with_suffix('.evaluation.initial.json')
    if aggregate.exists() and not old_aggregate.exists():
        shutil.copyfile(aggregate, old_aggregate)
    for run, summary in zip(runs, summaries, strict=True):
        old_evaluation = run / 'evaluation.initial.json'
        if (run / 'evaluation.json').exists() and not old_evaluation.exists():
            shutil.copyfile(run / 'evaluation.json', old_evaluation)
        if summary['p0']['merge_ok'] is False:
            continue
        for feature in summary['inputs']['task']['features']:
            result = run / 'evaluation' / f'p0-feature{feature}' / 'result.json'
            if result.exists():
                record = json.loads(result.read_text())
                if record.get('evaluation_revision') == 'upstream_test_filter_v1':
                    continue
                backup = result.with_name('initial-result.json')
                assert not backup.exists(), 'Never overwrite first-pass evidence'
                result.rename(backup)
    evaluator.archive = payload_archive
    evaluator.score_one = score_one
    evaluator.main()
    verify_batch(ROOT, manifest)
    report = json.loads(aggregate.read_text())
    report.update(
        evaluation_revision='upstream_test_filter_v1',
        initial_evaluation=str(old_aggregate.relative_to(ROOT)),
        upstream_filter_source_sha256=sha256(SOURCE),
        scoring_adapter_sha256=sha256(Path(__file__)),
        sealed_candidates_unchanged=True,
    )
    save_json(aggregate, report)


if __name__ == '__main__':
    main()
