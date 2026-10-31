"""Check baseline failure and official gold success; never run an experiment agent.

Hidden patches are copied programmatically into disposable containers. Their contents
and test failure details are not printed. Full logs remain in ignored local artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import docker
import requests
from common import (
    ROOT,
    configuration,
    docker_client,
    evaluator_cache,
    image_locks,
    save_json,
    sha256,
)


def test_counts(output: str, language: str = "python") -> dict[str, int]:
    if language == "go":
        return {
            "passed": len(re.findall(r"^--- PASS: ", output, re.MULTILINE)),
            "failed": len(re.findall(r"^--- FAIL: ", output, re.MULTILINE)),
            "errors": int("[build failed]" in output or "[setup failed]" in output),
            "skipped": len(re.findall(r"^--- SKIP: ", output, re.MULTILINE)),
        }
    # Parse only the final pytest summary, never incidental install or test text.
    summaries = re.findall(r"^=+ (.+?) =+\s*$", output, re.MULTILINE)
    for summary in reversed(summaries):
        if "no tests ran" in summary:
            return {"passed": 0, "failed": 0, "errors": 0}
        counts = {
            kind: int(value)
            for value, kind in re.findall(
                r"\b(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b",
                summary,
            )
        }
        if counts:
            counts["errors"] = counts.pop("error", 0) + counts.get("errors", 0)
            return {"passed": 0, "failed": 0, "errors": 0, **counts}
    return {"passed": 0, "failed": 0, "errors": 0}


def runner_bytes(task: dict, path: Path) -> bytes:
    """Keep failing Go test output visible despite the upstream runner's set -e."""
    source = path.read_bytes()
    if task.get("language") == "go":
        source = source.replace(b"    TEST_OUTPUT=", b"    set +e\n    TEST_OUTPUT=")
        source = source.replace(b"    TEST_EXIT_CODE=$?\n", b"    TEST_EXIT_CODE=$?\n    set -e\n")
    return source


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o644
            tar.addfile(item, io.BytesIO(data))
    return buffer.getvalue()


def copy_text(container, source: str, target: Path) -> None:
    chunks, _ = container.get_archive(source)
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks)), mode="r:*") as tar:
        item = tar.extractfile(tar.getmembers()[0])
        if item is None:
            raise RuntimeError(f"Missing container evidence: {source}")
        target.write_bytes(item.read())


