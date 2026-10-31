"""Frozen role contract and explicit project Skill loading (no model calls)."""

from __future__ import annotations

import json
from pathlib import Path

from intervention.funding import MINIMUM_START_REMAINING
from intervention.merge import digest
from intervention.tools import ROLE_TOOLS

PROTOCOL = "p0-fourconditions-high-v6"
CONDITIONS = ("single", "static", "periodic", "adaptive")
MODEL, EFFORT = "gpt-6.1-sol", "high"
SKILL_PATH = ".agents/skills/cooperbench-collaboration"
PROFILE_LAYOUT = 5
LIMITS = dict.fromkeys(
    ("worker_tool_calls", "development_seconds", "model_turn_seconds", "token_count")
)
FUNDING = {
    "source": "subscription_or_credits",
    "admission": "live_funding_availability",
    "allow_existing_credits": True,
    "on_unknown": "interrupt",
    "on_exhaustion": "interrupt",
    "minimum_remaining_percent_to_start": MINIMUM_START_REMAINING,
}


def canonical_hash(value) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def validate_plan(plan: dict, requirements: dict, *, layout_version: int = PROFILE_LAYOUT) -> dict:
    """Check plan structure and identifiers, never semantic quality or completeness."""
    if layout_version not in {4, PROFILE_LAYOUT}:
        raise ValueError("Unsupported plan layout")
    if not isinstance(plan, dict) or not isinstance(plan.get("assignments"), list):
        raise TypeError("Initial plan must contain an assignments list")
    from intervention.evidence import public_path

    regions = plan.get("shared_regions")
    if not isinstance(regions, list):
        raise ValueError("Plan must explicitly declare shared_regions (empty if none)")  # noqa: TRY004
    locations = set()
    for region in regions:
        if (
            not isinstance(region, dict)
            or set(region) != {"path", "symbol", "owner", "action", "verification"}
            or any(not isinstance(v, str) or not v.strip() for v in region.values())
            or region["owner"] not in {"worker_a", "worker_b"}
        ):
            raise ValueError("Shared regions need one owner, symbol, action and verification")
        public_path(region["path"])
        location = (region["path"], region["symbol"])
        if location in locations:
            raise ValueError(
                f"A shared region must have a single owner: {location[0]} :: {location[1]}. "
                "Use distinct concrete symbols or insertion blocks for disjoint edits in one file"
            )
        locations.add(location)
    assignments = plan.get("assignments", [])
    if (
        len(assignments) != 2
        or any(
            not isinstance(a, dict)
            or not isinstance(a.get("target"), str)
            or not isinstance(a.get("feature"), str)
            for a in assignments
        )
        or {a.get("target") for a in assignments} != {"worker_a", "worker_b"}
        or {a.get("feature") for a in assignments} != set(requirements)
        or not isinstance(plan.get("reason"), str)
        or not plan["reason"].strip()
        or any(
            set(a) != {"target", "feature", "instructions"}
            or not isinstance(a["instructions"], str)
            or not a["instructions"].strip()
            for a in assignments
        )
    ):
        raise ValueError("Initial plan must assign each complete feature to a different Worker")
    # Archived layout 4 plans remain readable; live planning always uses the current layout.
    contracts = plan.get("interface_contracts", [] if layout_version == 4 else None)
    if not isinstance(contracts, list):
        raise TypeError("Plan must explicitly declare interface_contracts (empty if none)")
    text_fields = {
        "path",
        "symbol",
        "inputs",
        "outputs",
        "state",
        "sequence",
        "errors",
        "verification",
    }
    interfaces = set()
    for contract in contracts:
        if (
            not isinstance(contract, dict)
            or set(contract) != text_fields | {"owner", "consumers"}
            or any(not isinstance(contract[k], str) or not contract[k].strip() for k in text_fields)
            or not isinstance(contract["owner"], str)
            or contract["owner"] not in {"worker_a", "worker_b"}
            or not isinstance(contract["consumers"], list)
            or not contract["consumers"]
            or any(
                not isinstance(r, str) or r not in {"worker_a", "worker_b"}
                for r in contract["consumers"]
            )
        ):
            raise ValueError(
                "Interface contracts need nonempty text fields, one owner and consumers"
            )
        if len(set(contract["consumers"])) != len(contract["consumers"]):
            raise ValueError("Interface consumers must be unique Worker identifiers")
        location = (public_path(contract["path"]), contract["symbol"])
        if location in interfaces:
            raise ValueError("Declare each interface contract once")
        if any(
            (public_path(r["path"]), r["symbol"]) == location and r["owner"] != contract["owner"]
            for r in regions
        ):
            raise ValueError("Shared region and interface owner identifiers must agree")
        interfaces.add(location)
    expected = {"reason", "assignments", "shared_regions"}
    if layout_version == PROFILE_LAYOUT or "interface_contracts" in plan:
        expected.add("interface_contracts")
    if set(plan) != expected:
        raise ValueError("Unexpected initial plan fields")
    return {a["target"]: a for a in assignments}


