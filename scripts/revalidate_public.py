"""Recheck sealed Outlines candidates in repaired environments, without agents or hidden tests."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, docker_client, save_json
from intervention.merge import digest
from intervention.protocol import canonical_hash, implementation_manifest
from intervention.sandbox import ContainerWorkspace


def verify_archived(path: Path) -> dict:
    """Verify historical bytes using their own seal, not today's funding/prompt contract."""
    summary = json.loads((path / "summary.json").read_text())
    seal = (path / "seal.json").read_bytes()
    if summary.get("status") != "sealed" or not summary.get("sessions_closed"):
        raise ValueError("Only sealed candidates with closed sessions can be revalidated")
    if digest(seal) != summary.get("seal_sha256") or json.loads(seal) != {
        k: v for k, v in summary.items() if k != "seal_sha256"
    }:
        raise ValueError("Archived seal or summary changed")
    for relative, expected in summary["evidence_sha256"].items():
        file = path / relative
        if (
            not file.resolve().is_relative_to(path.resolve())
            or digest(file.read_bytes()) != expected
        ):
            raise ValueError("Archived evidence changed: " + relative)
    if (
        summary["p0"].get("sha256")
        and digest((path / "p0.patch").read_bytes()) != summary["p0"]["sha256"]
    ):
        raise ValueError("Archived candidate changed")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    paths = [(ROOT / p).resolve() for p in args.runs]
    if any(not p.is_relative_to(ROOT) for p in paths) or len(paths) != len(set(paths)):
        raise ValueError("Use distinct project-local archived run paths")
    summaries = [verify_archived(p) for p in paths]
    if any(s["task"] != "outlines" for s in summaries):
        raise ValueError("This repair revalidation targets Outlines public tests only")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/public-revalidation" / stamp
    output.mkdir(parents=True)
    report = {
        "purpose": "posthoc_environment_repair_public_revalidation",
        "implementation_sha256": canonical_hash(implementation_manifest(ROOT)),
        "model_inference_invoked": False,
        "hidden_evaluation_invoked": False,
        "formal_comparison": False,
        "original_runs_modified": False,
        "status": "running",
        "rows": [],
    }
    client = docker_client(timeout=None)
    try:
        for index, (path, summary) in enumerate(zip(paths, summaries, strict=True)):
            row = {
                "run": str(path.relative_to(ROOT)),
                "strategy": summary["strategy"],
                "original_seal_sha256": summary["seal_sha256"],
                "original_candidate_sha256": summary["p0"].get("sha256"),
            }
            report["rows"].append(row)
            if not summary["p0"].get("sha256"):
                row["status"] = "no_executable_candidate"
                continue
            workspace = ContainerWorkspace(
                client, summary["common_contract"]["image"], stamp, f"public-recheck-{index}"
            )
            try:
                row["environment"] = workspace.prepare(summary["inputs"]["task"]["base_commit"])
                workspace.put_files(
                    {"tmp/public-candidate.patch": (path / "p0.patch").read_bytes()}
                )
                workspace.checked("git apply --index /tmp/public-candidate.patch")
                before = workspace.snapshot()
                command = "python -m pytest -q -- tests/types"
                row["tests"] = {"command": command, **workspace.execute(command, limit=100000)}
                row["candidate_unchanged_by_tests"] = before == workspace.snapshot()
                if not row["candidate_unchanged_by_tests"]:
                    raise RuntimeError("Public tests modified candidate source")
                verify_archived(path)
                row["status"] = (
                    "public_tests_passed"
                    if row["tests"]["exit_code"] == 0
                    else "public_tests_failed"
                )
            except Exception as error:  # noqa: BLE001 - retain each independent diagnostic failure
                row.update(status="revalidation_error", error=f"{type(error).__name__}: {error}")
            finally:
                workspace.close()
            save_json(output / "summary.json", report)
            print(row["strategy"], row["status"], flush=True)
        for path in paths:
            verify_archived(path)
        report["status"] = "finished"
    finally:
        client.close()
        save_json(output / "summary.json", report)
        save_json(
            ROOT / "docs/public-revalidation-status.json",
            {"path": str(output.relative_to(ROOT)), **report},
        )
        print(json.dumps({"output": str(output), "status": report["status"]}), flush=True)


if __name__ == "__main__":
    main()
