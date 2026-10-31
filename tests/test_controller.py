import asyncio
import io
import json
import tarfile
from unittest.mock import AsyncMock

import pytest
from evaluate_candidates import verify_sealed
from intervention.controller import ProtocolViolation
from intervention.funding import FundingRequired


@pytest.mark.parametrize("condition", ["single", "static", "periodic", "adaptive"])
def test_full_p0_paths_and_actual_role_inputs(run_factory, condition):
    run, state, workspaces = run_factory(condition, supervise=condition in {"periodic", "adaptive"})
    path = asyncio.run(run.execute())
    assert run.server.allow_existing_credits is True
    summary = verify_sealed(path)
    assert summary["status"] == "sealed" and summary["sessions_closed"] and state["closed"]
    assert not (path / "p1.patch").exists() and not (path / "evaluation.json").exists()
    assert all(t[2] == "high" for t in state["turns"])
    expected = 1 if condition == "single" else 3
    assert len(summary["threads"]) == expected
    assert summary["team_usage"]["totalTokens"] == expected * 10
    assert summary["team_usage"]["cachedInputTokens"] == expected * 2
    if condition == "single":
        assert set(workspaces) == {"single"} and run.policy is None
        text = state["turns"][0][1]
        assert "feature 1" in text and "feature 2" in text
        assert summary["p0"]["candidate_origin"] == "single_snapshot"
        assert summary["p0"]["merge_ok"] is None
        assert state["prompts"]["single"][2] == ["container_exec", "report_event"]
    else:
        assert "integration" in workspaces and "manager" not in workspaces
        assert summary["p0"]["merge_ok"] is True
        assert state["prompts"]["worker_a"][1] == state["prompts"]["worker_b"][1]
        for role, text, _ in [t for t in state["turns"] if t[0].startswith("worker")][:2]:
            assert ("feature 1" in text) is (role == "worker_a")
            assert ("feature 2" in text) is (role == "worker_b")
        if condition == "static":
            assert "manager" in state["prompts"] and summary["manager_checks"] == 0
            assert len(summary["manager_planning_turns"]) == 1
        else:
            assert state["prompts"]["manager"][2] == ["inspect_code", "plan_tasks", "coordinate"]
            assert summary["manager_checks"] == 1
            message = next(iter(summary["messages"].values()))
            assert message["accepted_at"] and message["behavior_verified"] is None
    (path / "p0.patch").write_text("tampered")
    with pytest.raises(ValueError, match="modified after sealing"):
        verify_sealed(path)


def test_conflict_is_sealed_failure_with_complete_recoverable_evidence(run_factory):
    run, _, _ = run_factory(conflict=True)
    path = asyncio.run(run.execute())
    summary = verify_sealed(path)
    assert summary["p0"]["merge_ok"] is False and summary["p0"]["joint_success"] is False
    assert not (path / "p0.patch").exists() and not (path / "p1.patch").exists()
    with tarfile.open(fileobj=io.BytesIO((path / "p0-recovery.tar").read_bytes())) as archive:
        names = archive.getnames()
        assert "./.git/index" in names and "./one.py" in names
        assert b"<<<<<<<" in archive.extractfile("./one.py").read()
    (path / "p0-conflicts.txt").write_text("tampered")
    with pytest.raises(ValueError, match="modified after sealing"):
        verify_sealed(path)


def test_no_old_tool_limit_duplicate_counting_or_manager_execution(run_factory):
    async def check():
        run, _, _ = run_factory("adaptive")
        await run.setup()
        thread = run.threads["worker_b"]
        run.server.active[thread] = "turn"
        for index in range(65):
            result = await run.handle(
                {
                    "threadId": thread,
                    "callId": str(index),
                    "tool": "container_exec",
                    "arguments": {"command": "true"},
                }
            )
            assert result["exit_code"] == 0
        assert run.counts["worker_b"] == 65
        duplicate = {
            "threadId": thread,
            "callId": "64",
            "tool": "container_exec",
            "arguments": {"command": "false"},
        }
        assert (await run.handle(duplicate))["exit_code"] == 0
        manager = run.threads["manager"]
        assert (
            await run.handle(
                {
                    "threadId": manager,
                    "callId": "bad",
                    "tool": "container_exec",
                    "arguments": {"command": "true"},
                }
            )
        )["error"]
        assert run.counts["manager"] == 2
        run.phase = "freezing"
        assert (await run.handle(duplicate | {"callId": "late"}))["error"]
        assert run.counts["worker_b"] == 65

    asyncio.run(check())


def test_pending_start_tools_and_end_gate_prevent_premature_or_late_continuation(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        assert run.all_coding_ended()
        run.pending_starts.add("worker_a")
        assert not run.all_coding_ended()
        # A pending delivery is not permission to reopen an otherwise finished team.
        assert not run.inference_allowed(run.threads["worker_a"])
        run.pending_starts.clear()
        run.inflight["worker_a"] = 1
        assert not run.all_coding_ended()
        run.inflight["worker_a"] = 0
        run.server.tools_pending[run.threads["worker_a"]] = 1
        assert not run.all_coding_ended()
        run.server.tools_pending.clear()
        run.current_trigger = {"check": 1}
        assert (
            await run.execute_tool(
                "manager",
                "coordinate",
                {"reason": "late", "messages": [{"target": "worker_a", "text": "continue"}]},
            )
        )["error"]
        assert not run.messages

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure,status",
    [
        (asyncio.CancelledError(), "user_interrupted"),
        (FundingRequired("unknown"), "funding_paused"),
        (ProtocolViolation("reroute"), "protocol_invalid"),
        (RuntimeError("transport"), "infrastructure_interrupted"),
    ],
)
def test_interruptions_never_become_candidates_or_restart(run_factory, failure, status):
    run, state, _ = run_factory()
    run.develop = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        asyncio.run(run.execute())
    summary = json.loads((run.output / "summary.json").read_text())
    assert summary["status"] == status and state["closed"]
    assert "p0" not in summary and not (run.output / "seal.json").exists()
    assert (run.output / "interrupted-worker_a.patch").exists()


def test_latest_coding_turn_requires_its_own_delivery_report(run_factory):
    async def check():
        run, _, _ = run_factory("single")
        await run.setup()
        thread = run.threads["single"]
        run.turns["single"] = "latest"
        run.server.completed[(thread, "latest")] = {"status": "completed"}
        assert run.server.messages[thread]  # Earlier reports do not satisfy the new turn.
        with pytest.raises(ProtocolViolation, match="delivery report"):
            await run.develop()

    asyncio.run(check())


def test_coordinate_no_op_is_one_decision_and_does_not_invent_messages(run_factory):
    async def check():
        run, _, _ = run_factory("adaptive")
        await run.setup()
        run.server.active[run.threads["worker_b"]] = "turn"
        run.current_trigger = {"check": 1}
        run.work_ready.clear()
        assert (
            await run.execute_tool("manager", "coordinate", {"reason": "No action", "messages": []})
        )["recorded"]
        assert not run.messages and len(run.decisions) == 1
        assert (
            await run.execute_tool("manager", "coordinate", {"reason": "Again", "messages": []})
        )["error"]

    asyncio.run(check())