def validate_config(config: dict) -> None:
    expected = {
        "protocol_id",
        "model",
        "reasoning_effort",
        "execution_limits",
        "funding",
        "formal_protocol_frozen",
    }
    if set(config) != expected or config.get("protocol_id") != PROTOCOL:
        raise ValueError("Use the explicit P0 four-condition config; legacy/extra options rejected")
    if (config["model"], config["reasoning_effort"]) != (MODEL, EFFORT):
        raise ValueError("All actual roles must use gpt-6.1-sol / high")
    if config["execution_limits"] != LIMITS:
        raise ValueError("Research caps must be null")
    if config["funding"] != FUNDING:
        raise ValueError("Funding policy must match the current subscription-or-Credits contract")
    if not isinstance(config["formal_protocol_frozen"], bool):
        raise TypeError("formal_protocol_frozen must be a JSON boolean")


def load_profile(root: Path, condition: str, config: dict | None = None) -> dict:
    """Build only role-visible inputs; record exact source and composed prompt hashes."""
    if condition not in CONDITIONS:
        raise ValueError(condition)
    config = json.loads((root / "configs/pilot.json").read_text()) if config is None else config
    validate_config(config)
    profile = {
        "layout_version": PROFILE_LAYOUT,
        "condition": condition,
        "skill": None,
        "policy": None,
        "prompts": {},
        "roles": {},
        "files": {},
    }
    contents = {}

    def read_source(relative: str) -> str:
        if relative not in contents:
            content = (root / relative).read_bytes()
            profile["files"][relative] = digest(content)
            contents[relative] = content.decode()
        return contents[relative]

    def add_role(role: str, paths: list[str], enabled: bool = True) -> None:
        parts = [read_source(relative) for relative in paths]
        # Single is a plain prompt, preserved byte-for-byte. Cooperative fragments
        # have a fixed separator independent of the selected supervision strategy.
        prompt = parts[0] if role == "single" else "\n\n".join(p.strip() for p in parts) + "\n"
        profile["prompts"][role] = prompt
        profile["roles"][role] = {
            "model": config["model"],
            "reasoning_effort": config["reasoning_effort"],
            "prompt_sources": paths,
            "prompt_sha256": digest(prompt.encode()),
            "tools": list(ROLE_TOOLS[role]),
            "inference_enabled": enabled,
        }

    if condition == "single":
        add_role("single", ["prompts/single.md"])
        return profile

    policies = json.loads(read_source(f"{SKILL_PATH}/policies.json"))
    # These checks freeze the approved experiment; the JSON supplies the actual settings.
    expected = {
        "static": {"trigger": "none"},
        "periodic": {
            "trigger": "fixed_interval",
            "interval_seconds": 60,
            "busy_behavior": "skip_tick",
            "pause_workers": True,
        },
        "adaptive": {
            "trigger": "qualified_event",
            "busy_behavior": "coalesce_then_revalidate",
            "pause_workers": True,
        },
    }
    if policies != expected:
        raise ValueError("Skill supervision drift or unsupported policy fields")
    profile.update(skill=SKILL_PATH, policy=policies[condition])
    read_source(f"{SKILL_PATH}/SKILL.md")
    for role in ("worker_a", "worker_b"):
        add_role(role, [f"{SKILL_PATH}/roles/worker.md", f"{SKILL_PATH}/references/events.md"])
    add_role("manager", [f"{SKILL_PATH}/roles/manager.md"])
    return profile


def implementation_manifest(root: Path) -> dict:
    paths = set((root / "scripts").rglob("*.py"))
    for directory in ("configs", ".agents/skills", "prompts"):
        paths.update(p for p in (root / directory).rglob("*") if p.is_file())
    paths.update((root / "docs/environment-dependencies").glob("*.freeze.txt"))
    paths.update(p for p in (root / "pyproject.toml", root / "uv.lock") if p.exists())
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(paths)}


def evaluation_inputs(root: Path, task: dict) -> dict:
    directory = root / "vendor/CooperBench/dataset" / task["repository"] / f"task{task['task_id']}"
    paths = [
        directory / "runner.sh",
        root / f"docs/environment-dependencies/{task['name']}.freeze.txt",
    ]
    paths.extend(directory / f"feature{feature}/tests.patch" for feature in task["features"])
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in paths}


def team_usage(usage: dict) -> dict:
    # Each notice is already a cumulative total. Cached/reasoning tokens are subitems.
    fields = (
        "totalTokens",
        "inputTokens",
        "cachedInputTokens",
        "outputTokens",
        "reasoningOutputTokens",
    )
    return {
        field: sum(u[field] for u in usage.values())
        if usage
        and all(
            isinstance(u, dict)
            and isinstance(u.get(field), int)
            and not isinstance(u.get(field), bool)
            and u[field] >= 0
            for u in usage.values()
        )
        else None
        for field in fields
    }
