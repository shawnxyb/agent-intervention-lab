import json
import shutil
from unittest.mock import AsyncMock

import pytest
from common import ROOT
from intervention.merge import digest
from intervention.protocol import (
    SKILL_PATH,
    load_profile,
    team_usage,
    validate_config,
    validate_plan,
)
from intervention.sandbox import ContainerWorkspace
from intervention.tools import EXEC, PLAN, planning_tool


def test_planning_schema_exposes_exact_ids_without_assigning_workers():
    definition = planning_tool({"feature7": "Second", "feature1": "First"})
    assignments = definition["inputSchema"]["properties"]["assignments"]
    assert assignments["items"]["properties"]["feature"]["enum"] == ["feature1", "feature7"]
    assert assignments["items"]["properties"]["target"]["enum"] == ["worker_a", "worker_b"]
    assert assignments["minItems"] == assignments["maxItems"] == 2
    assert (
        "enum"
        not in PLAN["inputSchema"]["properties"]["assignments"]["items"]["properties"]["feature"]
    )


@pytest.fixture
def interface_plan():
    return {
        "reason": "Declared responsibility boundary",
        "assignments": [
            {"target": "worker_a", "feature": "feature1", "instructions": "implement"},
            {"target": "worker_b", "feature": "feature2", "instructions": "connect and verify"},
        ],
        "shared_regions": [],
        "interface_contracts": [
            {
                "path": "one.py",
                "symbol": "public_value",
                "owner": "worker_a",
                "consumers": ["worker_b"],
                "inputs": "declared input",
                "outputs": "declared output",
                "state": "Agent must review state ownership",
                "sequence": "A implements, B connects",
                "errors": "Agent must review fallback semantics",
                "verification": "B exercises the public entry point",
            }
        ],
    }


def test_controller_records_contract_text_without_semantic_approval(interface_plan):
    # Contradictory prose still passes structural validation; only Agents assess it.
    interface_plan["interface_contracts"][0].update(
        inputs="requires state", state="no state exists"
    )
    assert set(validate_plan(interface_plan, {"feature1": "one", "feature2": "two"})) == {
        "worker_a",
        "worker_b",
    }
    schema = PLAN["inputSchema"]
    assert "interface_contracts" in schema["required"]
    assert set(schema["properties"]["interface_contracts"]["items"]["required"]) == set(
        interface_plan["interface_contracts"][0]
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_contracts",
        "missing_input",
        "blank_output",
        "unknown_owner",
        "unknown_consumer",
        "empty_consumers",
        "duplicate_consumers",
        "private_path",
        "duplicate_interface",
        "conflicting_owner",
        "extra_field",
    ],
)
def test_interface_contract_structure_errors_are_rejected(interface_plan, case):
    item = interface_plan["interface_contracts"][0]
    if case == "missing_contracts":
        interface_plan.pop("interface_contracts")
    elif case == "missing_input":
        item.pop("inputs")
    elif case == "blank_output":
        item["outputs"] = " "
    elif case == "unknown_owner":
        item["owner"] = "manager"
    elif case == "unknown_consumer":
        item["consumers"] = ["manager"]
    elif case == "empty_consumers":
        item["consumers"] = []
    elif case == "duplicate_consumers":
        item["consumers"] *= 2
    elif case == "private_path":
        item["path"] = "../private.py"
    elif case == "duplicate_interface":
        interface_plan["interface_contracts"].append(item.copy())
    elif case == "conflicting_owner":
        interface_plan["shared_regions"] = [
            {
                "path": item["path"],
                "symbol": item["symbol"],
                "owner": "worker_b",
                "action": "edit",
                "verification": "test",
            }
        ]
    else:
        item["automatic_correctness"] = True
    with pytest.raises((ValueError, TypeError)):
        validate_plan(interface_plan, {"feature1": "one", "feature2": "two"})


def test_archived_plan_layout_is_readable_but_not_accepted_for_live_planning(interface_plan):
    interface_plan.pop("interface_contracts")
    needs = {"feature1": "one", "feature2": "two"}
    assert validate_plan(interface_plan, needs, layout_version=4)
    with pytest.raises(TypeError, match="interface_contracts"):
        validate_plan(interface_plan, needs)


def test_profiles_share_one_skill_and_single_uses_plain_prompt():
    profiles = [load_profile(ROOT, name) for name in ("single", "static", "periodic", "adaptive")]
    assert profiles[0]["skill"] is None and set(profiles[0]["roles"]) == {"single"}
    assert len({p["prompts"]["worker_a"] for p in profiles[1:]}) == 1
    for profile in profiles[1:]:
        assert len(profile["files"]) == 5
        assert profile["skill"] == SKILL_PATH
        assert len(profile["roles"]["worker_a"]["prompt_sources"]) == 2
        assert len(profile["roles"]["manager"]["prompt_sources"]) == 1
        assert profile["roles"]["manager"]["tools"] == ["inspect_code", "plan_tasks", "coordinate"]


@pytest.mark.parametrize("mutation", ["interval", "trigger", "busy", "caps", "permission"])
def test_policy_drift_is_rejected(tmp_path, mutation):
    shutil.copytree(ROOT / ".agents", tmp_path / ".agents")
    (tmp_path / "configs").mkdir()
    shutil.copyfile(ROOT / "configs/pilot.json", tmp_path / "configs/pilot.json")
    path = tmp_path / SKILL_PATH / "policies.json"
    policies = json.loads(path.read_text())
    if mutation == "interval":
        policies["periodic"]["interval_seconds"] = 61
    elif mutation == "trigger":
        policies["static"]["trigger"] = "qualified_event"
    elif mutation == "busy":
        policies["periodic"]["busy_behavior"] = "queue"
    elif mutation == "caps":
        policies["adaptive"]["max_checks"] = 2
    else:
        policies["adaptive"]["tools"] = ["container_exec"]
    path.write_text(json.dumps(policies))
    with pytest.raises(ValueError, match="supervision drift"):
        load_profile(tmp_path, "periodic")


