"""Check current experiment readiness without starting model turns or task containers."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime

from common import ROOT, docker_client, save_json
from intervention.protocol import PROTOCOL, canonical_hash, implementation_manifest, validate_config
from intervention.readiness import acceptance_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["pilot", "formal"], default="pilot")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs/pilot.json").read_text())
    validate_config(config)
    fingerprint = canonical_hash(implementation_manifest(ROOT))
    # A failed refresh cannot reuse an earlier successful environment status.
    refresh_started = datetime.now(UTC).isoformat()
    refreshed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_environment.py"), "--require-controls"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    status_path = ROOT / "artifacts/environment/status.json"
    environment = json.loads(status_path.read_text()) if status_path.is_file() else {}
    if environment.get("checked_at", "") < refresh_started:
        environment = {}
    codex = environment.get("codex", {})
    offline = json.loads((ROOT / "docs/controller-validation.json").read_text())
    checks = {
        "offline_validation": {
            "passed": offline.get("protocol_id") == PROTOCOL
            and offline.get("implementation_sha256") == fingerprint
            and offline.get("pytest", {}).get("passed", 0) > 0
            and offline["pytest"].get("failures") == offline["pytest"].get("errors") == 0,
        },
        "environment": {"passed": refreshed.returncode == 0 and environment.get("ready") is True},
        "model_and_effort": {"passed": codex.get("target_model_available") is True},
        "funding_availability": {
            "passed": codex.get("experiment_start_quota", {}).get("passed") is True
        },
        "experiment_start_quota": codex.get(
            "experiment_start_quota", {"passed": False, "reason": "Start reserve unverified"}
        ),
        **acceptance_status(ROOT),
    }
    try:
        client = docker_client(timeout=15)
        try:
            running = client.containers.list(
                filters={"label": "academic-writing.project=manager-intervention"}
            )
            checks["no_active_experiment_containers"] = {
                "passed": not running,
                "count": len(running),
            }
        finally:
            client.close()
    except Exception as error:  # noqa: BLE001 - diagnostic boundary, never permission to run
        checks["no_active_experiment_containers"] = {"passed": False, "reason": str(error)}
    if args.kind == "formal":
        checks["formal_protocol_frozen"] = {"passed": config["formal_protocol_frozen"] is True}
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "protocol_id": PROTOCOL,
        "kind": args.kind,
        "formal_protocol_frozen": config["formal_protocol_frozen"],
        "implementation_sha256": fingerprint,
        "checks": checks,
        "ready": all(c["passed"] for c in checks.values()),
        "blockers": [name for name, c in checks.items() if not c["passed"]],
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "quota_used_percent": codex.get("quota_used_percent"),
        "environment_refresh_exit_code": refreshed.returncode,
        "model_inference_invoked": False,
        "experiment_containers_started": False,
        "hidden_evaluation_invoked": False,
    }
    save_json(ROOT / "docs/start-readiness.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