def validate_one(
    client, task: dict, lock: dict, feature: int, kind: str, output: Path, timeout: int
) -> dict:
    task_dir = ROOT / "vendor/CooperBench/dataset" / task["repository"] / f"task{task['task_id']}"
    runner = task_dir / "runner.sh"
    tests = task_dir / f"feature{feature}/tests.patch"
    patch = None
    if kind == "single_gold":
        patch = task_dir / f"feature{feature}/feature.patch"
    elif kind == "combined_gold":
        patch = task_dir / "combined.patch"
    case_id = f"{task['name']}-f{feature}-{kind}"
    case_dir = output / case_id
    case_dir.mkdir()
    payload = {
        "environment/runner.sh": runner_bytes(task, runner),
        "patches/tests.patch": tests.read_bytes(),
    }
    patch_argument = ""
    if patch is not None:
        payload["patches/feature.patch"] = patch.read_bytes()
        patch_argument = " feature.patch"
    # Python runners stay unchanged; Go normalization only preserves failure output.
    # The cleanups in runner.sh execute exclusively inside this disposable container.
    python = "python3" if task.get("language") == "go" else "python"
    entrypoint = f'''set -u
cd /workspace/repo || exit 70
git rev-parse HEAD > /environment/base-commit.txt
test "$(cat /environment/base-commit.txt)" = "{task["base_commit"]}" || exit 71
{python} -m pip freeze --all > /environment/pip.before.txt
timeout -k 15 {timeout} bash /environment/runner.sh tests.patch{patch_argument}
result=$?
{python} -m pip freeze --all > /environment/pip.after.txt
exit "$result"
'''
    payload["environment/entrypoint.sh"] = entrypoint.encode()
    image = client.images.get(lock["reference"])
    cache = evaluator_cache(task["name"], lock)
    cache.mkdir(parents=True, exist_ok=True)
    container = client.containers.create(
        image=lock["reference"],
        entrypoint="/bin/bash",
        command=["/environment/entrypoint.sh"],
        working_dir="/workspace/repo",
        labels={
            "academic-writing.project": "manager-intervention",
            "academic-writing.check": case_id,
        },
        mem_limit="6g",
        nano_cpus=2_000_000_000,
        pids_limit=512,
        network_mode="bridge",
        # Only a task-specific package cache is shared; code and patches are not.
        volumes={str(cache): {"bind": "/root/.cache/uv", "mode": "rw"}},
        environment={
            "UV_CACHE_DIR": "/root/.cache/uv",
            "UV_LINK_MODE": "copy",
            "UV_HTTP_TIMEOUT": "600",
            "UV_HTTP_RETRIES": "5",
        },
    )
    record = {
        "case": case_id,
        "task": task["name"],
        "feature": feature,
        "control": kind,
        "expected": "test_failure" if kind == "baseline" else "test_success",
        "image_reference": lock["reference"],
        "image_id": image.id,
        "base_commit": task["base_commit"],
        "runner_sha256": sha256(runner),
        "effective_runner_sha256": hashlib.sha256(payload["environment/runner.sh"]).hexdigest(),
        "tests_sha256": sha256(tests),
        "feature_patch_sha256": sha256(patch) if patch else None,
        "network_enabled_for_upstream_dependency_install": True,
        "dependency_cache": str(cache.relative_to(ROOT)),
        "uv_http_timeout_seconds": 600,
        "uv_http_retries": 5,
        "timeout_seconds": timeout,
    }
    start = time.monotonic()
    try:
        container.put_archive("/", archive(payload))
        container.start()
        try:
            status = container.wait(timeout=timeout + 60)
            code = status["StatusCode"]
        except requests.exceptions.RequestException:
            container.kill()
            code = 124
        raw = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        (case_dir / "runner.log").write_text(raw)
        counts = test_counts(raw, task.get("language", "python"))
        passed = (
            code == 0 and counts["passed"] > 0 and counts["errors"] == 0 and counts["failed"] == 0
        )
        # A new public symbol may be absent at baseline, causing a collection error.
        # Accept that only when both corresponding gold controls later pass.
        expected_failure = (
            code in (1, 2)
            and counts["failed"] + counts["errors"] > 0
            and ("Running Go tests..." if task.get("language") == "go" else "Running tests...")
            in raw
        )
        record.update(
            exit_code=code,
            counts=counts,
            evaluator_passed=passed,
            control_ok=expected_failure if kind == "baseline" else passed,
            duration_seconds=round(time.monotonic() - start, 2),
        )
        for filename in ("base-commit.txt", "pip.before.txt", "pip.after.txt"):
            try:
                copy_text(container, f"/environment/{filename}", case_dir / filename)
            except (docker.errors.APIError, tarfile.TarError, OSError, RuntimeError) as error:
                record.setdefault("evidence_errors", []).append(
                    f"{filename}: {type(error).__name__}"
                )
                record["control_ok"] = False
        container.reload()
        record["oom_killed"] = container.attrs["State"].get("OOMKilled", False)
        if record["oom_killed"]:
            record["control_ok"] = False
    finally:
        container.remove(force=True)
    save_json(case_dir / "result.json", record)
    print(
        f"{case_id}: {'OK' if record['control_ok'] else 'CHECK_FAILED'} "
        f"(exit={record['exit_code']}, counts={record['counts']}, {record['duration_seconds']}s)",
        flush=True,
    )
    return record


def main() -> None:
    config = configuration()
    tasks = {task["name"]: task for task in config["tasks"]}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", choices=tasks, default=list(tasks))
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/environment/controls" / timestamp
    output.mkdir(parents=True)
    report = {
        "checked_at": timestamp,
        "purpose": "environment_controls_only",
        "model_inference_invoked": False,
        "upstream_revision": config["upstream"]["revision"],
        "requested_tasks": args.tasks,
        "results": [],
        "all_controls_ok": False,
    }
    locks = image_locks()
    client = docker_client(timeout=args.timeout + 90)
    try:
        for name in args.tasks:
            for feature in tasks[name]["features"]:
                for kind in ("baseline", "single_gold", "combined_gold"):
                    record = validate_one(
                        client, tasks[name], locks[name], feature, kind, output, args.timeout
                    )
                    report["results"].append(record)
                    save_json(output / "summary.json", report)
                group = report["results"][-3:]
                if not all(record["control_ok"] for record in group[1:]):
                    group[0]["control_ok"] = False
                    group[0]["reason"] = "negative_control_not_validated_by_both_gold_controls"
                    save_json(output / group[0]["case"] / "result.json", group[0])
        report["all_controls_ok"] = all(record["control_ok"] for record in report["results"])
        save_json(output / "summary.json", report)
        save_json(
            ROOT / "artifacts/environment/controls.latest.json",
            {"path": str(output / "summary.json")},
        )
        print(
            json.dumps(
                {
                    "summary": str(output / "summary.json"),
                    "all_controls_ok": report["all_controls_ok"],
                }
            )
        )
    finally:
        client.close()
    if not report["all_controls_ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
