"""Real synthetic acceptance through the production controller; not a benchmark run."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from common import ROOT, save_json
from intervention.controller import Run
from intervention.funding import FundingRequired
from intervention.protocol import CONDITIONS, PROTOCOL, canonical_hash, implementation_manifest
from intervention.readiness import acceptance_status


class RuntimeRun(Run):
    """Replace only task inputs and acceptance preflight, retaining real scheduling."""

    def __init__(self, condition, output):
        config = json.loads((ROOT / "configs/pilot.json").read_text())
        super().__init__("click", condition, config, batch_id="synthetic-" + PROTOCOL)
        # Run construction performs no Docker connection/model call. Remove its unused
        # empty run directory; all synthetic evidence belongs under artifacts/runtime.
        self.output.rmdir()
        self.output = output
        self.output.mkdir(parents=True)
        self.server.output = self.output
        self.report["purpose"] = "synthetic_runtime_acceptance"

    def preflight(self):
        isolation = acceptance_status(ROOT)["isolation"]
        if not isolation["passed"]:
            raise RuntimeError("Isolation acceptance must pass first")
        # This check creates runtime evidence; it cannot require its own prior pass.
        # The normal controller's per-turn subscription gates remain active.

    def load_inputs(self):
        wait = "First run the foreground command sleep 65. " if self.strategy == "periodic" else ""
        # The same paired task is used for Single/Static/Adaptive; Periodic includes a
        # synthetic duration fixture solely to cross the real 60-second boundary.
        requirements = {
            "feature1": wait + "Write exactly ONE into feature-one.txt. "
            "Then call report_event with issue_id=public-file, kind=contract_change, "
            "evidence naming feature-one.txt and its new public ONE format, attempts=[], resolved=false. "
            "After the receipt and any instructions, finish with an honest delivery report.",
            "feature7": "Write exactly TWO into feature-two.txt. Report status and finish with a delivery report.",
        }
        if self.strategy == "single":
            requirements["feature1"] = "Write exactly ONE into feature-one.txt. Report status."
        contract = (
            "Synthetic qualification fixture, not a benchmark feature. Use container_exec in /workspace/repo. "
            "Keep the two files independent. Only use supplied tools. "
            "Manager INITIAL_PLANNING: assign each complete feature to one Worker. "
            "Manager SUPERVISION: coordinate messages to both Workers asking them to acknowledge "
            "and write exactly REVIEWED into reviewed.txt using container_exec. "
            "Workers receiving those messages should implement that file change, report status, "
            "perform their normal integration verification, then finish; do not ask them to skip verification."
            " All cooperative Workers: after publishing your feature, call verify_integration with "
            "test_paths=[tests/test_context.py], read feature-one.txt in the returned integration "
            "snapshot using inspect_code, and honestly report whether that fixed pair includes both files. "
            "Manager: inspect actual baseline Python code before recording the initial plan."
        )
        return requirements, contract

    async def seal_candidate(self):
        await super().seal_candidate()
        checks = {}
        if self.strategy == "single":
            targets = {"feature1": "single", "feature7": "single"}
        else:
            targets = {a["feature"]: r for r, a in self.assignments.items()}
            checks["one_initial_plan"] = len(self.planning_turns) == 1
            checks["baseline_code_inspected"] = any(
                i["role"] == "manager"
                and i["phase"] == "planning"
                and i["request"]["operation"] == "read"
                for i in self.inspections
            )
            final = self.report["final_public_validation"]
            checks["integrated_public_validation"] = (
                final.get("origin") == "controller_after_development"
                and final["snapshot_pair"] == self.report["p0"]["snapshot_pair"]
                and final.get("tests") is not None
                and final["tests"]["exit_code"] == 0
            )
            verified_worker_pairs = {
                c["snapshot_pair"]
                for c in self.integration_checks
                if c.get("origin") == "worker_request"
                and c.get("tests") is not None
                and c["tests"]["exit_code"] == 0
            }
            checks["worker_requested_integration_validation"] = bool(verified_worker_pairs)
            checks["integrated_code_inspected"] = any(
                i["role"] in self.coding_roles
                and i.get("snapshot_pair") in verified_worker_pairs
                and i["request"]["operation"] == "read"
                for i in self.inspections
            )
        for feature, filename, expected in (
            ("feature1", "feature-one.txt", "ONE"),
            ("feature7", "feature-two.txt", "TWO"),
        ):
            result = await asyncio.to_thread(
                self.workspaces[targets[feature]].execute, "cat " + filename
            )
            checks[feature] = result["exit_code"] == 0 and result["output"].strip() == expected
        checks["report_receipts"] = bool(self.receipts) and all(
            r.get("replied_at") for r in self.receipts.values()
        )
        if self.strategy in {"periodic", "adaptive"}:
            checks["manager_check"] = bool(self.manager_turns)
            completed_pauses = [p for p in self.pauses if not p.get("skipped")]
            checks["joint_pause"] = (
                len(completed_pauses) == len(self.manager_turns) > 0
                and all(p.get("quiescent_at") and p.get("resumed_at") for p in completed_pauses)
                and all(p.get("resumed_at") for p in self.pauses)
            )
            checks["acknowledgements"] = bool(self.messages) and all(
                m.get("acknowledged_at") for m in self.messages.values()
            )
            for role in self.coding_roles:
                result = await asyncio.to_thread(self.workspaces[role].execute, "cat reviewed.txt")
                checks[role + "_behavior"] = (
                    result["exit_code"] == 0 and result["output"].strip() == "REVIEWED"
                )
        elif self.strategy == "static":
            checks["no_runtime_manager"] = not self.manager_turns and not self.pauses
        self.report["runtime_checks"] = checks
        if not all(checks.values()):
            raise RuntimeError(f"Runtime acceptance failed: {checks}")


async def main():
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/runtime" / stamp
    output.mkdir(parents=True)
    report = {
        "protocol_id": PROTOCOL,
        "purpose": "synthetic_controller_acceptance",
        "implementation_sha256": canonical_hash(implementation_manifest(ROOT)),
        "subscription_only_confirmed": None,
        "funding_admission": "live_funding_availability",
        "funding_note": "User authorizes existing Credits; no reserve threshold or automatic purchase",
        "passed": False,
        "status": "preflight",
        "model_inference_invoked": False,
        "experiment_containers_started": False,
        "coordination_protocol_verified": False,
        "runs": [],
    }
    started_runs = []
    try:
        if not acceptance_status(ROOT)["isolation"]["passed"]:
            raise RuntimeError("Isolation acceptance must pass first")
        # Each production Run performs a fresh funding availability check before task containers
        # or generation; unknown or exhausted funding stops subsequent turns.
        for condition in CONDITIONS:
            run = RuntimeRun(condition, output / condition)
            started_runs.append(run)
            report.update(
                status="running",
                model_inference_invoked=None,
                experiment_containers_started=None,
            )
            # Qualification-only liveness guard, never a research development limit.
            async with asyncio.timeout(600):
                path = await run.execute()
            report["runs"].append(str(path))
        report.update(passed=True, status="passed", coordination_protocol_verified=True)
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["status"] = (
            "blocked_before_inference"
            if isinstance(error, FundingRequired) and not started_runs
            else "failed"
        )
        raise
    finally:
        if any(run.server.started_turns for run in started_runs):
            report["model_inference_invoked"] = True
        if any(run.workspaces for run in started_runs):
            report["experiment_containers_started"] = True
        save_json(output / "summary.json", report)
        marker = {"path": str(output / "summary.json"), **report}
        save_json(ROOT / "artifacts/runtime/latest.json", marker)
        save_json(ROOT / f"artifacts/runtime/{PROTOCOL}.json", marker)
        print(json.dumps({"output": str(output), "passed": report["passed"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(main())
