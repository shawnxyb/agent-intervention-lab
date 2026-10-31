"""Evaluate a fully sealed batch. No model calls and no feedback to experiment agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import requests
from common import ROOT, docker_client, evaluator_cache, save_json, sha256
from intervention.batch import verify_batch, verify_sealed
from validate_evaluators import archive, copy_text, runner_bytes, test_counts


def score_one(client, run: Path, summary: dict, stage: str, feature: int, timeout=1800) -> dict:
    task = summary["inputs"]["task"]
    task_dir = ROOT / "vendor/CooperBench/dataset" / task["repository"] / f"task{task['task_id']}"
    runner, tests = task_dir / "runner.sh", task_dir / f"feature{feature}/tests.patch"
    reference = json.loads((run / "environment.json").read_text())["image"]
    lock = {"digest": reference.split("@", 1)[1]}
    cache = evaluator_cache(task["name"], lock)
    patch_path = run / f"{stage}.patch"
    destination = run / "evaluation" / f"{stage}-feature{feature}"
    result_root = destination
    if destination.exists():
        destination = destination / f"attempt-{time.time_ns()}"
    destination.mkdir(parents=True, exist_ok=False)
    constraints_path = ROOT / f"docs/environment-dependencies/{task['name']}.freeze.txt"
    constraints = (
        "\n".join(
            line
            for line in constraints_path.read_text().splitlines()
            if "==" in line and not line.startswith("#")
        )
        + "\n"
    )
    payload = {
        "environment/runner.sh": runner_bytes(task, runner),
        "environment/constraints.txt": constraints.encode(),
        "patches/tests.patch": tests.read_bytes(),
        "patches/candidate.patch": patch_path.read_bytes(),
    }
    patch_argument = " candidate.patch" if patch_path.stat().st_size else ""
    python = "python3" if task.get("language") == "go" else "python"
    payload["environment/entrypoint.sh"] = f'''set -u
cd /workspace/repo || exit 70
test "$(git rev-parse HEAD)" = "{task["base_commit"]}" || exit 71
{python} -m pip freeze --all > /environment/pip.before.txt
timeout -k 15 {timeout} bash /environment/runner.sh tests.patch{patch_argument}
result=$?
{python} -m pip freeze --all > /environment/pip.after.txt
exit "$result"
'''.encode()
    container = client.containers.create(
        image=reference,
        entrypoint="/bin/bash",
        command=["/environment/entrypoint.sh"],
        working_dir="/workspace/repo",
        mem_limit="6g",
        nano_cpus=2_000_000_000,
        pids_limit=512,
        network_mode="bridge",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        labels={
            "academic-writing.project": "manager-intervention",
            "academic-writing.run": summary["run_id"],
            "academic-writing.role": "evaluator",
        },
        volumes={str(cache): {"bind": "/root/.cache/uv", "mode": "rw"}},
        environment={
            "UV_CACHE_DIR": "/root/.cache/uv",
            "UV_LINK_MODE": "copy",
            "UV_HTTP_TIMEOUT": "600",
            "UV_HTTP_RETRIES": "5",
            "PIP_CONSTRAINT": "/environment/constraints.txt",
            "UV_CONSTRAINT": "/environment/constraints.txt",
        },
    )
    start = time.monotonic()
    record = {
        "stage": stage,
        "feature": feature,
        "image": reference,
        "patch_sha256": sha256(patch_path),
        "runner_sha256": sha256(runner),
        "effective_runner_sha256": hashlib.sha256(payload["environment/runner.sh"]).hexdigest(),
        "tests_sha256": sha256(tests),
        "constraints": constraints,
        "hidden_feedback_to_agents": False,
        "protocol_id": summary["protocol_id"],
        "common_fingerprint": summary["common_fingerprint"],
        "attempt_path": str(destination.relative_to(run)),
    }
    try:
        container.put_archive("/", archive(payload))
        container.start()
        try:
            code = container.wait(timeout=timeout + 60)["StatusCode"]
        except requests.exceptions.RequestException:
            container.kill()
            code = 124
        raw = container.logs().decode(errors="replace")
        (destination / "runner.log").write_text(raw)
        counts = test_counts(raw, task.get("language", "python"))
        reached_tests = (
            "Running Go tests..." if task.get("language") == "go" else "Running tests..."
        ) in raw
        passed = code == 0 and counts["passed"] > 0 and counts["failed"] + counts["errors"] == 0
        outcome = "passed" if passed else "failed"
        if not reached_tests or code in (124, 137):
            outcome = "evaluation_infrastructure_error"
        record.update(
            exit_code=code,
            counts=counts,
            outcome=outcome,
            seconds=round(time.monotonic() - start, 2),
        )
        for name in ("pip.before.txt", "pip.after.txt"):
            copy_text(container, f"/environment/{name}", destination / name)
        actual = set((destination / "pip.after.txt").read_text().splitlines())
        required = set(constraints.splitlines())
        record["dependency_pins_match"] = required.issubset(actual)
        if not record["dependency_pins_match"]:
            record["outcome"] = "evaluation_dependency_drift"
        container.reload()
        if container.attrs["State"].get("OOMKilled"):
            record["outcome"] = "evaluation_infrastructure_error"
    finally:
        container.remove(force=True)
    save_json(destination / "result.json", record)
    if destination != result_root:
        save_json(result_root / "result.json", record)
    print(f"{summary['run_id']} {stage}/f{feature}: {record['outcome']} {counts}", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    runs, summaries = verify_batch(ROOT, manifest)
    # Verify every candidate before any hidden test is revealed.
    client = docker_client(timeout=1900)
    try:
        evaluations = []
        for run, summary in zip(runs, summaries, strict=True):
            verify_sealed(run)
            records = []
            for stage in ("p0",):
                if summary[stage]["merge_ok"] is False:
                    records.append(
                        {
                            "stage": stage,
                            "outcome": "merge_conflict",
                            "joint_success": False,
                            "feature_evaluation": "not_run",
                        }
                    )
                    continue
                for feature in summary["inputs"]["task"]["features"]:
                    saved = run / "evaluation" / f"{stage}-feature{feature}" / "result.json"
                    if saved.exists():
                        record = json.loads(saved.read_text())
                        if (
                            record["patch_sha256"] != summary[stage]["sha256"]
                            or record.get("common_fingerprint") != summary["common_fingerprint"]
                            or record.get("protocol_id") != summary["protocol_id"]
                            or record.get("feature") != feature
                            or record.get("stage") != stage
                        ):
                            raise ValueError(
                                "Existing evaluation has a different patch/protocol/context"
                            )
                    else:
                        record = score_one(client, run, summary, stage, feature)
                    records.append(record)
            verify_sealed(run)
            stages = {}
            for stage in ("p0",):
                results = [r for r in records if r["stage"] == stage]
                invalid = any(r["outcome"].startswith("evaluation_") for r in results)
                stages[stage] = {
                    "joint_success": None
                    if invalid
                    else all(r["outcome"] == "passed" for r in results),
                    "results": results,
                    "merge_ok": summary[stage]["merge_ok"],
                    "candidate_origin": summary[stage]["candidate_origin"],
                }
            evaluation = {"stages": stages, "batch": str(args.manifest)}
            save_json(run / "evaluation.json", evaluation)
            evaluations.append(
                {
                    "run_id": summary["run_id"],
                    "task": summary["task"],
                    "strategy": summary["strategy"],
                    "manager_planning_turns": len(summary.get("manager_planning_turns", [])),
                    "manager_checks": summary.get("manager_checks"),
                    "planning_seconds": summary.get("planning_seconds"),
                    "development_seconds": summary.get("development_seconds"),
                    "pause_total_seconds": summary.get("pause_total_seconds"),
                    "wall_seconds": summary.get("wall_seconds"),
                    "team_usage": summary.get("team_usage"),
                    "role_usage": summary.get("usage"),
                    **stages["p0"],
                }
            )
        # Include interrupted attempts with the same batch id when accounting for cost.
        attempts = []
        for path in sorted((ROOT / "runs").glob("*/summary.json")):
            record = json.loads(path.read_text())
            if (
                record.get("protocol_id") == manifest["protocol_id"]
                and record.get("batch_id") == manifest["batch_id"]
            ):
                attempts.append(
                    {
                        "run_id": record["run_id"],
                        "status": record["status"],
                        "strategy": record["strategy"],
                        "wall_seconds": record.get("wall_seconds"),
                        "team_usage": record.get("team_usage"),
                        "planning_seconds": record.get("planning_seconds"),
                        "manager_planning_turns": len(record.get("manager_planning_turns", [])),
                        "manager_checks": record.get("manager_checks"),
                    }
                )
        save_json(
            args.manifest.with_suffix(".evaluation.json"),
            {
                "protocol_id": manifest["protocol_id"],
                "batch_id": manifest["batch_id"],
                "feature_evaluation_units": sum(
                    len(r["results"]) for r in evaluations if r["merge_ok"] is not False
                ),
                "samples": evaluations,
                "attempt_costs_including_interruptions": attempts,
            },
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
