"""Regrade sealed DSPy candidates with validated, additive dependency constraints.

No experiment sources, sealed inputs, candidates, or upstream runners are changed.
The same constraints are enforced for baseline, reference, and candidate evaluation.
"""

from __future__ import annotations

import fcntl
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_candidates as evaluator
import validate_evaluators as controls
from common import configuration, docker_client, image_locks, save_json, sha256
from intervention.batch import verify_batch

REVISION = "dspy_dependency_constraints_v1"
STATUS = ROOT / "docs/dspy8-revaluation-status.json"
OLD_CONTROLS = ROOT / "artifacts/environment/controls/20261021T114917.202884Z"
TASKS = ("dspy_8394", "dspy_8587")
FILTER_PATH = ROOT / "batches/evaluation-repairs/upstream_test_filter_v1.py"


def load_filter():
    spec = importlib.util.spec_from_file_location("upstream_projection", FILTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pins(text):
    return (
        "\n".join(line for line in text.splitlines() if "==" in line and not line.startswith("#"))
        + "\n"
    )


class ConstrainedContainers:
    def __init__(self, containers):
        self.containers = containers

    def create(self, *args, **kwargs):
        kwargs["environment"] = {
            **kwargs.get("environment", {}),
            "PIP_CONSTRAINT": "/environment/constraints.txt",
            "UV_CONSTRAINT": "/environment/constraints.txt",
        }
        return self.containers.create(*args, **kwargs)


class ConstrainedClient:
    def __init__(self, client):
        self.images = client.images
        self.containers = ConstrainedContainers(client.containers)


def main():
    lock = (ROOT / ".local/dspy8-revaluation.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/evaluation-repairs" / f"{REVISION}-{stamp}"
    output.mkdir(parents=True)
    state = {
        "status": "running",
        "phase": "verification",
        "revision": REVISION,
        "artifact_root": str(output.relative_to(ROOT)),
        "model_inference_invoked": False,
        "controls_completed": 0,
        "controls_expected": 12,
        "feature_evaluations_completed": 0,
        "feature_evaluations_expected": 16,
        "all_sealed_candidates_unchanged": False,
    }

    def update(**kwargs):
        state.update(updated_at=datetime.now(UTC).isoformat(), **kwargs)
        save_json(STATUS, state)
        save_json(output / "status.json", state)

    def check_unchanged():
        for path, expected in immutable.items():
            if sha256(ROOT / path) != expected:
                raise RuntimeError(f"Sealed artifact changed: {path}")
        for manifest in manifests:
            verify_batch(ROOT, manifest)

    client = None
    try:
        update()
        progress = json.loads((ROOT / "docs/dspy8-experiments-status.json").read_text())
        assert progress["completed_attempts"] == progress["sealed_runs"] == 8
        manifest_paths = [ROOT / p for p in progress["manifests"]]
        manifests = [json.loads(p.read_text()) for p in manifest_paths]
        assert [m["task"] for m in manifests] == list(TASKS)
        immutable = {}
        runs = []
        for manifest in manifests:
            paths, summaries = verify_batch(ROOT, manifest)
            runs.extend(paths)
            for run, summary in zip(paths, summaries, strict=True):
                required = set(summary["evidence_sha256"]) | {
                    "p0.patch",
                    "seal.json",
                    "summary.json",
                    "implementation.json",
                }
                for relative in required:
                    path = run / relative
                    immutable[str(path.relative_to(ROOT))] = sha256(path)
                shutil.copytree(run / "evaluation", output / "original-evaluations" / run.name)
                shutil.copy2(
                    run / "evaluation.json",
                    output / "original-evaluations" / run.name / "aggregate.json",
                )
        for path in manifest_paths:
            immutable[str(path.relative_to(ROOT))] = sha256(path)
            shutil.copy2(
                path.with_suffix(".evaluation.json"),
                output / (path.stem + ".original-evaluation.json"),
            )
        for name in ("dspy8-results.json", "dspy8-results.md"):
            shutil.copy2(ROOT / "docs" / name, output / ("original-" + name))
        save_json(output / "immutable-artifacts.json", immutable)
        shutil.copy2(Path(__file__), output / Path(__file__).name)
        repaired = {}
        provenance = {}
        for task in TASKS:
            sources = sorted(OLD_CONTROLS.glob(task + "-*-*gold/pip.after.txt"))
            assert len(sources) == 4
            texts = [pins(p.read_text()) for p in sources]
            assert len(set(texts)) == 1, "Reference control environments must agree"
            target = output / (task + ".constraints.txt")
            target.write_text(texts[0])
            repaired[task] = target
            provenance[task] = {
                "constraints_path": str(target.relative_to(ROOT)),
                "constraints_sha256": sha256(target),
                "source_controls": {str(p.relative_to(ROOT)): sha256(p) for p in sources},
                "original_constraints_sha256": sha256(
                    ROOT / f"docs/environment-dependencies/{task}.freeze.txt"
                ),
            }
        assert repaired[TASKS[0]].read_bytes() == repaired[TASKS[1]].read_bytes()
        save_json(output / "constraints-provenance.json", provenance)
        projection = load_filter()
        original_archive = controls.archive
        current = {}

        def payload_archive(payload):
            payload = {
                **payload,
                "environment/constraints.txt": current["constraints"].read_bytes(),
            }
            if "patches/candidate.patch" in payload:
                payload["patches/candidate.patch"] = projection.projected(
                    payload["patches/candidate.patch"]
                )
            return original_archive(payload)

        # Compile the frozen scoring function, replacing only its constraints path.
        # The unchanged upstream runner, test projection and outcome rules are reused.
        old = (
            "constraints_path = ROOT / f\"docs/environment-dependencies/{task['name']}.freeze.txt\""
        )
        source = inspect.getsource(evaluator.score_one)
        assert source.count(old) == 1
        namespace = {
            **vars(evaluator),
            "REPAIRED_CONSTRAINTS": repaired,
            "archive": payload_archive,
        }
        exec(  # noqa: S102 - compile only the verified local evaluator
            compile(
                source.replace(old, 'constraints_path = REPAIRED_CONSTRAINTS[task["name"]]'),
                str(Path(__file__)),
                "exec",
            ),
            namespace,
        )
        score = namespace["score_one"]
        controls.archive = payload_archive
        client = docker_client(timeout=1900)
        constrained_client = ConstrainedClient(client)
        tasks = {t["name"]: t for t in configuration()["tasks"]}
        results = []
        update(phase="controls", constraints=provenance)
        for task in TASKS:
            current["constraints"] = repaired[task]
            for feature in tasks[task]["features"]:
                for kind in ("baseline", "single_gold", "combined_gold"):
                    result = controls.validate_one(
                        constrained_client,
                        tasks[task],
                        image_locks()[task],
                        feature,
                        kind,
                        output,
                        1800,
                    )
                    destination = output / result["case"]
                    actual = set((destination / "pip.after.txt").read_text().splitlines())
                    result["dependency_pins_match"] = set(
                        repaired[task].read_text().splitlines()
                    ).issubset(actual)
                    result["constraints_sha256"] = sha256(repaired[task])
                    save_json(destination / "result.json", result)
                    results.append(result)
                    save_json(output / "controls.json", results)
                    update(controls_completed=len(results), active_control=result["case"])
                    if not result["control_ok"] or not result["dependency_pins_match"]:
                        raise RuntimeError(f"Repaired environment control failed: {result['case']}")
        check_unchanged()
        update(phase="scoring", active_control=None, controls_passed=True)

        def repaired_score(client_, run, summary, stage, feature, **kwargs):
            task = summary["task"]
            current["constraints"] = repaired[task]
            record = score(client_, run, summary, stage, feature, **kwargs)
            effective = projection.projected((run / f"{stage}.patch").read_bytes())
            record.update(
                evaluation_revision=REVISION,
                dependency_repair=provenance[task],
                repair_driver_sha256=sha256(Path(__file__)),
                scoring_source_sha256=sha256(ROOT / "scripts/evaluate_candidates.py"),
                candidate_test_filter="upstream_test_filter_v1",
                upstream_filter_source_sha256=sha256(projection.SOURCE),
                evaluated_patch_sha256=projection.hashlib.sha256(effective).hexdigest(),
                candidate_tests_filtered=(effective != (run / f"{stage}.patch").read_bytes()),
                original_candidate_unchanged=(
                    sha256(run / f"{stage}.patch") == summary[stage]["sha256"]
                ),
            )
            destination = run / record["attempt_path"]
            (destination / "evaluated.patch").write_bytes(effective)
            save_json(destination / "result.json", record)
            save_json(run / "evaluation" / f"{stage}-feature{feature}" / "result.json", record)
            update(
                feature_evaluations_completed=state["feature_evaluations_completed"] + 1,
                active_run=run.name,
                active_feature=feature,
                latest_outcome=record["outcome"],
            )
            return record

        evaluator.score_one = repaired_score
        for manifest_path, manifest in zip(manifest_paths, manifests, strict=True):
            paths, summaries = verify_batch(ROOT, manifest)
            for run, summary in zip(paths, summaries, strict=True):
                for feature in summary["inputs"]["task"]["features"]:
                    result = run / "evaluation" / f"p0-feature{feature}" / "result.json"
                    record = json.loads(result.read_text())
                    assert record["outcome"].startswith("evaluation_"), (
                        "Do not replace valid scores"
                    )
                    preserved = result.with_name("initial-result.json")
                    assert not preserved.exists()
                    shutil.copy2(result, preserved)
                    # Absence of the index requests a new attempt; all first-pass files
                    # and a complete copy in original-evaluations remain preserved.
                    result.unlink()
            sys.argv = [str(Path(__file__)), "--manifest", str(manifest_path)]
            evaluator.main()
            aggregate_path = manifest_path.with_suffix(".evaluation.json")
            aggregate = json.loads(aggregate_path.read_text())
            aggregate.update(
                evaluation_revision=REVISION,
                repair_artifacts=str(output.relative_to(ROOT)),
                sealed_candidates_unchanged=True,
            )
            save_json(aggregate_path, aggregate)
            check_unchanged()
        subprocess.run(
            [sys.executable, str(ROOT / ".local/summarize-dspy8-v6.py")], cwd=ROOT, check=True
        )
        report = json.loads((ROOT / "docs/dspy8-results.json").read_text())
        valid = all(r["evaluation"]["joint_success"] is not None for r in report["rows"])
        check_unchanged()
        update(
            status="finished" if valid else "blocked",
            phase="finished" if valid else "scoring_error",
            all_sealed_candidates_unchanged=True,
            active_run=None,
            active_feature=None,
            results_path="docs/dspy8-results.md",
            valid_feature_evaluations=sum(
                x["outcome"] in ("passed", "failed")
                for r in report["rows"]
                for x in r["evaluation"]["results"]
            ),
        )
    except BaseException as error:
        update(
            status="blocked",
            error=f"{type(error).__name__}: {error}",
            traceback=traceback.format_exc(),
        )
        raise
    finally:
        if client is not None:
            client.close()
        lock.close()


if __name__ == "__main__":
    main()
