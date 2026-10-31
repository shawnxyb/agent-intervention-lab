"""P0-only controller. Scheduling and integration never acquire model coding powers."""

from __future__ import annotations

import asyncio
import fcntl
import json
import shlex
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, configuration, docker_client, image_locks, save_json

from intervention.codex import AppServer, ProtocolError
from intervention.evidence import inspect as inspect_public_code
from intervention.evidence import public_test_targets
from intervention.funding import FundingRequired
from intervention.merge import digest, merge_snapshots
from intervention.policy import Event, Policy
from intervention.protocol import (
    PROTOCOL,
    canonical_hash,
    evaluation_inputs,
    implementation_manifest,
    load_profile,
    team_usage,
    validate_config,
    validate_plan,
)
from intervention.readiness import acceptance_status
from intervention.sandbox import ContainerWorkspace
from intervention.tools import ACK, COORDINATE, EXEC, INSPECT, PLAN, REPORT, VERIFY, planning_tool

WORKSPACE_CONTRACT = {
    "worker_checkouts": "Independent writable worktrees; no direct peer messaging or directory access",
    "integration": "Separate disposable worktree; successful merge never synchronizes Worker files",
    "inspection": "Read-only baseline and explicitly identified integration snapshots via inspect_code",
    "verification": "Worker verify_integration uses own current and peer last-published patches only",
    "responsibility": "Manager plans; Workers implement; Controller never resolves source conflicts",
    "assessment": "Agents judge interface correctness and test sufficiency; Controller records facts only",
}


class ProtocolViolation(RuntimeError):
    pass


