"""Synchronous receipts, joint pause barriers and initial planning; no live models."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from intervention.controller import ProtocolViolation
from intervention.protocol import validate_plan


def report(kind="status", issue="progress"):
    return {
        "issue_id": issue,
        "kind": kind,
        "summary": "public evidence",
        "evidence": "one.py: changed public interface",
        "attempts": [],
        "resolved": False,
    }


def activate(run):
    for role in run.coding_roles:
        run.server.active[run.threads[role]] = run.turns[role]


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def delayed_manager(run, messages):
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(thread, prompt, effort):
        assert thread == run.threads["manager"] and prompt.startswith("SUPERVISION")
        assert effort == "high"
        assert all(
            run.parked[r] or run.threads[r] not in run.server.active for r in run.coding_roles
        )
        run.server.active[thread] = "check"
        result = await run.execute_tool(
            "manager", "coordinate", {"reason": "compatibility", "messages": messages}
        )
        assert result["recorded"]
        entered.set()
        return "check"

    async def wait(thread, turn, timeout=None):
        assert timeout is None
        await release.wait()
        run.server.finish(thread, turn)
        return {"status": "completed"}

    run.server.turn = AsyncMock(side_effect=start)
    run.server.wait_turn = AsyncMock(side_effect=wait)
    return entered, release


@pytest.mark.parametrize("condition", ["single", "static", "periodic", "adaptive"])
def test_ordinary_report_has_correlated_empty_receipt_and_no_supervision(run_factory, condition):
    async def check():
        run, state, _ = run_factory(condition)
        await run.setup()
        role = run.coding_roles[0]
        activate(run)
        before = len(state["turns"])
        result = await run.execute_tool(role, "report_event", report())
        assert result["messages"] == [] and result["snapshot"]
        receipt = run.receipts[result["report_id"]]
        assert receipt["source"] == role and receipt["snapshot"] == result["snapshot"]
        assert receipt["replied_at"] >= receipt["received_at"]
        assert len(state["turns"]) == before and run.manager_task is None

    asyncio.run(check())


def test_periodic_pauses_both_workers_and_requires_ack_before_queued_command(run_factory):
    async def check():
        run, _, workspaces = run_factory("periodic")
        await run.setup()
        activate(run)
        entered, release = delayed_manager(
            run, [{"target": "worker_a", "text": "Preserve shared interface"}]
        )
        run.policy.clock = lambda: run.policy.started + 60.5
        run.start_check(run.policy.poll())
        a = asyncio.create_task(
            run.execute_tool("worker_a", "container_exec", {"command": "echo changed > paused-a"})
        )
        b = asyncio.create_task(
            run.execute_tool("worker_b", "container_exec", {"command": "echo changed > paused-b"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert not a.done() and not b.done()
        assert not (workspaces["worker_a"].root / "paused-a").exists()
        assert not (workspaces["worker_b"].root / "paused-b").exists()
        assert not run.inference_allowed(run.threads["worker_a"])
        release.set()
        await run.manager_task
        result_a, result_b = await asyncio.gather(a, b)
        assert result_a["executed"] is False and result_a["ack_required"]
        assert result_b["executed"] is True
        assert not (workspaces["worker_a"].root / "paused-a").exists()
        message = result_a["messages"][0]
        await run.execute_tool(
            "worker_a",
            "acknowledge",
            {"message_id": message["message_id"], "understanding": "preserve interface"},
        )
        assert (
            await run.execute_tool(
                "worker_a", "container_exec", {"command": "echo changed > paused-a"}
            )
        )["executed"]
        assert (
            run.pauses[0]["requested_at"]
            <= run.pauses[0]["quiescent_at"]
            <= run.pauses[0]["resumed_at"]
        )

    asyncio.run(check())


def test_adaptive_report_waits_for_decision_without_holding_snapshot_or_peer_lock(run_factory):
    async def check():
        run, _, _ = run_factory("adaptive")
        await run.setup()
        activate(run)
        entered, release = delayed_manager(
            run, [{"target": "worker_a", "text": "Check the export"}]
        )
        params = {
            "threadId": run.threads["worker_a"],
            "callId": "same-report",
            "tool": "report_event",
            "arguments": report("contract_change"),
        }
        a = asyncio.create_task(run.handle(params))
        duplicate = asyncio.create_task(run.handle(params))
        await eventually(lambda: run.manager_task is not None)
        # A second report reaches the pause boundary; no deadlock on A's snapshot lock.
        b = asyncio.create_task(run.execute_tool("worker_b", "report_event", report()))
        queued = asyncio.create_task(
            run.execute_tool("worker_a", "container_exec", {"command": "true"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert not a.done() and not duplicate.done() and not b.done() and not queued.done()
        assert not run.snapshot_lock.locked() and not any(
            lock.locked() for lock in run.worker_locks.values()
        )
        release.set()
        await run.manager_task
        result, repeated, peer, command = await asyncio.gather(a, duplicate, b, queued)
        assert result == repeated and result["messages"][0]["text"] == "Check the export"
        assert peer["messages"] == [] and command["executed"] is False
        assert len(run.receipts) == 2

    asyncio.run(check())


def test_pause_drains_existing_command_and_holds_its_result_before_manager(run_factory):
    async def check():
        run, _, workspaces = run_factory("periodic")
        await run.setup()
        activate(run)
        command_started, command_finish = asyncio.Event(), asyncio.Event()
        entered, release = delayed_manager(run, [])
        original = asyncio.to_thread

        async def execute(function, *args):
            if getattr(function, "__name__", "") == "execute" and args == ("slow",):
                command_started.set()
                await command_finish.wait()
                (workspaces["worker_a"].root / "drained").write_text("complete")
                return {"exit_code": 0, "output": "complete"}
            return await original(function, *args)

        from unittest.mock import patch

        with patch("intervention.controller.asyncio.to_thread", side_effect=execute):
            a = asyncio.create_task(
                run.execute_tool("worker_a", "container_exec", {"command": "slow"})
            )
            await command_started.wait()
            run.start_check({"check": 1, "events": []})
            b = asyncio.create_task(
                run.execute_tool("worker_b", "container_exec", {"command": "true"})
            )
            await eventually(lambda: run.parked["worker_b"])
            assert not entered.is_set()
            command_finish.set()
            await asyncio.wait_for(entered.wait(), 2)
            assert not a.done() and b"drained" in run.snapshots["worker_a"]
            release.set()
            await run.manager_task
            assert (await a)["output"] == "complete" and (await b)["exit_code"] == 0

    asyncio.run(check())


def test_pause_waits_for_thinking_worker_to_reach_tool_boundary(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        activate(run)
        entered, release = delayed_manager(run, [])
        run.start_check({"check": 1, "events": []})
        a = asyncio.create_task(run.execute_tool("worker_a", "report_event", report()))
        await eventually(lambda: run.parked["worker_a"])
        assert not entered.is_set()  # B is still generating, not yet paused.
        b = asyncio.create_task(run.execute_tool("worker_b", "report_event", report()))
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        await run.manager_task
        assert all(r["messages"] == [] for r in await asyncio.gather(a, b))

    asyncio.run(check())


def test_empty_decision_releases_both_without_polling_or_time_limit(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        activate(run)
        entered, release = delayed_manager(run, [])
        run.start_check({"check": 1, "events": []})
        reports = [
            asyncio.create_task(run.execute_tool(r, "report_event", report()))
            for r in run.coding_roles
        ]
        await asyncio.wait_for(entered.wait(), 2)
        run.policy.clock = lambda: run.policy.started + 3600
        assert not run.manager_task.done()
        assert run.policy.poll(busy=True) is None
        assert not run.all_coding_ended()
        release.set()
        await run.manager_task
        assert all(result["messages"] == [] for result in await asyncio.gather(*reports))
        assert not run.messages and len(run.decisions) == 1

    asyncio.run(check())


def test_team_completion_before_pause_checkpoint_skips_manager(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        run.server.turn = AsyncMock()
        run.start_check({"check": 1, "events": []})
        await run.manager_task
        run.server.turn.assert_not_called()
        assert run.work_ready.is_set() and run.pauses[-1]["skipped"]

    asyncio.run(check())


def test_development_completion_closes_pending_checkpoint_without_manager(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        run.server.turn = AsyncMock()
        run.start_check({"check": 1, "events": []})
        # Both Workers have finished; develop wins before supervise gets CPU time.
        await run.develop()
        run.server.turn.assert_not_called()
        pause = run.pauses[-1]
        assert pause["skipped"] and pause["resumed_at"]
        assert pause["reason"] == "team_finished_before_checkpoint"
        assert not pause.get("quiescent_at") and not run.manager_turns
        assert run.work_ready.is_set() and run.phase == "freezing"

    asyncio.run(check())


@pytest.mark.parametrize(
    "case", ["duplicate_worker", "duplicate_feature", "missing", "unknown", "blank"]
)
def test_invalid_initial_assignment_is_not_silently_repaired(case):
    plan = {
        "reason": "partition",
        "shared_regions": [],
        "interface_contracts": [],
        "assignments": [
            {"target": "worker_a", "feature": "feature1", "instructions": "Preserve exports"},
            {"target": "worker_b", "feature": "feature2", "instructions": "Preserve exports"},
        ],
    }
    if case == "duplicate_worker":
        plan["assignments"][1]["target"] = "worker_a"
    elif case == "duplicate_feature":
        plan["assignments"][1]["feature"] = "feature1"
    elif case == "missing":
        plan["assignments"].pop()
    elif case == "unknown":
        plan["assignments"][1]["feature"] = "feature3"
    else:
        plan["assignments"][0]["instructions"] = " "
    with pytest.raises(ValueError):
        validate_plan(plan, {"feature1": "one", "feature2": "two"})


def test_manager_plan_controls_actual_worker_input_and_precedes_development_clock(run_factory):
    async def check():
        run, state, _ = run_factory("static")
        original = run.server.turn

        async def turn(thread, prompt, effort):
            if prompt.startswith("INITIAL_PLANNING"):
                assert not run.turns and run.policy.started is None
                assert not run.inference_allowed(run.threads["worker_a"])
                await run.execute_tool(
                    "manager",
                    "inspect_code",
                    {
                        "view": "base",
                        "operation": "read",
                        "path": "one.py",
                    },
                )
                await run.handle(
                    {
                        "threadId": thread,
                        "callId": "custom-plan",
                        "tool": "plan_tasks",
                        "arguments": {
                            "reason": "reverse mapping",
                            "shared_regions": [],
                            "interface_contracts": [
                                {
                                    "path": "one.py",
                                    "symbol": "public_value",
                                    "owner": "worker_a",
                                    "consumers": ["worker_b"],
                                    "inputs": "none",
                                    "outputs": "declared value",
                                    "state": "module state",
                                    "sequence": "A defines, B consumes",
                                    "errors": "preserve existing exceptions",
                                    "verification": "B verifies through public entry",
                                }
                            ],
                            "assignments": [
                                {
                                    "target": "worker_a",
                                    "feature": "feature2",
                                    "instructions": "A-specific plan",
                                },
                                {
                                    "target": "worker_b",
                                    "feature": "feature1",
                                    "instructions": "B-specific plan",
                                },
                            ],
                        },
                    }
                )
                run.server.finish(thread, "planning")
                return "planning"
            return await original(thread, prompt, effort)

        run.server.turn = turn
        await run.setup()
        workers = {role: prompt for role, prompt, _ in state["turns"]}
        assert "feature 2" in workers["worker_a"] and "feature 1" not in workers["worker_a"]
        assert "A-specific plan" in workers["worker_a"]
        assert "feature 1" in workers["worker_b"] and "B-specific plan" in workers["worker_b"]
        for role in ("worker_a", "worker_b"):
            contracts = (
                workers[role]
                .split("Shared interface contracts:\n", 1)[1]
                .split("\nWorkspace capabilities:", 1)[0]
            )
            assert json.loads(contracts) == run.initial_plan["interface_contracts"]
        assert run.policy.started is not None and run.report["planning_seconds"] >= 0
        with pytest.raises(ProtocolViolation):
            await run.execute_tool("manager", "plan_tasks", run.initial_plan)

    asyncio.run(check())


def test_missing_initial_plan_prevents_worker_start(run_factory):
    async def check():
        run, _, _ = run_factory("static")

        async def no_plan(thread, prompt, effort):
            run.server.finish(thread, "planning")
            return "planning"

        run.server.turn = no_plan
        with pytest.raises(ProtocolViolation, match="initial plan"):
            await run.setup()
        assert not run.turns

    asyncio.run(check())


def test_funding_failure_during_pause_never_releases_worker_into_more_work(run_factory):
    import json

    from intervention.funding import FundingRequired

    async def check():
        run, _, _ = run_factory("periodic", supervise=True)
        setup = run.setup
        events = {}

        async def prepare():
            await setup()
            events["entered"], events["release"] = delayed_manager(run, [])

        run.setup = prepare
        task = asyncio.create_task(run.execute())
        await eventually(lambda: "entered" in events)
        await asyncio.wait_for(events["entered"].wait(), 2)
        assert not run.work_ready.is_set()
        run.server.funding_error = "subscription exhausted"
        with pytest.raises(FundingRequired):
            await asyncio.wait_for(task, 2)
        summary = json.loads((run.output / "summary.json").read_text())
        assert summary["status"] == "funding_paused" and not (run.output / "seal.json").exists()
        assert not run.work_ready.is_set()
        assert all(not r.get("replied_at") for r in summary["report_receipts"].values())

    asyncio.run(check())


def test_manager_without_decision_invalidates_run_and_leaves_waiting_reports_unreleased(
    run_factory,
):
    import json

    run, _, _ = run_factory("periodic", supervise=True)
    original = run.server.turn

    async def missing_decision(thread, prompt, effort):
        if prompt.startswith("SUPERVISION"):
            run.server.finish(thread, "check")
            return "check"
        return await original(thread, prompt, effort)

    run.server.turn = missing_decision
    with pytest.raises(ProtocolViolation, match="coordinate decision"):
        asyncio.run(run.execute())
    summary = json.loads((run.output / "summary.json").read_text())
    assert summary["status"] == "protocol_invalid" and not (run.output / "seal.json").exists()
    assert not run.work_ready.is_set()


def test_late_idle_delivery_cannot_reopen_finished_team(run_factory):
    from intervention.codex import ProtocolError

    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        run.server.active[run.threads["worker_b"]] = "turn"
        run.current_trigger = {"check": 1}
        run.work_ready.clear()
        await run.execute_tool(
            "manager",
            "coordinate",
            {"reason": "race", "messages": [{"target": "worker_a", "text": "check"}]},
        )
        run.work_ready.set()

        async def deliver(*args):
            run.server.finish(run.threads["worker_b"])
            assert not run.inference_allowed(run.threads["worker_a"])
            raise ProtocolError("Inference gate closed during funding read")

        run.server.deliver = deliver
        await run.deliver_idle_messages()
        assert run.messages["message-1"]["delivery"] == "team_finished_before_acceptance"
        assert run.all_coding_ended()

    asyncio.run(check())


def test_idle_delivery_network_wait_does_not_lock_peer_and_early_ack_waits_receipt(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        run.server.active[run.threads["worker_b"]] = "turn"
        run.current_trigger = {"check": 1}
        run.work_ready.clear()
        await run.execute_tool(
            "manager",
            "coordinate",
            {
                "reason": "compatibility",
                "messages": [{"target": "worker_a", "text": "check interface"}],
            },
        )
        run.work_ready.set()
        entered, release = asyncio.Event(), asyncio.Event()

        async def deliver(*args):
            entered.set()
            await release.wait()
            return {"mode": "continuation", "turn_id": "continued"}

        run.server.deliver = deliver
        delivery = asyncio.create_task(run.deliver_idle_messages())
        await entered.wait()
        assert (await run.execute_tool("worker_b", "container_exec", {"command": "true"}))[
            "executed"
        ]
        ack = asyncio.create_task(
            run.execute_tool(
                "worker_a", "acknowledge", {"message_id": "message-1", "understanding": "ok"}
            )
        )
        await asyncio.sleep(0)
        assert not ack.done()
        release.set()
        await delivery
        assert (await ack)["recorded"]
        message = run.messages["message-1"]
        assert message["acknowledged_at"] >= message["accepted_at"]

    asyncio.run(check())


def test_successive_pause_cannot_leak_a_previous_waiter_through_closed_gate(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        run.work_ready.clear()
        waiter = asyncio.create_task(run.checkpoint("worker_a"))
        await eventually(lambda: run.parked["worker_a"])
        run.work_ready.set()
        run.work_ready.clear()  # The next check starts before the old waiter is scheduled.
        await asyncio.sleep(0)
        assert not waiter.done() and run.parked["worker_a"]
        run.work_ready.set()
        await waiter
        assert not run.parked["worker_a"]

    asyncio.run(check())


def test_adaptive_rechecks_resolved_evidence_before_inference(run_factory):
    from intervention.policy import Event

    async def check():
        run, _, _ = run_factory("adaptive")
        await run.setup()
        activate(run)
        event = Event("interface", "worker_a", "contract_change", "signature", "one.py", "old")
        run.policy.report(event)
        trigger = run.policy.poll()
        run.start_check(trigger)
        # An already-admitted report can resolve the issue before both Workers park.
        run.policy.report(
            Event(
                "interface",
                "worker_a",
                "contract_change",
                "verified",
                "one.py public test passed",
                "new",
                resolved=True,
            )
        )
        run.server.turn = AsyncMock()
        waiters = [
            asyncio.create_task(run.execute_tool(r, "container_exec", {"command": "true"}))
            for r in run.coding_roles
        ]
        await run.manager_task
        await asyncio.gather(*waiters)
        run.server.turn.assert_not_called()
        assert run.pauses[-1]["skipped"] and run.work_ready.is_set()

    asyncio.run(check())


def test_ack_parked_during_check_receives_new_manager_instructions(run_factory):
    async def check():
        run, _, _ = run_factory("periodic")
        await run.setup()
        activate(run)
        run.messages["previous"] = {
            "message_id": "previous",
            "target": "worker_a",
            "text": "old",
            "accepted_at": 1,
            "acknowledged_at": None,
        }
        entered, release = delayed_manager(
            run, [{"target": "worker_a", "text": "new interface instruction"}]
        )
        run.start_check({"check": 1, "events": []})
        ack = asyncio.create_task(
            run.execute_tool(
                "worker_a",
                "acknowledge",
                {"message_id": "previous", "understanding": "old instruction"},
            )
        )
        peer = asyncio.create_task(
            run.execute_tool("worker_b", "container_exec", {"command": "true"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        await run.manager_task
        result = await ack
        await peer
        assert result["recorded"] and result["messages"][0]["text"] == "new interface instruction"
        assert run.messages["previous"]["acknowledged_at"]

    asyncio.run(check())


def test_invalid_plan_can_be_corrected_in_same_turn_before_workers_start(run_factory):
    async def check():
        run, state, _ = run_factory("static")
        original = run.server.turn

        async def turn(thread, prompt, effort):
            if not prompt.startswith("INITIAL_PLANNING"):
                return await original(thread, prompt, effort)
            await run.execute_tool(
                "manager",
                "inspect_code",
                {
                    "view": "base",
                    "operation": "read",
                    "path": "one.py",
                },
            )
            region = {
                "path": "one.py",
                "symbol": "module declarations",
                "owner": "worker_a",
                "action": "add declaration",
                "verification": "public tests",
            }
            plan = {
                "reason": "separate blocks",
                "shared_regions": [region, region | {"owner": "worker_b"}],
                "interface_contracts": [],
                "assignments": [
                    {"target": "worker_a", "feature": "feature1", "instructions": "one"},
                    {"target": "worker_b", "feature": "feature2", "instructions": "two"},
                ],
            }

            async def submit(call_id):
                return await run.handle(
                    {"threadId": thread, "callId": call_id, "tool": "plan_tasks", "arguments": plan}
                )

            # More than one rejection is recoverable; no retry counter terminates planning.
            for call_id in ("invalid1", "invalid2", "invalid3"):
                result = await submit(call_id)
                assert result["retryable"] and not result["recorded"]
                assert "one.py" in result["error"] and "module declarations" in result["error"]
                assert run.initial_plan is None and not run.assignments and not run.turns
                assert not getattr(run, "tool_failure", None)
                assert not (run.output / "initial-plan.json").exists()
                assert not any(r.startswith("worker") for r, _, _ in state["turns"])
                assert not run.inference_allowed(run.threads["worker_a"])
            plan["shared_regions"] = [
                region | {"symbol": "credit_card"},
                region | {"symbol": "hash_sha256", "owner": "worker_b"},
            ]
            assert (await submit("valid"))["recorded"]
            run.server.finish(thread, "planning")
            return "planning"

        run.server.turn = turn
        await run.setup()
        assert set(run.assignments) == {"worker_a", "worker_b"}
        assert set(run.turns) == {"worker_a", "worker_b"}
        assert len(run.planning_turns) == 1
        with pytest.raises(ProtocolViolation):
            await run.execute_tool("manager", "plan_tasks", run.initial_plan)

    asyncio.run(check())
