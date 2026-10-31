"""Predeclared four-condition batches and validation before any hidden scoring."""

from __future__ import annotations

import json
from pathlib import Path

from intervention.merge import digest
from intervention.protocol import (
    CONDITIONS,
    EFFORT,
    MODEL,
    PROFILE_LAYOUT,
    PROTOCOL,
    SKILL_PATH,
    canonical_hash,
    evaluation_inputs,
    implementation_manifest,
    load_profile,
    validate_config,
    validate_plan,
)
from intervention.tools import ROLE_TOOLS

FORMAL_BLOCKS = (
    ("dirty_equals", 1, ("single", "static", "periodic", "adaptive")),
    ("click", 1, ("adaptive", "single", "static", "periodic")),
    ("click", 2, ("single", "static", "periodic", "adaptive")),
    ("dirty_equals", 2, ("static", "periodic", "adaptive", "single")),
    ("dirty_equals", 3, ("periodic", "adaptive", "single", "static")),
    ("click", 3, ("periodic", "adaptive", "single", "static")),
)


def plan(kind: str, task: str = "outlines") -> list[dict]:
    if task not in {
        "outlines",
        "datasets_3997",
        "chi_26",
        "dirty_equals",
        "click",
        "dspy_8563",
        "dspy_8394",
        "dspy_8587",
        "click_2068",
    }:
        raise ValueError("Unknown task for four-condition pilot")
    if kind == "formal" and task != "outlines":
        raise ValueError("Formal task order is fixed by FORMAL_BLOCKS")
    blocks = ((task, 1, CONDITIONS),) if kind == "pilot" else FORMAL_BLOCKS
    return [
        {"task": task, "repeat": repeat, "strategy": condition}
        for task, repeat, order in blocks
        for condition in order
    ]


def verify_sealed(path: Path) -> dict:
    summary = json.loads((path / "summary.json").read_text())
    if summary.get("status") != "sealed":
        raise ValueError(f"Run is not sealed: {path}")
    if summary.get("protocol_id") != PROTOCOL:
        raise ValueError("Legacy P0/P1 runs cannot enter a P0-only batch")
    if not summary.get("sessions_closed") or summary.get("phase") != "closed":
        raise ValueError("All sessions must be closed before scoring")
    seal = path / "seal.json"
    if not seal.is_file() or digest(seal.read_bytes()) != summary.get("seal_sha256"):
        raise ValueError("Seal was modified after sealing")
    if json.loads(seal.read_text()) != {k: v for k, v in summary.items() if k != "seal_sha256"}:
        raise ValueError("Summary was modified after sealing")
    validate_config(summary["config"])
    condition = summary["strategy"]
    expected_roles = {"single"} if condition == "single" else {"worker_a", "worker_b"}
    if condition != "single":
        expected_roles.add("manager")
    if condition not in CONDITIONS or set(summary["threads"]) != expected_roles:
        raise ValueError("Wrong actual roles")
    if set(summary["role_settings"]) != expected_roles:
        raise ValueError("Wrong role settings")
    for role, spec in summary["role_settings"].items():
        if (spec["model"], spec["reasoning_effort"]) != (MODEL, EFFORT):
            raise ValueError("Role model/effort drift")
        expected_tools = list(ROLE_TOOLS[role])
        if spec["tools"] != expected_tools:
            raise ValueError("Role tool permissions drift")
    if summary.get("candidate_stages") != ["p0"] or "p1" in summary:
        raise ValueError("Only P0 is permitted")
    evidence = summary.get("evidence_sha256", {})
    required = {
        "implementation.json",
        "effective-profile.json",
        "environment.json",
        "protocol.jsonl",
        "events.jsonl",
    }
    coding_roles = expected_roles - {"manager"}
    profile = json.loads((path / "effective-profile.json").read_text())
    if (
        profile.get("layout_version") not in {4, PROFILE_LAYOUT}
        or profile["condition"] != condition
        or profile["skill"] != (None if condition == "single" else SKILL_PATH)
    ):
        raise ValueError("Effective prompt source/layout mismatch")
    if condition != "single":
        required.add("initial-plan.json")
        required.add("final-public-validation.json")
        final_path = path / "final-public-validation.json"
        if not final_path.is_file() or json.loads(final_path.read_text()) != summary.get(
            "final_public_validation"
        ):
            raise ValueError("Missing or mismatched final public verification evidence")
        if (
            summary["final_public_validation"].get("snapshot_pair")
            != summary["p0"]["snapshot_pair"]
        ):
            raise ValueError("Final public verification refers to another snapshot pair")
        if not (path / "initial-plan.json").is_file():
            raise ValueError("Incomplete sealed initial plan")
        initial = json.loads((path / "initial-plan.json").read_text())
        validate_plan(
            initial, summary["inputs"]["requirements"], layout_version=profile["layout_version"]
        )
        inspections = summary.get("code_inspections", [])
        if not any(
            i.get("role") == "manager"
            and i.get("phase") == "planning"
            and i.get("request", {}).get("operation") == "read"
            and "content" in i.get("result", {})
            for i in inspections
        ) or not any(p.startswith("code-inspections/") for p in evidence):
            raise ValueError("Missing baseline code inspection evidence before initial planning")
        if (
            initial != summary.get("initial_plan")
            or len(summary.get("manager_planning_turns", [])) != 1
        ):
            raise ValueError("Missing or mismatched initial Manager planning")
        if condition == "static" and (
            summary.get("manager_checks")
            or summary.get("pauses")
            or summary.get("messages")
            or summary.get("coordination_decisions")
        ):
            raise ValueError("Static must not supervise during development")
    elif (
        summary.get("initial_plan")
        or summary.get("manager_planning_turns")
        or summary.get("pauses")
    ):
        raise ValueError("Single must not have a Manager phase")
    required.update(f"environment-{role}.json" for role in coding_roles)
    if condition == "single":
        required |= {"single.patch", "p0.patch"}
        if (
            summary["p0"].get("candidate_origin") != "single_snapshot"
            or summary["p0"]["merge_ok"] is not None
        ):
            raise ValueError("Single must use a direct snapshot; merge is not applicable")
    else:
        required |= {"worker_a.patch", "worker_b.patch", "environment-integration.json"}
        if summary["p0"].get("candidate_origin") != "strict_merge":
            raise ValueError("Cooperation must use strict integration")
        required |= (
            {"p0.patch"}
            if summary["p0"]["merge_ok"]
            else {"p0-conflicts.txt", "p0-recovery.tar", "p0-recovery.json"}
        )
    if not required.issubset(evidence):
        raise ValueError("Incomplete sealed evidence")
    for relative, expected in evidence.items():
        file = path / relative
        if file.resolve().is_relative_to(path.resolve()) is False:
            raise ValueError("Evidence path escapes the run")
        if not file.is_file() or digest(file.read_bytes()) != expected:
            raise ValueError(f"Evidence was modified after sealing: {relative}")
    if "p0.patch" in required:
        if digest((path / "p0.patch").read_bytes()) != summary["p0"]["sha256"]:
            raise ValueError("Candidate was modified after sealing")
    elif (path / "p0.patch").exists() or summary["p0"].get("sha256") is not None:
        raise ValueError("Conflicts must not have an executable candidate")
    for role, spec in summary["role_settings"].items():
        if (
            spec != profile["roles"][role]
            or digest(profile["prompts"][role].encode()) != spec["prompt_sha256"]
        ):
            raise ValueError("Effective role prompt/hash mismatch")
    if condition == "single":
        if digest((path / "single.patch").read_bytes()) != summary["p0"]["sha256"]:
            raise ValueError("Single candidate is not its own snapshot")
    else:
        for role, letter in (("worker_a", "a"), ("worker_b", "b")):
            if digest((path / f"{role}.patch").read_bytes()) != summary["p0"][f"{letter}_sha256"]:
                raise ValueError("Merged contribution hash mismatch")
    common = summary["common_contract"]
    if canonical_hash(common) != summary["common_fingerprint"]:
        raise ValueError("Common fingerprint mismatch")
    if (
        common["inputs"] != summary["inputs"]
        or common["config"] != summary["config"]
        or common["task"] != summary["inputs"]["task"]
        or summary["inputs"]["task"]["name"] != summary["task"]
    ):
        raise ValueError("Inputs/config mismatch")
    if (
        canonical_hash(json.loads((path / "implementation.json").read_text()))
        != common["implementation_sha256"]
    ):
        raise ValueError("Implementation hash mismatch")
    environment = json.loads((path / "environment.json").read_text())
    if environment["image"] != common["image"]:
        raise ValueError("Evaluation image drift")
    return summary


