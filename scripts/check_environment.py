"""Check the installed Linux environment without model inference or agent work."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime

import docker
from common import ROOT, configuration, docker_client, image_locks, save_json, sha256


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-controls", action="store_true")
    args = parser.parse_args()
    config = configuration()
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": {},
        "model_inference_invoked": False,
    }
    checks = report["checks"]
    checks["platform"] = platform.system() == "Linux" and platform.machine() == "x86_64"
    checks["python"] = sys.version_info[:2] == (3, 12)
    report["python"] = platform.python_version()
    checks["git_root"] = command("git", "rev-parse", "--show-toplevel") == str(ROOT)
    report["branch"] = command("git", "branch", "--show-current")
    report["mount"] = command("findmnt", "-no", "FSTYPE,TARGET", "-T", str(ROOT))
    report["dependencies"] = {
        name: importlib.metadata.version(name)
        for name in ("docker", "pyyaml", "rich", "pytest", "ruff")
    }
    checks["python_dependencies"] = (
        subprocess.run(
            ["uv", "pip", "check", "--python", sys.executable],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )
    source = ROOT / config["upstream"]["source_directory"]
    marker = source / ".source-revision"
    checks["upstream_revision"] = (
        marker.exists() and marker.read_text().strip() == config["upstream"]["revision"]
    )
    codex = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_codex.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    checks["codex_login_and_models"] = codex.returncode == 0
    if codex.stdout.strip():
        report["codex"] = json.loads(codex.stdout)
        checks["target_model_and_effort"] = report["codex"]["target_model_available"]
        checks["codex_version"] = (
            report["codex"]["version"] == f"codex-cli {config['runtime']['codex_expected']}"
        )
    client = docker_client(timeout=30)
    locks = image_locks()
    report["images"] = {}
    try:
        info = client.info()
        report["docker"] = {
            key: info[key]
            for key in (
                "ServerVersion",
                "Driver",
                "DockerRootDir",
                "SecurityOptions",
                "CgroupVersion",
            )
        }
        checks["docker_rootless"] = "name=rootless" in info["SecurityOptions"]
        checks["docker_version"] = info["ServerVersion"] == config["runtime"]["docker"]
        free = shutil.disk_usage(info["DockerRootDir"]).free
        report["docker_storage_free_gib"] = round(free / 1024**3, 2)
        checks["docker_disk_headroom"] = free >= 5 * 1024**3
        for name, lock in locks.items():
            try:
                image = client.images.get(lock["reference"])
            except docker.errors.ImageNotFound:
                checks[f"image_{name}"] = False
                continue
            checks[f"image_{name}"] = (
                lock["reference"] in image.attrs.get("RepoDigests", [])
                and image.attrs["Os"] == "linux"
                and image.attrs["Architecture"] == "amd64"
            )
            report["images"][name] = {"reference": lock["reference"], "image_id": image.id}
    finally:
        client.close()
    # Different tasks can be verified in separate invocations; retain their evidence.
    reports = sorted((ROOT / "artifacts/environment/controls").glob("*/summary.json"))
    report["controls"] = {}
    for task in config["tasks"]:
        name = task["name"]
        latest = None
        for path in reports:
            data = json.loads(path.read_text())
            if name in data["requested_tasks"]:
                latest = (path, data)
        valid = False
        if latest:
            path, data = latest
            records = [record for record in data["results"] if record["task"] == name]
            task_dir = source / "dataset" / task["repository"] / f"task{task['task_id']}"
            expected_cases = {
                (feature, kind)
                for feature in task["features"]
                for kind in ("baseline", "single_gold", "combined_gold")
            }
            valid = (
                {(record["feature"], record["control"]) for record in records} == expected_cases
                and len(records) == len(expected_cases)
                and data["upstream_revision"] == config["upstream"]["revision"]
            )
            for record in records:
                feature_dir = task_dir / f"feature{record['feature']}"
                patch = (
                    feature_dir / "feature.patch"
                    if record["control"] == "single_gold"
                    else task_dir / "combined.patch"
                )
                valid = valid and (
                    record["control_ok"]
                    and record["image_reference"] == locks[name]["reference"]
                    and record["base_commit"] == task["base_commit"]
                    and record["runner_sha256"] == sha256(task_dir / "runner.sh")
                    and record["tests_sha256"] == sha256(feature_dir / "tests.patch")
                    and (
                        record["control"] == "baseline"
                        or record["feature_patch_sha256"] == sha256(patch)
                    )
                )
            report["controls"][name] = {
                "summary": str(path),
                "verified": valid,
                "count": len(records),
            }
        else:
            report["controls"][name] = {"verified": False, "count": 0}
        if args.require_controls:
            checks[f"controls_{name}"] = valid
    report["ready"] = all(checks.values())
    save_json(ROOT / "artifacts/environment/status.json", report)
    for name, success in checks.items():
        print(f"{'PASS' if success else 'FAIL'} {name}")
    print(f"Report: {ROOT / 'artifacts/environment/status.json'}")
    if not report["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
