"""Public Git views and fixed-pair validation, using local Git and pytest only."""

import asyncio
import json
import shlex
import sys

import pytest
from intervention.batch import verify_sealed
from intervention.controller import ProtocolViolation
from intervention.evidence import inspect, public_test_targets
from intervention.protocol import validate_plan
from test_merge import LocalFixture


def test_shared_region_requires_one_owner_and_rejects_duplicate_ownership():
    region = {
        "path": "one.py",
        "symbol": "public_interface",
        "owner": "worker_a",
        "action": "Preserve the peer's call contract",
        "verification": "tests/test_public.py",
    }
    plan = {
        "reason": "explicit boundary",
        "shared_regions": [region],
        "interface_contracts": [],
        "assignments": [
            {"target": "worker_a", "feature": "feature1", "instructions": "one"},
            {"target": "worker_b", "feature": "feature2", "instructions": "two"},
        ],
    }
    assert validate_plan(plan, {"feature1": "one", "feature2": "two"})["worker_a"]
    assert validate_plan(
        plan
        | {"shared_regions": [region, region | {"symbol": "other_interface", "owner": "worker_b"}]},
        {"feature1": "one", "feature2": "two"},
    )["worker_b"]
    with pytest.raises(ValueError, match="single owner"):
        validate_plan(
            plan | {"shared_regions": [region, region | {"owner": "worker_b"}]},
            {"feature1": "one", "feature2": "two"},
        )


def test_git_view_does_not_follow_symlinks_or_read_worktree_changes(tmp_path):
    ws = LocalFixture(tmp_path)
    (tmp_path / "link.py").symlink_to("/etc/passwd")
    (tmp_path / "tests.patch").write_text("hidden input")
    ws.checked("git add .\ngit commit -qm links")
    tree = ws.checked("git rev-parse HEAD").strip()
    (tmp_path / "one.py").write_text("mutated checkout")
    assert inspect(ws, tree, {"operation": "read", "path": "one.py"})["content"] == "value = 1\n"
    listed = inspect(ws, tree, {"operation": "list", "path": ""})["paths"]
    assert "link.py" not in listed and "tests.patch" not in listed
    for path in ["../one.py", "/etc/passwd", ".git/config", "link.py", "tests.patch"]:
        with pytest.raises(ValueError):
            inspect(ws, tree, {"operation": "read", "path": path})
    assert inspect(ws, tree, {"operation": "search", "path": "", "query": "value"})["matches"]