def test_role_loading_excludes_entry_policy_and_other_role(tmp_path):
    shutil.copytree(ROOT / ".agents", tmp_path / ".agents")
    directory = tmp_path / SKILL_PATH
    (directory / "roles/worker.md").write_text("WORKER_ONLY")
    (directory / "roles/manager.md").write_text("MANAGER_ONLY")
    (directory / "references/events.md").write_text("EVENTS_ONLY")
    (directory / "SKILL.md").write_text("ENTRY_ONLY")
    config = json.loads((ROOT / "configs/pilot.json").read_text())
    profiles = [
        load_profile(tmp_path, condition, config)
        for condition in ("static", "periodic", "adaptive")
    ]
    workers = {p["prompts"]["worker_a"] for p in profiles}
    assert len(workers) == 1
    worker = workers.pop()
    assert "WORKER_ONLY" in worker and "EVENTS_ONLY" in worker
    assert "ENTRY_ONLY" not in worker and "MANAGER_ONLY" not in worker
    assert all(p["prompts"]["worker_b"] == worker for p in profiles)
    for profile in profiles:
        assert "MANAGER_ONLY" in profile["prompts"]["manager"]
        assert "EVENTS_ONLY" not in profile["prompts"]["manager"]
        assert profile["roles"]["worker_a"]["prompt_sha256"] == digest(worker.encode())
        assert profile["roles"]["manager"]["tools"] == ["inspect_code", "plan_tasks", "coordinate"]


def test_prompt_edit_updates_hash_and_single_does_not_require_a_skill(tmp_path):
    config = json.loads((ROOT / "configs/pilot.json").read_text())
    (tmp_path / "prompts").mkdir()
    shutil.copyfile(ROOT / "prompts/single.md", tmp_path / "prompts/single.md")
    single = load_profile(tmp_path, "single", config)
    assert single["skill"] is None and single["policy"] is None
    assert single["prompts"]["single"] == (ROOT / "prompts/single.md").read_text()
    shutil.copytree(ROOT / ".agents", tmp_path / ".agents")
    original = load_profile(tmp_path, "periodic", config)
    events = tmp_path / SKILL_PATH / "references/events.md"
    events.write_text(events.read_text() + "\nNew evidence instruction.\n")
    changed = load_profile(tmp_path, "periodic", config)
    assert (
        changed["roles"]["worker_a"]["prompt_sha256"]
        != original["roles"]["worker_a"]["prompt_sha256"]
    )
    assert (
        changed["roles"]["manager"]["prompt_sha256"]
        == original["roles"]["manager"]["prompt_sha256"]
    )


def test_legacy_config_and_budget_injection_rejected():
    config = json.loads((ROOT / "configs/pilot.json").read_text())
    validate_config(config)
    for changes in (
        {"manager_checks": 2},
        {"reasoning_effort": "medium"},
        {"worker_tool_limit": 60},
        {"allow_existing_credits": True},
        {"periodic_interval_seconds": 60},
    ):
        with pytest.raises(ValueError):
            validate_config(config | changes)


def test_usage_includes_manager_and_unknown_does_not_become_zero():
    assert (
        team_usage(
            {"a": {"totalTokens": 10}, "b": {"totalTokens": 20}, "manager": {"totalTokens": 5}}
        )["totalTokens"]
        == 35
    )
    assert team_usage({"a": {"totalTokens": 10}, "manager": None})["totalTokens"] is None
    assert team_usage({"single": {"totalTokens": 0}})["totalTokens"] == 0
    assert team_usage({})["totalTokens"] is None


def test_execution_has_no_duration_default_or_schema_ceiling():
    from threading import Lock
    from types import SimpleNamespace
    from unittest.mock import Mock

    workspace = ContainerWorkspace.__new__(ContainerWorkspace)
    workspace.lock = Lock()
    workspace.container = SimpleNamespace(
        exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=b"ok"))
    )
    assert workspace.execute("long running command")["exit_code"] == 0
    argv = workspace.container.exec_run.call_args.args[0]
    assert argv[0] == "/bin/bash" and "timeout" not in argv
    assert EXEC["inputSchema"]["required"] == ["command"]
    assert "timeout_seconds" not in EXEC["inputSchema"]["properties"]


def test_thread_creation_pins_actual_high_effort(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from intervention.codex import AppServer

    async def check():
        client = AppServer(tmp_path)
        client.host_directory = SimpleNamespace(name="/empty")
        client.request = AsyncMock(return_value={"model": "gpt-6-luna", "thread": {"id": "t"}})
        await client.new_thread("gpt-6-luna", "prompt", [])
        params = client.request.call_args.args[1]
        assert params["config"]["model_reasoning_effort"] == "high"
        assert params["config"]["features.multi_agent"] is False
        assert params["config"]["features.multi_agent_v2"] is False
        assert "features.code_mode_host" not in params["config"]
        assert params["config"]["mcp_servers"] == {}
        assert params["config"]["web_search"] == "disabled"
        assert params["allowProviderModelFallback"] is False

    asyncio.run(check())