class Run:
    def __init__(
        self,
        task_name: str,
        strategy: str,
        config: dict,
        kind: str = "pilot",
        repeat: int = 1,
        batch_id: str | None = None,
    ):
        validate_config(config)
        if kind not in {"pilot", "formal"} or repeat not in range(1, 4):
            raise ValueError("Invalid experiment kind/repeat")
        if kind == "formal" and not config["formal_protocol_frozen"]:
            raise ValueError("Formal protocol must be frozen before formal runs")
        self.task = next(t for t in configuration()["tasks"] if t["name"] == task_name)
        self.profile = load_profile(ROOT, strategy, config)
        self.config, self.kind, self.strategy = config, kind, strategy
        self.coding_roles = ("single",) if strategy == "single" else ("worker_a", "worker_b")
        self.policy = (
            None
            if strategy == "single"
            else Policy(
                strategy, interval_seconds=self.profile["policy"].get("interval_seconds", 60)
            )
        )
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{task_name}-{strategy}"
        self.output = ROOT / "runs" / self.run_id
        self.output.mkdir(parents=True)
        self.client = None  # No Docker connection during config validation/construction.
        self.server = AppServer(
            self.output, allow_existing_credits=config["funding"]["allow_existing_credits"]
        )
        self.server.inference_guard = self.inference_allowed
        self.workspaces, self.threads, self.roles = {}, {}, {}
        self.snapshots = dict.fromkeys(self.coding_roles, b"")
        self.calls, self.counts, self.inflight = {}, {}, dict.fromkeys(self.coding_roles, 0)
        self.messages, self.decisions, self.turns = {}, [], {}
        self.delivery_pending = {}
        self.pending_starts = set(self.coding_roles)
        self.phase, self.manager_task = "setup", None
        self.check_decided, self.current_trigger = False, None
        self.manager_turns = []
        self.phase_lock, self.snapshot_lock = asyncio.Lock(), asyncio.Lock()
        self.integration_lock = asyncio.Lock()
        self.inspections, self.integration_checks = [], []
        self.verification_containers_created = 0
        self.worker_locks = {r: asyncio.Lock() for r in self.coding_roles}
        self.report_locks = {r: asyncio.Lock() for r in self.coding_roles}
        self.report_ready = {r: asyncio.Event() for r in self.coding_roles}
        for ready in self.report_ready.values():
            ready.set()
        self.work_ready = asyncio.Event()
        self.work_ready.set()
        self.parked = dict.fromkeys(self.coding_roles, 0)
        self.receipts, self.pauses, self.initial_plan = {}, [], None
        self.assignments, self.planning_turns = {}, []
        self.report = {
            "protocol_id": PROTOCOL,
            "run_id": self.run_id,
            "kind": kind,
            "strategy": strategy,
            "task": task_name,
            "repeat": repeat,
            "batch_id": batch_id
            or ("outlines-" + PROTOCOL if kind == "pilot" else "formal-" + PROTOCOL),
            "config": config,
            "status": "setting_up",
            "sessions_closed": False,
            "scientific_result_available": False,
            "candidate_stages": ["p0"],
            "planning_seconds": 0,
            "initial_prompts": {},
            "role_settings": {
                r: s for r, s in self.profile["roles"].items() if s["inference_enabled"]
            },
            "coding_container_count": len(self.coding_roles),
            "coding_container_resources": {"cpus": 2, "memory_gib": 4, "pids": 256},
            "subscription_only_server_guarantee": "not_enforced",
            "funding_admission": config["funding"]["admission"],
        }
        self.report["code_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        self.report["code_dirty"] = bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        )

    def log(self, event: str, **data) -> None:
        with (self.output / "events.jsonl").open("a") as f:
            f.write(
                json.dumps({"time": time.time(), "event": event, **data}, ensure_ascii=False) + "\n"
            )

    def save(self) -> None:
        usage = {r: self.server.usage.get(t) for r, t in self.threads.items()}
        self.report.update(
            phase=self.phase,
            threads=self.threads,
            tool_counts=self.counts,
            manager_checks=len(self.manager_turns),
            manager_planning_turns=self.planning_turns,
            initial_plan=self.initial_plan,
            report_receipts=self.receipts,
            pauses=self.pauses,
            pause_total_seconds=sum(p.get("paused_seconds", 0) for p in self.pauses),
            workers_parked=self.parked,
            supervision_requests=self.policy.checks if self.policy else 0,
            skipped_ticks=self.policy.skipped_ticks if self.policy else [],
            coordination_decisions=self.decisions,
            messages=self.messages,
            code_inspections=self.inspections,
            integration_checks=self.integration_checks,
            public_evidence=self.public_evidence(),
            verification_containers_created=self.verification_containers_created,
            usage=usage,
            team_usage=team_usage(usage),
            coding_turns=self.turns,
            tools_inflight=self.inflight,
            coding_container_count=sum(role in self.coding_roles for role in self.workspaces),
            controller_container_count=sum(
                role not in self.coding_roles for role in self.workspaces
            ),
        )
        save_json(self.output / "summary.json", self.report)

    def all_coding_ended(self, include_pending: bool = True) -> bool:
        if (include_pending and self.pending_starts) or any(self.inflight.values()):
            return False
        for role in self.coding_roles:
            thread, turn = self.threads.get(role), self.turns.get(role)
            if (
                not turn
                or getattr(self.server, "tools_pending", {}).get(thread, 0)
                or thread in self.server.active
                or (include_pending and thread in self.server.starting)
                or self.server.completed.get((thread, turn), {}).get("status") != "completed"
            ):
                return False
        return True

    def inference_allowed(self, thread: str) -> bool:
        if self.phase == "planning":
            return thread == self.threads.get("manager")
        return (
            self.phase == "development"
            and not self.all_coding_ended(include_pending=False)
            and (thread == self.threads.get("manager") or self.work_ready.is_set())
        )

    async def checkpoint(self, role: str) -> None:
        """Park a tool request/result, without cancelling a command or polling the model."""
        while not self.work_ready.is_set():
            self.parked[role] += 1
            self.log("worker_parked", role=role)
            try:
                await self.work_ready.wait()
            finally:
                self.parked[role] -= 1

    def pending_messages(self, role: str) -> list[dict]:
        return [
            m
            for m in self.messages.values()
            if m["target"] == role and not m.get("acknowledged_at")
        ]

    def receipt_messages(self, role: str) -> list[dict]:
        messages = self.pending_messages(role)
        for message in messages:
            if not message.get("accepted_at"):
                message.update(accepted_at=time.time(), delivery={"mode": "tool_receipt"})
                self.log("message_delivery", **message)
        return [{"message_id": m["message_id"], "text": m["text"]} for m in messages]

    def start_check(self, trigger: dict) -> None:
        if self.manager_task is not None:
            raise ProtocolViolation("Concurrent Manager check")
        self.work_ready.clear()
        pause = {
            "check": trigger["check"],
            "requested_at": time.time(),
            "requested_monotonic": time.monotonic(),
        }
        self.pauses.append(pause)
        self.log("pause_requested", **pause)
        self.manager_task = asyncio.create_task(self.supervise(trigger))

    async def wait_for_workers(self) -> None:
        # A running command drains; a thinking Worker reaches a dynamic tool boundary
        # or completes its turn. Manager inference never overlaps unparked Workers.
        while any(
            self.worker_locks[r].locked()
            or (
                not self.parked[r]
                and (
                    self.threads[r] in self.server.active
                    or self.threads[r] in self.server.starting
                    or getattr(self.server, "tools_pending", {}).get(self.threads[r], 0)
                )
            )
            for r in self.coding_roles
        ):
            self.check_events()
            await asyncio.sleep(0.01)

    async def capture(self, role: str) -> str:
        patch = await asyncio.to_thread(self.workspaces[role].snapshot)
        self.snapshots[role] = patch
        key = digest(patch)
        path = self.output / "snapshots" / role / f"{key}.patch"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(patch)
        self.log("snapshot", role=role, sha256=key)
        return key

    def published_pair(self) -> tuple[str, dict]:
        patches = {r: self.snapshots[r] for r in self.coding_roles}
        pair = digest(patches["worker_a"] + b"\0" + patches["worker_b"])
        directory = self.output / "integrations" / pair
        directory.mkdir(parents=True, exist_ok=True)
        for role, patch in patches.items():
            (directory / f"{role}.patch").write_bytes(patch)
        return pair, patches

    def public_evidence(self) -> dict:
        """Summarize patch presence and test identity, without inferring feature coverage."""
        result = {
            "published_snapshots": {r: digest(p) for r, p in self.snapshots.items()},
            "nonempty_contributions": [r for r, p in self.snapshots.items() if p],
        }
        if self.policy:
            pair = digest(self.snapshots["worker_a"] + b"\0" + self.snapshots["worker_b"])
            latest = next((c for c in reversed(self.integration_checks) if c.get("tests")), None)
            result.update(
                published_snapshot_pair=pair,
                latest_test=None
                if latest is None
                else {
                    "sequence": latest["sequence"],
                    "snapshot_pair": latest["snapshot_pair"],
                    "origin": latest["origin"],
                    "test_paths": latest["test_paths"],
                    "command": latest["tests"]["command"],
                    "exit_code": latest["tests"]["exit_code"],
                    "matches_latest_published_pair": latest["snapshot_pair"] == pair,
                },
            )
        return result

    def record_integration(self, record: dict) -> None:
        self.log("merge_probe", **record)
        for message in self.messages.values():
            role = message["target"]
            before = message.get("issued_snapshots", {}).get(role)
            if before:
                message["code_changed_since_instruction"] = before != digest(self.snapshots[role])
            message["latest_integration"] = {
                "snapshot_pair": record["snapshot_pair"],
                "merge_ok": record["merge_ok"],
                "snapshots": {"worker_a": record["a_sha256"], "worker_b": record["b_sha256"]},
            }
        self.policy.report(
            Event(
                "integration",
                "controller",
                "merge_conflict",
                "Stable snapshot integration",
                record["unmerged_index"] or "Strict merge succeeded",
                record["snapshot_pair"],
                resolved=record["merge_ok"],
            )
        )

    async def monitor(self) -> None:
        pair, patches = self.published_pair()
        async with self.integration_lock:
            record = await asyncio.to_thread(
                merge_snapshots,
                self.workspaces["integration"],
                patches["worker_a"],
                patches["worker_b"],
            )
        assert record["snapshot_pair"] == pair
        self.record_integration(record)

    async def inspect_code(self, role: str, args: dict) -> dict:
        view = args.get("view")
        ws = self.workspaces["integration"]
        identity = {"view": view, "source_base": self.task["base_commit"]}
        async with self.integration_lock:
            if view == "base":
                tree = ws.base
            elif view == "integration" and self.phase == "development":
                pair = args.get("snapshot_pair", "")
                if len(pair) != 64 or any(c not in "0123456789abcdef" for c in pair):
                    return {"error": "Specify an existing immutable snapshot_pair"}
                directory = self.output / "integrations" / pair
                try:
                    patches = [(directory / f"{r}.patch").read_bytes() for r in self.coding_roles]
                except FileNotFoundError:
                    return {"error": "Unknown snapshot_pair; publish/verify snapshots first"}
                if digest(patches[0] + b"\0" + patches[1]) != pair:
                    raise ProtocolViolation("Integration snapshot evidence was modified")
                record = await asyncio.to_thread(merge_snapshots, ws, *patches)
                if not record["merge_ok"]:
                    return {"error": "No merged code view for conflicting snapshots", **record}
                tree = (await asyncio.to_thread(ws.checked, "git write-tree")).strip()
                identity["snapshot_pair"] = pair
            else:
                return {"error": "View unavailable in this phase"}
            try:
                result = await asyncio.to_thread(inspect_public_code, ws, tree, args)
            except (ValueError, TypeError, KeyError) as error:
                return {"error": str(error)}
        record = {"role": role, "phase": self.phase, **identity, "request": args, "result": result}
        self.inspections.append(record)
        save_json(self.output / "code-inspections" / f"{len(self.inspections)}.json", record)
        return {**identity, **result}

    async def verify_integration(self, role: str, args: dict) -> dict:
        # Called with this Worker's operation lock. Never snapshot a moving peer tree.
        async with self.snapshot_lock:
            await self.capture(role)
            _, patches = self.published_pair()
        return await self.check_pair(role, patches, args.get("test_paths"), "worker_request")

    async def check_pair(self, role: str, patches: dict, test_paths: list, origin: str) -> dict:
        """Mechanical public verification; distinguish Agent requests from final checks."""
        pair = digest(patches["worker_a"] + b"\0" + patches["worker_b"])
        if not isinstance(test_paths, list):
            return {"error": "test_paths must be an array; [] requests merge-only verification"}
        ws = self.workspaces["integration"]
        async with self.integration_lock:
            record = await asyncio.to_thread(
                merge_snapshots,
                ws,
                patches["worker_a"],
                patches["worker_b"],
            )
            result = {
                **record,
                "snapshots": {r: digest(p) for r, p in patches.items()},
                "nonempty_contributions": [r for r, p in patches.items() if p],
                "peer_state": "final_frozen_pair"
                if origin == "controller_after_development"
                else "last_published_snapshot",
                "origin": origin,
                "test_paths": test_paths,
                "tests": None,
            }
            if not record["merge_ok"]:
                result["conflict_diff"] = await asyncio.to_thread(
                    ws.checked,
                    "git diff --no-ext-diff --no-textconv",
                )
            elif test_paths:
                tree = (await asyncio.to_thread(ws.checked, "git write-tree")).strip()
                try:
                    targets = await asyncio.to_thread(
                        public_test_targets,
                        ws,
                        tree,
                        test_paths,
                        self.task.get("language", "python"),
                    )
                except (ValueError, TypeError) as error:
                    return {"error": str(error), "snapshot_pair": pair}
                prefix = (
                    "go test -v "
                    if self.task.get("language") == "go"
                    else "python -m pytest -q -- "
                )
                command = prefix + " ".join(shlex.quote(p) for p in targets)
                # Public/self-authored tests may mutate files, modules or process state.
                # Never run them in the reusable code-inspection/merge workspace.
                self.verification_containers_created += 1
                name = f"verification-{self.verification_containers_created}"
                temporary = ContainerWorkspace(
                    self.client,
                    self.report["common_contract"]["image"],
                    self.run_id,
                    name,
                )
                self.workspaces[name] = temporary
                try:
                    environment = await asyncio.to_thread(
                        temporary.prepare, self.task["base_commit"]
                    )
                    save_json(self.output / f"environment-{name}.json", environment)
                    reconstructed = await asyncio.to_thread(
                        merge_snapshots,
                        temporary,
                        patches["worker_a"],
                        patches["worker_b"],
                    )
                    if not reconstructed["merge_ok"] or reconstructed["snapshot_pair"] != pair:
                        raise ProtocolViolation(
                            "Public validation did not reconstruct the requested pair"
                        )
                    tested = await asyncio.to_thread(temporary.execute, command)
                finally:
                    await asyncio.to_thread(temporary.close)
                    self.workspaces.pop(name)
                result["tests"] = {"command": command, **tested}
        result.update(role=role, observed_at=time.time(), sequence=len(self.integration_checks) + 1)
        self.integration_checks.append(result)
        save_json(self.output / "integrations" / pair / f"check-{result['sequence']}.json", result)
        self.record_integration(record)
        # A merge-only check must not erase the last actual test execution.
        if result["tests"] is not None:
            for message in self.messages.values():
                message["latest_public_validation"] = {
                    "snapshot_pair": pair,
                    "snapshots": result["snapshots"],
                    "nonempty_contributions": result["nonempty_contributions"],
                    "sequence": result["sequence"],
                    "tests": result["tests"],
                    "merge_ok": record["merge_ok"],
                }
        self.log("integration_verification", **result)
        return result

    async def handle(self, params: dict) -> dict:
        role = self.roles.get(params["threadId"])
        if role is None:
            return {"error": "Unknown role"}
        key = (role, params["callId"])
        if key in self.calls:
            return await asyncio.shield(self.calls[key])
        future = asyncio.get_running_loop().create_future()
        self.calls[key] = future
        try:
            async with self.phase_lock:
                if self.phase != "development" and not (
                    self.phase == "planning" and role == "manager"
                ):
                    result = {"error": "Development has ended"}
                    future.set_result(result)
                    return result
                if role in self.inflight:
                    self.inflight[role] += 1
            try:
                result = await self.execute_tool(role, params["tool"], params["arguments"])
                if not result.get("error"):
                    self.counts[role] = self.counts.get(role, 0) + 1
                    if self.policy and role in self.coding_roles:
                        self.policy.tool_completed(role, params["callId"])
                self.log(
                    "tool_completed",
                    role=role,
                    call_id=params["callId"],
                    tool=params["tool"],
                    result=result,
                )
                future.set_result(result)
                self.save()
                return result
            finally:
                if role in self.inflight:
                    self.inflight[role] -= 1
        except BaseException as error:
            if not future.done():
                future.set_result({"error": str(error)})
            # A failed controller operation invalidates the run even if the model continues.
            self.tool_failure = error
            raise

    async def execute_tool(self, role: str, name: str, args: dict) -> dict:
        if self.phase not in {"development", "planning"} or self.server.funding_error:
            return {"error": "Development is closed or funding unavailable"}
        allowed = self.profile["roles"][role]["tools"]
        if name not in allowed:
            return {"error": "Tool not available for this role"}
        if name == "inspect_code" and role == "manager":
            if self.phase != "planning" and (
                self.current_trigger is None or self.check_decided or self.work_ready.is_set()
            ):
                return {
                    "error": "Manager inspection is only available during planning or paused supervision"
                }
            return await self.inspect_code(role, args)
        if name == "plan_tasks":
            if self.phase != "planning" or self.initial_plan is not None:
                raise ProtocolViolation("Initial planning is a separate, single-decision phase")
            try:
                assignments = validate_plan(args, self.requirements)
            except (ValueError, TypeError, KeyError) as error:
                return {
                    "recorded": False,
                    "error": str(error),
                    "retryable": True,
                    "next_action": "Correct the plan and call plan_tasks again; Workers remain stopped",
                }
            if not any(
                i["role"] == "manager"
                and i["phase"] == "planning"
                and i["request"].get("operation") == "read"
                and "content" in i["result"]
                and Path(i["result"]["path"]).suffix
                in {
                    ".py",
                    ".pyi",
                    ".js",
                    ".jsx",
                    ".ts",
                    ".tsx",
                    ".go",
                    ".rs",
                    ".c",
                    ".cpp",
                    ".h",
                }
                for i in self.inspections
            ):
                return {
                    "recorded": False,
                    "error": "Manager must inspect actual baseline code before planning",
                    "retryable": True,
                    "next_action": "Read baseline source with inspect_code, then resubmit plan_tasks",
                }
            self.assignments = assignments
            self.initial_plan = dict(args)
            save_json(self.output / "initial-plan.json", self.initial_plan)
            self.log("initial_plan", **self.initial_plan)
            return {"recorded": True, "validation_scope": "structure_and_identifiers_only"}
        if self.phase != "development":
            return {"error": "Only initial planning is available before development"}
        if name in {"container_exec", "inspect_code", "verify_integration"}:
            while True:
                await self.report_ready[role].wait()
                await self.checkpoint(role)
                async with self.worker_locks[role]:
                    if not self.work_ready.is_set() or not self.report_ready[role].is_set():
                        continue
                    messages = self.receipt_messages(role)
                    if messages:
                        return {"executed": False, "messages": messages, "ack_required": True}
                    if name == "container_exec":
                        result = await asyncio.to_thread(
                            self.workspaces[role].execute, args["command"]
                        )
                    elif name == "inspect_code":
                        result = await self.inspect_code(role, args)
                    else:
                        result = await self.verify_integration(role, args)
                    break
            if (
                name == "verify_integration"
                and self.strategy == "adaptive"
                and self.manager_task is None
            ):
                trigger = self.policy.poll()
                if trigger:
                    self.start_check(trigger)
            await self.checkpoint(role)
            if result.get("snapshot_pair"):
                result["matches_latest_published_pair_at_return"] = result[
                    "snapshot_pair"
                ] == digest(self.snapshots["worker_a"] + b"\0" + self.snapshots["worker_b"])
            return {**result, "executed": True, "messages": self.receipt_messages(role)}
        if name == "report_event":
            permitted = (
                {"status"}
                if self.strategy == "single"
                else {"status", "contract_change", "cross_task_blocker"}
            )
            if args["kind"] not in permitted:
                return {"error": "Invalid event kind for this role"}
            identifier = f"report-{len(self.receipts) + 1}"
            receipt = {
                "report_id": identifier,
                "source": role,
                "issue_id": args["issue_id"],
                "received_at": time.time(),
            }
            self.receipts[identifier] = receipt
            self.log("report_received", **receipt)
            async with self.report_locks[role]:
                self.report_ready[role].clear()
                try:
                    await self.checkpoint(role)
                    async with self.worker_locks[role], self.snapshot_lock:
                        snapshot = await self.capture(role)
                        event = Event(source=role, snapshot=snapshot, **args)
                        changed = self.policy.report(event) if self.policy else True
                        self.log("worker_report", changed=changed, payload=asdict(event))
                        for message in self.messages.values():
                            if message["target"] == role and message.get("accepted_at"):
                                message.setdefault("followup_reports", []).append(
                                    {
                                        "snapshot": snapshot,
                                        "issue_id": event.issue_id,
                                        "evidence_version": event.revision,
                                        "resolved_claim": event.resolved,
                                        "observed_at": time.time(),
                                    }
                                )
                        if self.policy:
                            await self.monitor()
                    receipt.update(snapshot=snapshot, evidence_version=event.revision)
                    if self.strategy == "adaptive" and self.manager_task is None:
                        trigger = self.policy.poll()
                        if trigger:
                            self.start_check(trigger)
                    # Release all snapshot/operation locks before waiting for a Manager.
                    await self.checkpoint(role)
                    messages = self.receipt_messages(role)
                    receipt.update(replied_at=time.time(), messages=messages)
                    self.log("report_receipt", **receipt)
                    return {
                        "recorded": changed,
                        "report_id": identifier,
                        "snapshot": snapshot,
                        "messages": messages,
                    }
                finally:
                    self.report_ready[role].set()
        if name == "acknowledge":
            await self.report_ready[role].wait()
            await self.checkpoint(role)
            identifier = args["message_id"]
            pending = self.delivery_pending.get(identifier)
            if pending is not None:
                await asyncio.shield(pending)
            message = self.messages.get(identifier)
            if message is None or message["target"] != role or not message.get("accepted_at"):
                return {"error": "Unknown or undelivered message for this Worker"}
            message.update(acknowledged_at=time.time(), understanding=args["understanding"])
            self.log("message_acknowledged", **message)
            return {"recorded": True, "messages": self.receipt_messages(role)}
        if name == "coordinate":
            async with self.phase_lock:
                if self.phase != "development" or self.all_coding_ended():
                    return {"error": "Team finished; no late coordination"}
                if self.check_decided or self.current_trigger is None:
                    return {"error": "Only one decision in an active supervision check"}
                if self.work_ready.is_set():
                    return {"error": "Workers must be paused before coordination"}
                if any(m["target"] not in self.coding_roles for m in args["messages"]):
                    return {"error": "Unknown target"}
                self.check_decided = True
                self.decisions.append(
                    {
                        "check": self.current_trigger["check"],
                        "trigger": self.current_trigger,
                        "reason": args["reason"],
                        "message_count": len(args["messages"]),
                        "time": time.time(),
                    }
                )
            for item in args["messages"]:
                async with self.phase_lock:
                    if self.phase != "development" or self.all_coding_ended():
                        break
                    identifier = f"message-{len(self.messages) + 1}"
                    message = {
                        **item,
                        "message_id": identifier,
                        "sent_at": time.time(),
                        "check": self.current_trigger["check"],
                        "trigger_issues": [
                            {
                                "source": e["source"],
                                "issue_id": e["issue_id"],
                                "snapshot": e["snapshot"],
                            }
                            for e in self.current_trigger.get("events", [])
                        ],
                        "accepted_at": None,
                        "acknowledged_at": None,
                        "behavior_verified": None,
                        "resolved_by_evidence": None,
                        "issued_snapshots": {r: digest(p) for r, p in self.snapshots.items()},
                    }
                    self.messages[identifier] = message
                    self.log("message_queued", **message)
            return {"recorded": True}
        return {"error": "Unknown tool"}

    def context(self, trigger: dict) -> str:
        # Full evidence is retained. No arbitrary context truncation or token budget.
        return json.dumps(
            {
                "contract": self.contract,
                "workspace_contract": WORKSPACE_CONTRACT,
                "requirements": self.requirements,
                "initial_plan": self.initial_plan,
                "public_evidence": self.public_evidence(),
                "coordination_messages": list(self.messages.values()),
                "trigger": trigger,
                "worker_tool_counts": self.counts,
                "integration_checks": [
                    c
                    for c in self.integration_checks
                    if c["snapshot_pair"]
                    == digest(self.snapshots["worker_a"] + b"\0" + self.snapshots["worker_b"])
                ],
                "diffs": {r: p.decode(errors="replace") for r, p in self.snapshots.items()},
                "worker_activity": {
                    r: self.threads[r] in self.server.active for r in self.coding_roles
                },
                "recent_worker_messages": {
                    r: self.server.messages.get(self.threads[r], [])[-1:] for r in self.coding_roles
                },
            },
            ensure_ascii=False,
        )

    async def deliver_idle_messages(self) -> None:
        """Completed Workers may be continued while their peer is still working."""
        for role in self.coding_roles:
            thread = self.threads[role]
            if thread in self.server.active or self.parked[role]:
                continue
            messages = [m for m in self.pending_messages(role) if not m.get("accepted_at")]
            if not messages:
                continue
            self.pending_starts.add(role)
            pending = asyncio.get_running_loop().create_future()
            for message in messages:
                self.delivery_pending[message["message_id"]] = pending
            try:
                delivery = await self.server.deliver(
                    thread,
                    "Manager instructions; acknowledge each message_id before coding:\n"
                    + json.dumps(messages, ensure_ascii=False),
                    self.config["reasoning_effort"],
                )
                self.turns[role] = delivery["turn_id"]
                for message in messages:
                    message.update(
                        delivery=delivery, accepted_at=message.get("accepted_at") or time.time()
                    )
            except ProtocolError:
                if not self.all_coding_ended(include_pending=False):
                    raise
                for message in messages:
                    message["delivery"] = "team_finished_before_acceptance"
            finally:
                self.pending_starts.discard(role)
                pending.set_result(all(m.get("accepted_at") for m in messages))
                for message in messages:
                    self.delivery_pending.pop(message["message_id"], None)
                    self.log("message_delivery", **message)

    async def supervise(self, trigger: dict) -> None:
        pause = self.pauses[-1]
        await self.wait_for_workers()
        if self.all_coding_ended():
            self.log("manager_check_skipped", reason="team_finished_before_checkpoint", **trigger)
            pause.update(resumed_at=time.time(), skipped=True)
            self.work_ready.set()
            return
        pause["quiescent_at"] = time.time()
        pause["quiescent_monotonic"] = time.monotonic()
        pause["quiescence_wait_seconds"] = (
            pause["quiescent_monotonic"] - pause["requested_monotonic"]
        )
        async with self.snapshot_lock:
            for role in self.coding_roles:
                await self.capture(role)
            await self.monitor()
        # Decisions see the fresh, jointly stable state, not the trigger-time patch pair.
        if self.strategy == "adaptive" and not any(e.eligible for e in self.policy.events.values()):
            pause.update(resumed_at=time.time(), skipped=True)
            self.log("manager_check_skipped", reason="evidence_resolved_before_check", **trigger)
            self.work_ready.set()
            return
        trigger = {
            **trigger,
            "events": [asdict(e) for e in self.policy.events.values()],
            "snapshots": {r: digest(p) for r, p in self.snapshots.items()},
        }
        self.policy.handled.update(
            (e.source, e.issue_id, e.revision) for e in self.policy.events.values()
        )
        self.check_decided, self.current_trigger = False, trigger
        self.log("workers_quiescent", **pause)
        self.log("manager_check", **trigger)
        thread = self.threads["manager"]
        turn = await self.server.turn(
            thread, "SUPERVISION\n" + self.context(trigger), self.config["reasoning_effort"]
        )
        self.manager_turns.append(turn)
        result = await self.server.wait_turn(thread, turn, None)
        if result["status"] != "completed":
            raise ProtocolViolation(f"Manager turn ended abnormally: {result}")
        if not self.check_decided:
            raise ProtocolViolation("Manager ended without a coordinate decision")
        self.log("manager_check_finished", status=result["status"], decision=True)
        self.current_trigger = None
        pause["resumed_at"] = time.time()
        resumed = time.monotonic()
        pause["paused_seconds"] = resumed - pause["requested_monotonic"]
        pause["supervision_pause_seconds"] = resumed - pause["quiescent_monotonic"]
        self.log("workers_resumed", **pause)
        self.work_ready.set()
        await self.deliver_idle_messages()

    async def plan_initial_tasks(self) -> None:
        self.phase = "planning"
        started = time.monotonic()
        self.log("planning_started")
        prompt = "INITIAL_PLANNING\n" + json.dumps(
            {
                "contract": self.contract,
                "requirements": self.requirements,
                "workers": list(self.coding_roles),
                "workspace_contract": WORKSPACE_CONTRACT,
                "code_access": "Use inspect_code(view=base) to read source/public tests before plan_tasks",
            },
            ensure_ascii=False,
        )
        self.report["initial_prompts"]["manager"] = prompt
        turn = await self.server.turn(
            self.threads["manager"], prompt, self.config["reasoning_effort"]
        )
        self.planning_turns.append(turn)
        result = await self.server.wait_turn(self.threads["manager"], turn, None)
        self.check_events()
        if result["status"] != "completed" or self.initial_plan is None:
            raise ProtocolViolation("Manager must complete an initial plan before Workers start")
        self.report["planning_seconds"] = time.monotonic() - started
        self.log("planning_finished", seconds=self.report["planning_seconds"])

    def check_events(self) -> None:
        if self.server.funding_error:
            raise FundingRequired(self.server.funding_error)
        if getattr(self, "tool_failure", None):
            raise self.tool_failure
        if self.server.unexpected_items:
            raise ProtocolViolation("Unexpected host capability used")
        while not self.server.notifications.empty():
            message = self.server.notifications.get_nowait()
            if message.get("method") == "model/rerouted":
                raise ProtocolViolation("Model rerouting detected")
            if message.get("method") == "turn/completed":
                turn = message["params"]["turn"]
                if turn["status"] != "completed":
                    raise RuntimeError(f"Unexpected inference termination: {turn}")
        if self.server.reader.done():
            raise RuntimeError("App Server stopped unexpectedly")

    def preflight(self) -> None:
        status = acceptance_status(ROOT)
        failures = [
            f"{name}: {check.get('reason', 'not passed')}"
            for name, check in status.items()
            if not check["passed"]
        ]
        if failures:
            raise RuntimeError("Live acceptance incomplete: " + "; ".join(failures))
        evidence_path = ROOT / f"artifacts/runtime/{PROTOCOL}.json"
        self.report["runtime_acceptance_sha256"] = digest(evidence_path.read_bytes())

    def load_inputs(self) -> tuple[dict, str]:
        task_dir = (
            ROOT
            / "vendor/CooperBench/dataset"
            / self.task["repository"]
            / (f"task{self.task['task_id']}")
        )
        requirements = {
            f"feature{f}": (task_dir / f"feature{f}/feature.md").read_text()
            for f in self.task["features"]
        }
        contract = (ROOT / f"configs/contracts/{self.task['name']}.md").read_text()
        return requirements, contract

    async def setup(self) -> None:
        self.preflight()
        await self.server.start()
        self.report["start_quota"] = await self.server.check_funding(starting_experiment=True)
        self.client = docker_client(timeout=None)
        self.requirements, self.contract = self.load_inputs()
        self.report["inputs"] = {
            "contract": self.contract,
            "requirements": self.requirements,
            "task": self.task,
        }
        source = implementation_manifest(ROOT)
        image = image_locks()[self.task["name"]]["reference"]
        common = {
            "protocol_id": PROTOCOL,
            "task": self.task,
            "inputs": self.report["inputs"],
            "model": self.config["model"],
            "effort": self.config["reasoning_effort"],
            "image": image,
            "implementation_sha256": canonical_hash(source),
            "config": self.config,
            "evaluation_inputs": evaluation_inputs(ROOT, self.task),
        }
        self.report["common_fingerprint"] = canonical_hash(common)
        self.report["common_contract"] = common
        save_json(self.output / "implementation.json", source)
        save_json(self.output / "effective-profile.json", self.profile)
        for role in (*self.coding_roles, *(("integration",) if self.policy else ())):
            ws = ContainerWorkspace(self.client, image, self.run_id, role)
            self.workspaces[role] = ws
            meta = await asyncio.to_thread(ws.prepare, self.task["base_commit"])
            save_json(self.output / f"environment-{role}.json", meta)
            if role == self.coding_roles[0]:
                save_json(self.output / "environment.json", meta)
        self.server.tool_handler = self.handle
        definitions = {t["name"]: t for t in (EXEC, REPORT, ACK, PLAN, COORDINATE, INSPECT, VERIFY)}
        definitions["plan_tasks"] = planning_tool(self.requirements)
        for role, spec in self.profile["roles"].items():
            if not spec["inference_enabled"]:
                continue
            thread = await self.server.new_thread(
                spec["model"],
                self.profile["prompts"][role],
                [definitions[t] for t in spec["tools"]],
            )
            self.threads[role], self.roles[thread] = thread, role
            self.counts[role] = 0
        if self.policy:
            await self.plan_initial_tasks()
        self.phase, self.report["status"] = "development", "running"
        self.started = time.monotonic()
        if self.policy:
            self.policy.start(self.started)
        self.log("development_started")
        # Requests return on acceptance, not turn completion; both Workers run in parallel.
        for role in self.coding_roles:
            needs = (
                self.requirements
                if role == "single"
                else {
                    self.assignments[role]["feature"]: self.requirements[
                        self.assignments[role]["feature"]
                    ]
                }
            )
            prompt = (
                f"Your role: {role}\nShared contract:\n{self.contract}\nFull feature requests:\n"
                + json.dumps(needs, ensure_ascii=False)
                + (
                    "\nManager initial instructions:\n"
                    + self.assignments[role]["instructions"]
                    + "\nShared edit responsibilities:\n"
                    + json.dumps(self.initial_plan["shared_regions"], ensure_ascii=False)
                    + "\nShared interface contracts:\n"
                    + json.dumps(self.initial_plan["interface_contracts"], ensure_ascii=False)
                    + "\nWorkspace capabilities:\n"
                    + json.dumps(WORKSPACE_CONTRACT)
                    if self.policy
                    else ""
                )
            )
            self.report["initial_prompts"][role] = prompt
            turn = await self.server.turn(
                self.threads[role], prompt, self.config["reasoning_effort"]
            )
            self.turns[role] = turn
            self.pending_starts.discard(role)
        self.save()

    async def develop(self) -> None:
        while True:
            self.check_events()
            if self.manager_task and self.manager_task.done():
                await self.manager_task
                self.manager_task = None
            async with self.phase_lock:
                if self.all_coding_ended():
                    self.phase = "freezing"
                    self.server.inference_open = False
                    self.report["development_seconds"] = time.monotonic() - self.started
                    break
            if self.policy:
                skipped = len(self.policy.skipped_ticks)
                trigger = self.policy.poll(busy=self.manager_task is not None)
                for record in self.policy.skipped_ticks[skipped:]:
                    self.log("periodic_ticks_skipped", **record)
                if trigger:
                    self.start_check(trigger)
            await asyncio.sleep(0.05)
        # No coding tools remain. Cancel supervision; its late output has no authority.
        if self.manager_task:
            self.manager_task.cancel()
            await asyncio.gather(self.manager_task, return_exceptions=True)
            pause = self.pauses[-1]
            if not pause.get("quiescent_at") and not pause.get("resumed_at"):
                # Team completion can win the race against the checkpoint waiter.
                # No Manager turn began; close this request as a skipped checkpoint.
                pause.update(
                    resumed_at=time.time(),
                    skipped=True,
                    reason="team_finished_before_checkpoint",
                )
                self.log("manager_check_skipped", **pause)
                self.work_ready.set()
        manager_thread = self.threads.get("manager")
        if manager_thread in self.server.active:
            await self.server.request(
                "turn/interrupt",
                {"threadId": manager_thread, "turnId": self.server.active[manager_thread]},
            )
        for role in self.coding_roles:
            if not self.server.turn_messages.get((self.threads[role], self.turns[role])):
                raise ProtocolViolation(f"{role} ended without a delivery report")
        self.report["agent_messages"] = {
            r: self.server.messages.get(t, []) for r, t in self.threads.items()
        }

    async def seal_candidate(self) -> None:
        for role in self.coding_roles:
            await self.capture(role)
            (self.output / f"{role}.patch").write_bytes(self.snapshots[role])
        if not self.policy:
            patch = self.snapshots["single"]
            p0 = {"candidate_origin": "single_snapshot", "merge_ok": None, "sha256": digest(patch)}
            (self.output / "p0.patch").write_bytes(patch)
        else:
            ws = self.workspaces["integration"]
            p0 = await asyncio.to_thread(
                merge_snapshots, ws, self.snapshots["worker_a"], self.snapshots["worker_b"]
            )
            p0["candidate_origin"] = "strict_merge"
            if p0["merge_ok"]:
                patch = await asyncio.to_thread(ws.snapshot)
                (self.output / "p0.patch").write_bytes(patch)
                p0["sha256"] = digest(patch)
            else:
                p0.update(sha256=None, joint_success=False, feature_evaluation="not_run")
                (self.output / "p0-conflicts.txt").write_text(p0["unmerged_index"])
                # Entire Git directory, unmerged index, object database, base and conflict files.
                await asyncio.to_thread(ws.checked, "tar -cf /tmp/p0-recovery.tar .")
                recovery = await asyncio.to_thread(ws.read_file, "/tmp/p0-recovery.tar")
                (self.output / "p0-recovery.tar").write_bytes(recovery)
                save_json(
                    self.output / "p0-recovery.json",
                    {
                        "local_base": ws.base,
                        "source_base": self.task["base_commit"],
                        "merge": p0,
                        "archive": "p0-recovery.tar",
                        "patch_order": list(self.coding_roles),
                    },
                )
        self.report["p0"] = p0
        if self.policy:
            pair, patches = self.published_pair()
            requested = sorted(
                {
                    path
                    for c in self.integration_checks
                    if c.get("origin") == "worker_request" and c.get("tests") is not None
                    for path in c.get("test_paths", [])
                }
            )
            final = {
                "snapshot_pair": pair,
                "snapshots": {r: digest(p) for r, p in patches.items()},
                "nonempty_contributions": [r for r, p in patches.items() if p],
                "origin": "controller_after_development",
                "tests": None,
                "status": "merge_conflict" if not p0["merge_ok"] else "not_requested",
            }
            if p0["merge_ok"] and requested:
                final = await self.check_pair(
                    "controller", patches, requested, "controller_after_development"
                )
                final["status"] = "verification_error" if final.get("error") else "executed"
                if digest((self.output / "p0.patch").read_bytes()) != p0["sha256"]:
                    raise ProtocolViolation("Public final verification modified the P0 candidate")
            self.report["final_public_validation"] = final
            save_json(self.output / "final-public-validation.json", final)

    async def execute(self) -> Path:
        start = time.monotonic()
        lock_path = ROOT / ".local/active-experiment.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        run_lock = lock_path.open("a")
        completed, cleanup_errors = False, []
        try:
            fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self.setup()
            await self.develop()
            await self.seal_candidate()
            completed = True
        except BaseException as error:
            self.phase = "interrupted"
            self.report.update(
                status=(
                    "user_interrupted"
                    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
                    else "funding_paused"
                    if isinstance(error, FundingRequired)
                    else "protocol_invalid"
                    if isinstance(error, (ProtocolViolation, ProtocolError))
                    else "infrastructure_interrupted"
                ),
                error=f"{type(error).__name__}: {error}",
            )
            # Last stable evidence survives interruption. No automatic retry or partial candidate.
            for role, patch in self.snapshots.items():
                (self.output / f"interrupted-{role}.patch").write_bytes(patch)
            raise
        finally:
            self.server.inference_open = False
            if self.manager_task:
                self.manager_task.cancel()
                await asyncio.gather(self.manager_task, return_exceptions=True)
            try:
                await self.server.close()
            except Exception as error:  # noqa: BLE001 - preserve cleanup failure evidence
                cleanup_errors.append(str(error))
            for workspace in self.workspaces.values():
                try:
                    await asyncio.to_thread(workspace.close)
                except Exception as error:  # noqa: BLE001 - preserve cleanup failure evidence
                    cleanup_errors.append(str(error))
            if self.client:
                try:
                    self.client.close()
                except Exception as error:  # noqa: BLE001 - preserve cleanup failure evidence
                    cleanup_errors.append(str(error))
            self.report["sessions_closed"] = not cleanup_errors
            self.report["wall_seconds"] = time.monotonic() - start
            if cleanup_errors:
                self.report.update(
                    status="infrastructure_interrupted", cleanup_errors=cleanup_errors
                )
                completed = False
            if self.server.unexpected_items:
                self.report.update(
                    status="protocol_invalid", error="Unexpected host capability used"
                )
                completed = False
            if completed:
                self.phase, self.report["status"] = "closed", "sealed"
                # Logs may still be flushed at shutdown; immutable inputs/candidates are hashed here.
                paths = [
                    p
                    for p in self.output.rglob("*")
                    if p.is_file()
                    and (
                        p.suffix in {".patch", ".tar"}
                        or p.name
                        in {
                            "effective-profile.json",
                            "initial-plan.json",
                            "implementation.json",
                            "environment.json",
                            "p0-conflicts.txt",
                            "p0-recovery.json",
                            "final-public-validation.json",
                            "protocol.jsonl",
                            "events.jsonl",
                            "app-server.stderr.log",
                        }
                        or p.name.startswith("environment-")
                        or p.relative_to(self.output).parts[0]
                        in {"code-inspections", "integrations"}
                    )
                ]
                self.report["evidence_sha256"] = {
                    str(p.relative_to(self.output)): digest(p.read_bytes()) for p in sorted(paths)
                }
            self.save()
            if completed:
                save_json(self.output / "seal.json", self.report)
                self.report["seal_sha256"] = digest((self.output / "seal.json").read_bytes())
                save_json(self.output / "summary.json", self.report)
            fcntl.flock(run_lock, fcntl.LOCK_UN)
            run_lock.close()
        if not completed:
            raise RuntimeError("Cleanup failed; no sealed candidate")
        return self.output
