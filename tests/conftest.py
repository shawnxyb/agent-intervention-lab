"""Local Git fixtures and a protocol simulator: no Docker or model inference."""

import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from common import ROOT
from intervention import controller
from test_merge import LocalFixture


@pytest.fixture
def run_factory(monkeypatch, tmp_path):
    shutil.copytree(ROOT / ".agents", tmp_path / ".agents")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    task = {
        "name": "outlines",
        "repository": "toy",
        "task_id": 1,
        "features": [1, 2],
        "base_commit": "synthetic",
    }
    for feature in task["features"]:
        path = tmp_path / f"vendor/CooperBench/dataset/toy/task1/feature{feature}/feature.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"Complete requirement for feature {feature}")
        (path.parent / "tests.patch").write_text("Synthetic test input")
    (tmp_path / "vendor/CooperBench/dataset/toy/task1/runner.sh").write_text("Synthetic runner")
    pins = tmp_path / "docs/environment-dependencies/outlines.freeze.txt"
    pins.parent.mkdir(parents=True)
    pins.write_text("pytest==9.0.0")
    path = tmp_path / "configs/contracts/outlines.md"
    path.parent.mkdir(parents=True)
    path.write_text("Shared contract")
    shutil.copyfile(ROOT / "configs/pilot.json", tmp_path / "configs/pilot.json")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    workspaces = {}
    state = {"conflict": False, "supervise": False, "turns": [], "prompts": {}, "closed": False}

    class Workspace(LocalFixture):
        def __init__(self, _client, _image, run_id, role):
            directory = tmp_path / run_id / role / "repo"
            directory.mkdir(parents=True)
            super().__init__(directory)
            workspaces[role] = self

        def prepare(self, _expected):
            return {"image": "synthetic@sha256:abc", "source_base": "synthetic"}

        def snapshot(self):
            self.checked("git add -A")
            return self.checked(f"git diff --cached --binary {self.base}").encode()

        def read_file(self, path):
            if path == "/tmp/p0-recovery.tar":
                return (self.root.parent / "p0-recovery.tar").read_bytes()
            raise ValueError(path)

        def execute(self, command, *args):
            if command.startswith("python -m pytest"):
                command = shlex.quote(sys.executable) + command[len("python") :]
            command = command.replace(
                "/tmp/p0-recovery.tar", str(self.root.parent / "p0-recovery.tar")
            )
            return super().execute(command)

        def close(self):
            pass

    class Server:
        def __init__(self, _output, allow_existing_credits=False):
            self.allow_existing_credits = allow_existing_credits
            self.output = _output
            self.active, self.completed, self.messages, self.usage = {}, {}, {}, {}
            self.turn_messages = {}
            self.starting, self.tools_pending = set(), {}
            self.notifications = asyncio.Queue()
            self.handlers, self.unexpected_items = set(), []
            self.reader = SimpleNamespace(done=lambda: False)
            self.funding_error = None
            self.created = []
            self.inference_open = True

        async def start(self):
            (self.output / "protocol.jsonl").write_text('{"fixture":"offline transport"}\n')

        async def check_funding(self, *, starting_experiment=False):
            pass

        async def new_thread(self, model, prompt, tools):
            role = next(
                r
                for r in state["run"].profile["roles"]
                if state["run"].profile["roles"][r]["inference_enabled"] and r not in self.created
            )
            self.created.append(role)
            state["prompts"][role] = (model, prompt, [t["name"] for t in tools])
            return state["run"].run_id + ":" + role

        def finish(self, thread, turn="turn"):
            self.active.pop(thread, None)
            self.completed[(thread, turn)] = {"status": "completed"}
            self.turn_messages.setdefault((thread, turn), []).append("Delivery report")
            self.messages.setdefault(thread, []).append(
                "Delivery: implemented; public tests recorded"
            )
            self.usage[thread] = {
                "totalTokens": 10,
                "inputTokens": 6,
                "cachedInputTokens": 2,
                "outputTokens": 4,
                "reasoningOutputTokens": 3,
            }

        async def turn(self, thread, prompt, effort):
            assert effort == "high"
            assert self.inference_open and self.inference_guard(thread)
            run = state["run"]
            role = run.roles[thread]
            state["turns"].append((role, prompt, effort))
            self.active[thread] = "turn"
            if role == "single":
                for index, name in enumerate(("one.py", "two.py")):
                    (workspaces[role].root / name).write_text(f"value = {index + 2}\n")
                self.finish(thread)
            elif role == "worker_a":
                (workspaces[role].root / "one.py").write_text("value = 2\n")
                self.finish(thread)
            elif role == "worker_b":
                name = "one.py" if state["conflict"] else "two.py"
                (workspaces[role].root / name).write_text("value = 3\n")
                if state["supervise"]:
                    # The actual report below supplies the Adaptive event. A second
                    # synthetic source here races it and invents another check.
                    # Cross the boundary clearly: float cancellation around a real
                    # monotonic origin can turn exactly +60 into 59.999999999.
                    run.policy.clock = lambda: run.policy.started + 60.5

                    async def report_and_finish():
                        await run.handle(
                            {
                                "threadId": thread,
                                "callId": "report",
                                "tool": "report_event",
                                "arguments": {
                                    "issue_id": "interface",
                                    "kind": "contract_change",
                                    "summary": "signature",
                                    "evidence": "one.py: changed",
                                    "attempts": [],
                                    "resolved": False,
                                },
                            }
                        )
                        self.finish(thread)

                    task = asyncio.create_task(report_and_finish())
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
                else:
                    self.finish(thread)
            else:
                assert role == "manager"
                if prompt.startswith("INITIAL_PLANNING"):
                    await run.handle(
                        {
                            "threadId": thread,
                            "callId": "inspect",
                            "tool": "inspect_code",
                            "arguments": {"view": "base", "operation": "read", "path": "one.py"},
                        }
                    )
                    result = await run.handle(
                        {
                            "threadId": thread,
                            "callId": "plan",
                            "tool": "plan_tasks",
                            "arguments": {
                                "reason": "Separate complete features",
                                "shared_regions": [],
                                "interface_contracts": [],
                                "assignments": [
                                    {
                                        "target": "worker_a",
                                        "feature": "feature1",
                                        "instructions": "Preserve interfaces",
                                    },
                                    {
                                        "target": "worker_b",
                                        "feature": "feature2",
                                        "instructions": "Preserve interfaces",
                                    },
                                ],
                            },
                        }
                    )
                    assert result["recorded"]
                    self.finish(thread, "planning")
                    return "planning"
                await run.handle(
                    {
                        "threadId": thread,
                        "callId": "decision",
                        "tool": "coordinate",
                        "arguments": {
                            "reason": "Preserve shared interface",
                            "messages": [{"target": "worker_a", "text": "Confirm compatibility"}],
                        },
                    }
                )
                self.finish(thread)
            return "turn"

        async def deliver(self, thread, prompt, effort):
            assert self.inference_guard(thread) and self.inference_open and effort == "high"
            state["turns"].append(("worker_a", prompt, effort))

            async def accept_and_finish():
                await asyncio.sleep(0)
                run = state["run"]
                for message in run.pending_messages(run.roles[thread]):
                    await run.handle(
                        {
                            "threadId": thread,
                            "callId": "ack-" + message["message_id"],
                            "tool": "acknowledge",
                            "arguments": {
                                "message_id": message["message_id"],
                                "understanding": "ok",
                            },
                        }
                    )
                self.finish(thread, "continuation")

            task = asyncio.create_task(accept_and_finish())
            self.handlers.add(task)
            task.add_done_callback(self.handlers.discard)
            return {"mode": "continuation", "turn_id": "continuation"}

        async def wait_turn(self, thread, turn, timeout=None):
            assert timeout is None
            return self.completed[(thread, turn)]

        async def close(self):
            for task in list(self.handlers):
                task.cancel()
            await asyncio.gather(*self.handlers, return_exceptions=True)
            state["closed"] = True

    monkeypatch.setattr(controller, "ROOT", tmp_path)
    monkeypatch.setattr(controller, "configuration", lambda: {"tasks": [task]})
    monkeypatch.setattr(
        controller, "image_locks", lambda: {"outlines": {"reference": "synthetic@sha256:abc"}}
    )
    monkeypatch.setattr(
        controller, "docker_client", lambda **_kw: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(controller, "ContainerWorkspace", Workspace)
    monkeypatch.setattr(controller, "AppServer", Server)
    # Live acceptance is never manufactured by an offline test.
    monkeypatch.setattr(controller.Run, "preflight", lambda self: None)
    config = json.loads((ROOT / "configs/pilot.json").read_text())

    def factory(condition="static", conflict=False, supervise=False):
        state.update(conflict=conflict, supervise=supervise, turns=[], prompts={}, closed=False)
        workspaces.clear()
        run = controller.Run("outlines", condition, config)
        state["run"] = run
        return run, state, workspaces

    return factory
