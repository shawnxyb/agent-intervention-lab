"""Versioned App Server transport. Credentials remain in the normal Codex store."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
import tomllib
from pathlib import Path

from intervention.funding import FundingRequired, require_experiment_start, require_subscription


class ProtocolError(RuntimeError):
    pass


def overrides(host_directory: str) -> list[str]:
    # Disable host capabilities before creating any experiment thread. The only
    # coding tool is client-routed container_exec, in a separate Docker container.
    flags = [
        "shell_tool",
        "unified_exec",
        "view_image",
        "apps",
        "plugins",
        "memories",
        "multi_agent",
        "multi_agent_v2",
        "multi_agent_v2_dynamic_tools",
        "browser_use",
        "browser_use_external",
        "computer_use",
        "image_generation",
        "hooks",
        "shell_snapshot",
        "code_mode",
        "workspace_dependencies",
        "skill_search",
        "goals",
        "unbounded_connection_retries",
    ]
    values = [f"features.{key}=false" for key in flags]
    executable = Path(shutil.which("codex")).resolve()
    binaries = list(executable.parents[1].glob("node_modules/@openai/*/vendor/*/bin/codex"))
    if not binaries:
        binaries = [executable]
    filesystem = {":root": "deny", ":minimal": "read", host_directory: "read"}
    filesystem.update({str(path): "read" for path in binaries})
    policy = "{" + ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in filesystem.items()) + "}"
    values += [
        "features.skip_host_skill_discovery=true",
        'web_search="disabled"',
        "project_doc_max_bytes=0",
        "mcp_servers={}",
        'default_permissions="experiment-host"',
        "sandbox_workspace_write={}",
        'approval_policy="never"',
        'approvals_reviewer="user"',
        'shell_environment_policy.inherit="none"',
        "permissions.experiment-host.filesystem=" + policy,
        "permissions.experiment-host.network.enabled=false",
    ]
    return [part for value in values for part in ("-c", value)]


class AppServer:
    def __init__(self, output: Path, allow_existing_credits: bool = False):
        self.output = output
        self.pending = {}
        self.sequence = 0
        self.active = {}
        self.starting = set()
        self.inference_open = True
        self.inference_guard = lambda _thread: True
        self.completed = {}
        self.started_turns = set()
        self.usage = {}
        self.messages = {}
        self.turn_messages = {}
        self.unexpected_items = []
        self.notifications = asyncio.Queue()
        self.tool_handler = None
        self.handlers = set()
        self.tools_pending = {}
        self.lock = asyncio.Lock()
        self.process = None
        self.allow_existing_credits = allow_existing_credits
        self.funding_error = None
        self.funding_task = None
        self.violation_task = None

    def reject_capability(self, message: dict) -> None:
        self.unexpected_items.append(message)
        self.inference_open = False
        if self.violation_task is None or self.violation_task.done():
            self.violation_task = asyncio.create_task(self._interrupt_active())

    def record(self, direction: str, message: dict) -> None:
        # account/read and config/read are deliberately not sent to the event log.
        with (self.output / "protocol.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {"time": time.time(), "direction": direction, "message": message},
                    ensure_ascii=False,
                )
                + "\n"
            )

    async def start(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self.host_directory = tempfile.TemporaryDirectory(prefix="intervention-host-")
        self.stderr = (self.output / "app-server.stderr.log").open("wb")
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            *overrides(self.host_directory.name),
            cwd=self.host_directory.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr,
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "manager_intervention_experiment", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized"})

    async def send(self, message: dict) -> None:
        async with self.lock:
            if message.get("method") in {"turn/start", "turn/steer"}:
                thread = message.get("params", {}).get("threadId")
                if not self.inference_open or not self.inference_guard(thread):
                    raise ProtocolError("Inference gate closed before transport write")
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 45) -> dict:
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = (future, method)
        message = {"id": identifier, "method": method, "params": params}
        if method not in {"account/read", "config/read"}:
            self.record("request", message)
        try:
            await self.send(message)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)

    async def _read(self) -> None:
        try:
            while raw := await self.process.stdout.readline():
                message = json.loads(raw)
                if "id" in message and "method" not in message:
                    entry = self.pending.get(message["id"])
                    if entry:
                        future, method = entry
                        if method not in {"account/read", "config/read"}:
                            self.record("response", message)
                        if not future.done():
                            if "error" in message:
                                future.set_exception(
                                    ProtocolError(f"{method}: {json.dumps(message['error'])}")
                                )
                            else:
                                future.set_result(message.get("result", {}))
                    continue
                self.record("event", message)
                method, params = message.get("method"), message.get("params", {})
                if method in {"item/started", "item/completed"}:
                    # Allow known inert events and client-routed tools only. Unknown
                    # native capabilities fail closed at start, before waiting for completion.
                    kind = params.get("item", {}).get("type")
                    if kind not in {
                        "userMessage",
                        "agentMessage",
                        "reasoning",
                        "dynamicToolCall",
                        "plan",
                        "contextCompaction",
                    }:
                        self.reject_capability(message)
                if "id" in message:
                    if method != "item/tool/call":
                        self.reject_capability(message)
                    tool_thread = params.get("threadId")
                    self.tools_pending[tool_thread] = self.tools_pending.get(tool_thread, 0) + 1
                    task = asyncio.create_task(self._handle(message))
                    self.handlers.add(task)

                    def finished(task, thread=tool_thread):
                        self.handlers.discard(task)
                        self.tools_pending[thread] -= 1

                    task.add_done_callback(finished)
                    continue
                thread = params.get("threadId")
                if method == "turn/started":
                    self.active[thread] = params["turn"]["id"]
                    self.started_turns.add((thread, params["turn"]["id"]))
                elif method == "turn/completed":
                    turn = params["turn"]
                    self.completed[(thread, turn["id"])] = turn
                    if self.active.get(thread) == turn["id"]:
                        self.active.pop(thread, None)
                elif method == "thread/tokenUsage/updated":
                    self.usage[thread] = params["tokenUsage"]["total"]
                elif method == "account/rateLimits/updated":
                    try:
                        require_subscription(params, self.allow_existing_credits)
                    except FundingRequired as error:
                        self.funding_error = str(error)
                        self.inference_open = False
                        if self.funding_task is None or self.funding_task.done():
                            self.funding_task = asyncio.create_task(self._interrupt_active())
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        self.messages.setdefault(thread, []).append(item.get("text", ""))
                        message_turn = params.get("turnId") or self.active.get(thread)
                        self.turn_messages.setdefault((thread, message_turn), []).append(
                            item.get("text", "")
                        )
                await self.notifications.put(message)
        except Exception as error:  # noqa: BLE001 - transport boundary returns structured failure
            for future, _ in self.pending.values():
                if not future.done():
                    future.set_exception(error)
        finally:
            for future, _ in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("App Server closed stdout"))

    async def _handle(self, message: dict) -> None:
        try:
            if self.funding_error or self.unexpected_items:
                reply = {
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": "Experiment stopped at funding or capability boundary",
                    },
                }
            elif message["method"] != "item/tool/call" or self.tool_handler is None:
                reply = {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Capability not available in this experiment",
                    },
                }
            else:
                result = await self.tool_handler(message["params"])
                reply = {
                    "id": message["id"],
                    "result": {
                        "contentItems": [{"type": "inputText", "text": json.dumps(result)}],
                        "success": not bool(result.get("error")),
                    },
                }
        except Exception as error:  # noqa: BLE001 - transport boundary returns structured failure
            reply = {
                "id": message["id"],
                "result": {
                    "contentItems": [{"type": "inputText", "text": str(error)}],
                    "success": False,
                },
            }
        self.record("tool_result", reply)
        await self.send(reply)

    async def new_thread(self, model: str, instructions: str, tools: list[dict]) -> str:
        # Pin the same restrictions at thread creation, rather than relying only
        # on the server process's configuration layer and inherited user defaults.
        settings = {}
        values = overrides(self.host_directory.name)[1::2]
        for entry in values:
            key, value = entry.split("=", 1)
            settings[key] = tomllib.loads("value=" + value)["value"]
        settings["model_reasoning_effort"] = "high"
        result = await self.request(
            "thread/start",
            {
                "model": model,
                "cwd": self.host_directory.name,
                "permissions": "experiment-host",
                "approvalPolicy": "never",
                "baseInstructions": instructions,
                "developerInstructions": "",
                "dynamicTools": tools,
                "ephemeral": True,
                "allowProviderModelFallback": False,
                "config": settings,
            },
        )
        if result.get("instructionSources"):
            raise ProtocolError("Unexpected inherited instructions")
        if result["model"] != model:
            raise ProtocolError("Model changed at thread creation")
        return result["thread"]["id"]

    async def turn(self, thread: str, prompt: str, effort: str = "high") -> str:
        if not self.inference_open or not self.inference_guard(thread):
            raise ProtocolError("Inference gate is closed")
        self.starting.add(thread)
        try:
            return await self._start_turn(thread, prompt, effort)
        finally:
            self.starting.discard(thread)

    async def _start_turn(self, thread: str, prompt: str, effort: str) -> str:
        await self.check_funding()
        if not self.inference_open or not self.inference_guard(thread):
            raise ProtocolError("Inference gate closed during funding check")
        result = await self.request(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": prompt}],
                "effort": effort,
                "permissions": "experiment-host",
            },
        )
        turn_id = result["turn"]["id"]
        self.started_turns.add((thread, turn_id))
        if (thread, turn_id) not in self.completed:
            self.active[thread] = turn_id
        return turn_id

    async def wait_turn(self, thread: str, turn: str, timeout: float | None = None) -> dict:
        async with asyncio.timeout(timeout):
            while (thread, turn) not in self.completed:
                if self.funding_error:
                    raise FundingRequired(self.funding_error)
                if self.reader.done():
                    raise ProtocolError("App Server stopped during turn")
                await asyncio.sleep(0.05)
        return self.completed[(thread, turn)]

    async def deliver(self, thread: str, prompt: str, effort: str = "high") -> dict:
        if not self.inference_open or not self.inference_guard(thread):
            raise ProtocolError("Inference gate is closed")
        await self.check_funding()
        if not self.inference_open or not self.inference_guard(thread):
            raise ProtocolError("Inference gate closed during funding check")
        active = self.active.get(thread)
        if active:
            try:
                result = await self.request(
                    "turn/steer",
                    {
                        "threadId": thread,
                        "expectedTurnId": active,
                        "input": [{"type": "text", "text": prompt}],
                    },
                )
                return {"mode": "steer", "turn_id": result["turnId"]}
            except ProtocolError:
                # Only a confirmed completion allows continuation; unrelated RPC
                # errors must never silently start another generation.
                if (thread, active) not in self.completed:
                    raise
        return {"mode": "continuation", "turn_id": await self.turn(thread, prompt, effort)}

    async def check_funding(self, *, starting_experiment: bool = False) -> dict | None:
        if self.funding_error:
            raise FundingRequired(self.funding_error)
        snapshot = await self.request("account/rateLimits/read", {})
        if self.funding_error:
            raise FundingRequired(self.funding_error)
        try:
            require_subscription(snapshot, self.allow_existing_credits)
            if starting_experiment:
                return require_experiment_start(snapshot, self.allow_existing_credits)
        except FundingRequired as error:
            self.funding_error = str(error)
            self.inference_open = False
            await self._interrupt_active()
            raise

    async def _interrupt_active(self) -> None:
        for thread, turn in list(self.active.items()):
            try:
                await self.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            except (ProtocolError, TimeoutError):
                # Controller also sees funding_error and closes the process.
                pass

    async def close(self) -> None:
        if self.process is not None:
            if self.process.returncode is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 10)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            await self.reader
            if self.funding_task:
                self.funding_task.cancel()
                await asyncio.gather(self.funding_task, return_exceptions=True)
            if self.violation_task:
                self.violation_task.cancel()
                await asyncio.gather(self.violation_task, return_exceptions=True)
            for task in list(self.handlers):
                task.cancel()
            await asyncio.gather(*self.handlers, return_exceptions=True)
            self.stderr.close()
            self.host_directory.cleanup()