def test_public_test_targets_are_paths_and_cannot_inject_shell_or_flags(tmp_path):
    ws = LocalFixture(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_public.py").write_text("def test_ok(): pass\n")
    ws.checked("git add .\ngit commit -qm tests")
    tree = ws.checked("git rev-parse HEAD").strip()
    assert public_test_targets(ws, tree, ["tests/test_public.py::test_ok"])
    for target in ["../private", "one.py", "-p", "tests; touch escaped", "tests.patch", "/tests"]:
        with pytest.raises(ValueError):
            public_test_targets(ws, tree, [target])


@pytest.mark.parametrize("condition", ["static", "periodic", "adaptive"])
def test_workers_get_identical_read_only_fixed_pair_validation(run_factory, condition):
    async def check():
        run, _, workspaces = run_factory(condition)
        await run.setup()
        a, b, integrated = (workspaces[r] for r in ["worker_a", "worker_b", "integration"])
        test_dir = a.root / "tests"
        test_dir.mkdir()
        (test_dir / "test_pair.py").write_text(
            "from pathlib import Path\n"
            "def test_pair():\n"
            "    root = Path(__file__).parents[1]\n"
            "    a, b = {}, {}\n"
            "    exec((root / 'one.py').read_text(), a)\n"
            "    exec((root / 'two.py').read_text(), b)\n"
            "    assert (a['value'], b['value']) == (2, 3)\n"
        )
        # Prove the same test fails on A's isolated tree, then passes on the paired tree.
        command = shlex.quote(sys.executable) + " -m pytest -q tests/test_pair.py"
        assert a.execute(command)["exit_code"] != 0
        await run.capture("worker_b")
        original = integrated.execute

        def execute(command, *args):
            if command.startswith("python -m pytest"):
                command = shlex.quote(sys.executable) + command[len("python") :]
            return original(command, *args)

        integrated.execute = execute
        result = await run.execute_tool(
            "worker_a", "verify_integration", {"test_paths": ["tests/test_pair.py"]}
        )
        if run.manager_task:
            await run.manager_task
        assert result["merge_ok"] and result["tests"]["exit_code"] == 0
        assert result["snapshots"]["worker_b"]
        assert (a.root / "two.py").read_text() == "value = 1\n"
        assert (b.root / "one.py").read_text() == "value = 1\n"
        view = await run.execute_tool(
            "worker_b",
            "inspect_code",
            {
                "view": "integration",
                "snapshot_pair": result["snapshot_pair"],
                "operation": "read",
                "path": "one.py",
            },
        )
        assert view["content"] == "value = 2\n"
        # Changing A's live contribution cannot change a previously identified view.
        (a.root / "one.py").write_text("value = 99\n")
        await run.capture("worker_a")
        again = await run.execute_tool(
            "worker_b",
            "inspect_code",
            {
                "view": "integration",
                "snapshot_pair": result["snapshot_pair"],
                "operation": "read",
                "path": "one.py",
            },
        )
        assert again["content"] == "value = 2\n"
        assert again["matches_latest_published_pair_at_return"] is False
        assert run.verification_containers_created == 1
        assert not any(r.startswith("verification-") for r in run.workspaces)
        assert not run.manager_turns and not run.pauses
        assert (await run.execute_tool("manager", "verify_integration", {"test_paths": []}))[
            "error"
        ]
        assert (
            await run.execute_tool(
                "manager",
                "inspect_code",
                {
                    "view": "base",
                    "operation": "read",
                    "path": "one.py",
                },
            )
        )["error"]  # No ad-hoc Manager inspection outside a supervised check.

    asyncio.run(check())


def test_conflicting_pair_exposes_diff_without_running_tests_or_rescuing(run_factory):
    async def check():
        run, _, workspaces = run_factory("static", conflict=True)
        await run.setup()
        await run.capture("worker_b")
        result = await run.execute_tool("worker_a", "verify_integration", {"test_paths": ["tests"]})
        assert not result["merge_ok"] and result["tests"] is None
        assert "<<<<<<<" in result["conflict_diff"]
        assert (workspaces["worker_a"].root / "one.py").read_text() == "value = 2\n"
        assert (workspaces["worker_b"].root / "one.py").read_text() == "value = 3\n"
        assert not run.manager_turns

    asyncio.run(check())


@pytest.mark.parametrize(
    "tool,args",
    [
        ("inspect_code", {"view": "base", "operation": "read", "path": "one.py"}),
        ("verify_integration", {"test_paths": []}),
    ],
)
def test_new_worker_tools_obey_joint_pause_and_ack_gate(run_factory, tool, args):
    from test_coordination import activate, delayed_manager

    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        activate(run)
        entered, release = delayed_manager(
            run,
            [{"target": "worker_a", "text": "Review your interface first"}],
        )
        run.start_check({"check": 1, "events": []})
        own = asyncio.create_task(run.execute_tool("worker_a", tool, args))
        peer = asyncio.create_task(
            run.execute_tool("worker_b", "container_exec", {"command": "true"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert not own.done() and not peer.done()
        release.set()
        await run.manager_task
        result = await own
        await peer
        assert result["executed"] is False and result["ack_required"]
        assert not run.integration_checks

    asyncio.run(check())


def test_manager_cannot_plan_without_reading_baseline_code(run_factory):
    async def check():
        run, _, _ = run_factory("static")
        original = run.server.turn

        async def no_inspection(thread, prompt, effort):
            if prompt.startswith("INITIAL_PLANNING"):
                result = await run.execute_tool(
                    "manager",
                    "plan_tasks",
                    {
                        "reason": "not inspected",
                        "shared_regions": [],
                        "interface_contracts": [],
                        "assignments": [
                            {"target": "worker_a", "feature": "feature1", "instructions": "one"},
                            {"target": "worker_b", "feature": "feature2", "instructions": "two"},
                        ],
                    },
                )
                assert result["retryable"] and "inspect actual baseline" in result["error"]
                assert run.initial_plan is None and not run.assignments and not run.turns
                run.server.finish(thread, "planning")
                return "planning"
            return await original(thread, prompt, effort)

        run.server.turn = no_inspection
        with pytest.raises(ProtocolViolation, match="must complete an initial plan"):
            await run.setup()
        assert not run.turns

    asyncio.run(check())


def test_ack_code_change_and_joint_validation_remain_distinct(run_factory):
    async def check():
        run, _, workspaces = run_factory("periodic")
        await run.setup()
        await run.capture("worker_a")
        await run.capture("worker_b")
        run.server.active[run.threads["worker_b"]] = "active"
        run.current_trigger = {"check": 1}
        run.work_ready.clear()
        await run.execute_tool(
            "manager",
            "coordinate",
            {
                "reason": "interface",
                "messages": [{"target": "worker_a", "text": "check your interface"}],
            },
        )
        run.work_ready.set()
        receipt = await run.execute_tool("worker_a", "container_exec", {"command": "true"})
        assert not receipt["executed"]
        message = next(iter(run.messages.values()))
        await run.execute_tool(
            "worker_a",
            "acknowledge",
            {
                "message_id": message["message_id"],
                "understanding": "understood",
            },
        )
        await run.execute_tool("worker_a", "verify_integration", {"test_paths": []})
        assert message["acknowledged_at"] and message["code_changed_since_instruction"] is False
        assert message["latest_integration"]["merge_ok"]
        assert "latest_public_validation" not in message
        (workspaces["worker_a"].root / "one.py").write_text("value = 42\n")
        await run.execute_tool("worker_a", "verify_integration", {"test_paths": []})
        assert message["code_changed_since_instruction"] is True
        assert message["behavior_verified"] is message["resolved_by_evidence"] is None

    asyncio.run(check())


def test_test_identity_and_patch_presence_do_not_imply_feature_correctness(run_factory):
    async def check():
        run, _, workspaces = run_factory("periodic")
        await run.setup()
        run.server.active[run.threads["worker_b"]] = "active"
        run.current_trigger = {"check": 1}
        run.work_ready.clear()
        await run.execute_tool(
            "manager",
            "coordinate",
            {"reason": "review evidence", "messages": [{"target": "worker_a", "text": "review"}]},
        )
        run.work_ready.set()
        await run.execute_tool("worker_a", "container_exec", {"command": "true"})
        message = next(iter(run.messages.values()))
        await run.execute_tool(
            "worker_a",
            "acknowledge",
            {"message_id": message["message_id"], "understanding": "understood"},
        )
        tests = workspaces["worker_a"].root / "tests"
        tests.mkdir()
        # A green test does not establish either feature's correctness.
        (tests / "test_public.py").write_text("def test_baseline(): assert True\n")
        tested = await run.execute_tool(
            "worker_a", "verify_integration", {"test_paths": ["tests/test_public.py"]}
        )
        assert tested["tests"]["exit_code"] == 0
        assert tested["nonempty_contributions"] == ["worker_a"]
        assert run.public_evidence()["latest_test"]["matches_latest_published_pair"] is True
        before = message["latest_public_validation"]
        assert before["snapshot_pair"] == tested["snapshot_pair"]

        # Publishing B changes the pair. Merge-only checks cannot refresh an old test.
        await run.capture("worker_b")
        assert run.public_evidence()["latest_test"]["matches_latest_published_pair"] is False
        merged = await run.execute_tool("worker_a", "verify_integration", {"test_paths": []})
        assert merged["merge_ok"] and merged["tests"] is None
        assert merged["nonempty_contributions"] == ["worker_a", "worker_b"]
        assert tested["nonempty_contributions"] == ["worker_a"]
        assert message["latest_public_validation"] == before
        assert message["latest_integration"]["snapshot_pair"] == merged["snapshot_pair"]
        assert message["behavior_verified"] is message["resolved_by_evidence"] is None
        context = json.loads(run.context({"check": 2}))
        assert context["public_evidence"]["latest_test"]["snapshot_pair"] == tested["snapshot_pair"]
        assert context["public_evidence"]["latest_test"]["matches_latest_published_pair"] is False
        assert context["coordination_messages"][0]["acknowledged_at"]
        assert context["coordination_messages"][0]["latest_public_validation"] == before
        run.save()
        saved = json.loads((run.output / "summary.json").read_text())
        assert saved["public_evidence"] == context["public_evidence"]

    asyncio.run(check())


def test_shutdown_logs_are_sealed_and_late_native_capability_prevents_seal(run_factory):
    run, _, _ = run_factory("single")
    path = asyncio.run(run.execute())
    assert "protocol.jsonl" in verify_sealed(path)["evidence_sha256"]
    (path / "protocol.jsonl").write_text("tampered after close")
    with pytest.raises(ValueError, match="modified after sealing"):
        verify_sealed(path)

    run, _, _ = run_factory("single")
    close = run.server.close

    async def late_item():
        await close()
        run.server.unexpected_items.append({"item": "native wait at shutdown"})

    run.server.close = late_item
    with pytest.raises(RuntimeError):
        asyncio.run(run.execute())
    assert not (run.output / "seal.json").exists()
    assert json.loads((run.output / "summary.json").read_text())["status"] == "protocol_invalid"


def test_final_public_check_uses_final_pair_without_repair_or_model_feedback(run_factory):
    async def check():
        run, state, workspaces = run_factory("static")
        await run.setup()
        a = workspaces["worker_a"]
        (a.root / "tests").mkdir()
        (a.root / "tests/test_final.py").write_text(
            "from pathlib import Path\n"
            "def test_current():\n"
            "    data = {}\n"
            "    exec((Path(__file__).parents[1] / 'one.py').read_text(), data)\n"
            "    assert data['value'] == 2\n"
        )
        await run.capture("worker_b")
        earlier = await run.execute_tool(
            "worker_a", "verify_integration", {"test_paths": ["tests/test_final.py"]}
        )
        assert earlier["tests"]["exit_code"] == 0
        (a.root / "one.py").write_text("value = 42\n")
        before_turns = len(state["turns"])
        run.phase = "freezing"
        run.server.inference_open = False
        await run.seal_candidate()
        final = run.report["final_public_validation"]
        assert (
            final["snapshot_pair"] == run.report["p0"]["snapshot_pair"] != earlier["snapshot_pair"]
        )
        assert final["tests"]["exit_code"] != 0
        assert final["origin"] == "controller_after_development" and final["role"] == "controller"
        assert len(state["turns"]) == before_turns and not run.manager_turns
        assert (a.root / "one.py").read_text() == "value = 42\n"
        assert "+value = 42" in (run.output / "p0.patch").read_text()
        assert run.report["p0"]["merge_ok"] is True

    asyncio.run(check())


def test_go_public_targets_compile_packages_and_reject_shell_inputs(tmp_path):
    ws = LocalFixture(tmp_path)
    (tmp_path / "router_test.go").write_text("package chi\n")
    (tmp_path / "middleware").mkdir()
    (tmp_path / "middleware/auth_test.go").write_text("package middleware\n")
    ws.checked("git add .\ngit commit -qm go-tests")
    tree = ws.checked("git rev-parse HEAD").strip()
    assert public_test_targets(ws, tree, ["router_test.go", "./..."], "go") == [".", "./..."]
    assert public_test_targets(ws, tree, ["middleware/auth_test.go"], "go") == ["./middleware"]
    for target in ["../private", "-run", "one.py", ".; touch escaped", "/tests", "unknown/..."]:
        with pytest.raises(ValueError):
            public_test_targets(ws, tree, [target], "go")
