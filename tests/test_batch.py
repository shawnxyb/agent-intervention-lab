import asyncio
import json
from collections import Counter

import pytest
from common import save_json
from intervention.batch import plan, verify_batch, verify_sealed
from intervention.merge import digest
from intervention.protocol import PROTOCOL


def reseal(run, mutate):
    summary = json.loads((run / "summary.json").read_text())
    summary.pop("seal_sha256")
    mutate(summary)
    save_json(run / "seal.json", summary)
    summary["seal_sha256"] = digest((run / "seal.json").read_bytes())
    save_json(run / "summary.json", summary)


@pytest.fixture
def pilot(run_factory):
    paths = []
    for condition in ("single", "static", "periodic", "adaptive"):
        run, _, _ = run_factory(condition)
        paths.append(asyncio.run(run.execute()))
    manifest = {
        "protocol_id": PROTOCOL,
        "kind": "pilot",
        "expected_runs": 4,
        "batch_id": "outlines-" + PROTOCOL,
        "plan": plan("pilot"),
        "runs": [str(p) for p in paths],
    }
    return paths, manifest


def test_four_condition_batch_accepts_p0_only_and_rejects_missing_or_mixed(pilot):
    paths, manifest = pilot
    root = paths[0].parents[1]
    assert len(verify_batch(root, manifest)[1]) == 4
    with pytest.raises(ValueError, match="incomplete"):
        verify_batch(root, manifest | {"runs": manifest["runs"][:3]})
    swapped = list(manifest["runs"])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    with pytest.raises(ValueError, match="ordering"):
        verify_batch(root, manifest | {"runs": swapped})
    reseal(paths[1], lambda s: s.update(batch_id="different"))
    with pytest.raises(ValueError, match="Mixed"):
        verify_batch(root, manifest)


def test_batch_rejects_edited_hidden_input_before_scoring(pilot):
    paths, manifest = pilot
    root = paths[0].parents[1]
    (root / "vendor/CooperBench/dataset/toy/task1/feature1/tests.patch").write_text("changed")
    with pytest.raises(ValueError, match="changed after development"):
        verify_batch(root, manifest)


def test_scoring_does_not_create_client_until_every_run_is_verified(monkeypatch, pilot):
    from unittest.mock import Mock

    import evaluate_candidates as evaluator

    paths, manifest = pilot
    root = paths[0].parents[1]
    manifest_path = root / "batch.json"
    save_json(manifest_path, manifest)
    (paths[-1] / "p0.patch").write_text("tamper")
    client = Mock(side_effect=AssertionError("Must not connect to Docker"))
    monkeypatch.setattr(evaluator, "docker_client", client)
    monkeypatch.setattr(evaluator, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["evaluator", "--manifest", str(manifest_path)])
    with pytest.raises(ValueError, match="modified after sealing"):
        evaluator.main()
    client.assert_not_called()


def test_candidate_origin_and_role_permissions_are_verified(pilot):
    paths, _ = pilot
    reseal(paths[0], lambda s: s["p0"].update(merge_ok=True))
    with pytest.raises(ValueError, match="Single"):
        verify_sealed(paths[0])
    reseal(paths[3], lambda s: s["role_settings"]["manager"]["tools"].append("container_exec"))
    with pytest.raises(ValueError, match="permissions"):
        verify_sealed(paths[3])


def test_formal_matrix_has_24_samples_and_approximate_position_balance():
    samples = plan("formal")
    assert len(samples) == 24 and len(plan("pilot")) * 2 == 8
    assert len(samples) * 2 == 48
    assert set(Counter((s["task"], s["strategy"]) for s in samples).values()) == {3}
    positions = Counter((s["strategy"], i % 4) for i, s in enumerate(samples))
    assert set(positions.values()) == {1, 2}


def test_pilot_scoring_uses_two_feature_units_per_candidate_and_reports_unknowns(
    monkeypatch, pilot
):
    from unittest.mock import Mock

    import evaluate_candidates as evaluator

    paths, manifest = pilot
    root = paths[0].parents[1]
    manifest_path = root / "batch.json"
    save_json(manifest_path, manifest)
    client = Mock()
    monkeypatch.setattr(evaluator, "docker_client", Mock(return_value=client))
    monkeypatch.setattr(evaluator, "ROOT", root)
    calls = []

    def score(_client, run, summary, stage, feature):
        calls.append((run, feature))
        # An evaluator failure is unknown joint performance, never a code failure.
        outcome = (
            "evaluation_infrastructure_error"
            if summary["strategy"] == "single" and feature == 2
            else "passed"
        )
        return {"stage": stage, "feature": feature, "outcome": outcome}

    monkeypatch.setattr(evaluator, "score_one", score)
    monkeypatch.setattr("sys.argv", ["evaluator", "--manifest", str(manifest_path)])
    evaluator.main()
    assert len(calls) == 8 and len(set(calls)) == 8
    report = json.loads(manifest_path.with_suffix(".evaluation.json").read_text())
    assert report["feature_evaluation_units"] == 8
    assert len(report["attempt_costs_including_interruptions"]) == 4
    single = report["samples"][0]
    assert single["merge_ok"] is None and single["joint_success"] is None
    assert all(r["joint_success"] for r in report["samples"][1:])
    client.close.assert_called_once()


def test_initial_manager_plan_is_immutable_and_required(pilot):
    paths, _ = pilot
    (paths[1] / "initial-plan.json").write_text('{"assignments": []}')
    with pytest.raises(ValueError):
        verify_sealed(paths[1])
    (paths[2] / "initial-plan.json").unlink()
    with pytest.raises(ValueError, match="Incomplete"):
        verify_sealed(paths[2])


@pytest.mark.parametrize("version", ["v4", "v5"])
def test_legacy_candidates_cannot_enter_the_new_protocol(pilot, version):
    paths, _ = pilot
    reseal(paths[0], lambda s: s.update(protocol_id="p0-fourconditions-high-" + version))
    with pytest.raises(ValueError, match="Legacy"):
        verify_sealed(paths[0])


@pytest.mark.parametrize(
    "task",
    [
        "dirty_equals",
        "click",
        "dspy_8394",
        "dspy_8587",
        "click_2068",
        "datasets_3997",
        "dspy_8563",
        "chi_26",
    ],
)
def test_task_pilot_has_exactly_four_conditions_without_changing_formal_plan(task):
    matrix = plan("pilot", task)
    assert [r["strategy"] for r in matrix] == ["single", "static", "periodic", "adaptive"]
    assert all(r["task"] == task and r["repeat"] == 1 for r in matrix)
    assert len(plan("formal")) == 24
    with pytest.raises(ValueError, match="Unknown task"):
        plan("pilot", "unregistered")
    with pytest.raises(ValueError, match="Formal task order"):
        plan("formal", task)


def test_manifest_task_cannot_relabel_existing_candidate_batch(pilot):
    paths, manifest = pilot
    with pytest.raises(ValueError, match="predeclared"):
        verify_batch(paths[0].parents[1], manifest | {"task": "click"})