def verify_batch(root: Path, manifest: dict) -> tuple[list[Path], list[dict]]:
    if manifest.get("protocol_id") != PROTOCOL or manifest.get("kind") not in {"pilot", "formal"}:
        raise ValueError("Not a four-condition P0 batch")
    kind = manifest["kind"]
    expected = plan(kind, manifest.get("task", "outlines"))
    runs = [(root / p).resolve() for p in manifest["runs"]]
    if (
        len(runs) != len(expected)
        or len(set(runs)) != len(runs)
        or manifest.get("expected_runs") != len(expected)
    ):
        raise ValueError("Batch is incomplete or contains duplicates")
    if manifest.get("plan") != expected:
        raise ValueError("Batch must follow the predeclared condition/repeat order")
    if kind == "formal" and not manifest.get("preregistered"):
        raise ValueError("Formal batch must be preregistered")
    # All verifications happen before obtaining a Docker client or reading test results.
    summaries = [verify_sealed(path) for path in runs]
    fingerprints = {}
    implementation = set()
    for sample, summary, run in zip(expected, summaries, runs, strict=True):
        if any(summary[k] != v for k, v in sample.items()):
            raise ValueError("Wrong task/repeat/condition or run ordering")
        if summary["kind"] != kind or summary["batch_id"] != manifest["batch_id"]:
            raise ValueError("Mixed kind/batch")
        if kind == "formal" and not summary["config"]["formal_protocol_frozen"]:
            raise ValueError("Formal protocol not frozen")
        task = summary["task"]
        profile = json.loads((run / "effective-profile.json").read_text())
        if profile != load_profile(root, summary["strategy"], summary["config"]):
            raise ValueError("Sealed role inputs no longer match the effective Skill/config")
        if (
            evaluation_inputs(root, summary["inputs"]["task"])
            != summary["common_contract"]["evaluation_inputs"]
        ):
            raise ValueError("Runner, hidden tests or dependency pins changed after development")
        fingerprints.setdefault(task, set()).add(summary["common_fingerprint"])
        implementation.add(summary["common_contract"]["implementation_sha256"])
    if any(len(v) != 1 for v in fingerprints.values()) or len(implementation) != 1:
        raise ValueError("Common conditions/implementation changed within batch")
    if implementation != {canonical_hash(implementation_manifest(root))}:
        raise ValueError("Scoring implementation/config no longer matches the sealed batch")
    threads = [thread for s in summaries for thread in s["threads"].values()]
    if len(set(threads)) != len(threads):
        raise ValueError("Sessions must be fresh across runs")
    return runs, summaries
