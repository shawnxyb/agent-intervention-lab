import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from intervention.codex import AppServer, ProtocolError


@pytest.mark.parametrize("kind", ["collabAgentToolCall", "collabToolCall", "futureNativeAction"])
def test_native_capabilities_close_inference_gate_at_start(tmp_path, kind):
    async def check():
        stream = asyncio.StreamReader()
        stream.feed_data(
            (
                json.dumps(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "t",
                            "item": {"type": kind, "tool": "wait"},
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        stream.feed_eof()
        client = AppServer(tmp_path)
        client.process = SimpleNamespace(stdout=stream)
        await client._read()
        assert len(client.unexpected_items) == 1
        assert not client.inference_open
        await client.violation_task
        client.request = AsyncMock()
        with pytest.raises(ProtocolError):
            await client.turn("t", "more work")
        client.request.assert_not_called()

    asyncio.run(check())


def test_idle_and_racing_completion_deliver_exactly_once(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        client.check_funding = AsyncMock()
        client.turn = AsyncMock(return_value="next")
        assert (await client.deliver("t", "message"))["mode"] == "continuation"
        client.turn.assert_awaited_once()
        client.turn.reset_mock()
        client.active["t"] = "old"

        async def race(*_args, **_kwargs):
            client.completed[("t", "old")] = {"status": "completed"}
            raise ProtocolError("turn finished")

        client.request = race
        assert (await client.deliver("t", "message"))["turn_id"] == "next"
        client.turn.assert_awaited_once()

    asyncio.run(check())


def test_unrelated_steer_error_never_duplicates_generation(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        client.check_funding = AsyncMock()
        client.active["t"] = "old"
        client.request = AsyncMock(side_effect=ProtocolError("transport unavailable"))
        client.turn = AsyncMock()
        with pytest.raises(ProtocolError):
            await client.deliver("t", "message")
        client.turn.assert_not_called()

    asyncio.run(check())


def test_cumulative_usage_is_not_summed_and_late_turn_does_not_clear_new_turn(tmp_path):
    async def check():
        stream = asyncio.StreamReader()
        events = [
            {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "new"}}},
            *[
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {"threadId": "t", "tokenUsage": {"total": {"totalTokens": value}}},
                }
                for value in (10, 10, 30)
            ],
            {
                "method": "turn/completed",
                "params": {"threadId": "t", "turn": {"id": "old", "status": "completed"}},
            },
        ]
        for event in events:
            stream.feed_data((json.dumps(event) + "\n").encode())
        stream.feed_eof()
        client = AppServer(tmp_path)
        client.process = SimpleNamespace(stdout=stream)
        await client._read()
        assert client.usage["t"]["totalTokens"] == 30
        assert client.active["t"] == "new"
        assert "missing_thread" not in client.usage

    asyncio.run(check())


def test_turn_start_rechecks_end_gate_after_funding_and_has_no_inference_timeout(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        allowed = [True]
        client.inference_guard = lambda thread: allowed[0]

        async def funding_race():
            allowed[0] = False

        client.check_funding = funding_race
        client.request = AsyncMock()
        with pytest.raises(ProtocolError, match="gate closed"):
            await client.turn("t", "late message")
        client.request.assert_not_called()
        assert not client.starting
        allowed[0] = True
        client.check_funding = AsyncMock()
        client.request = AsyncMock(return_value={"turn": {"id": "new"}})
        assert await client.turn("t", "work") == "new"
        assert client.request.call_args.args[1]["effort"] == "high"
        client.reader = SimpleNamespace(done=lambda: False)

        async def finish():
            await asyncio.sleep(0)
            client.completed[("t", "new")] = {"status": "completed"}

        task = asyncio.create_task(finish())
        assert (await client.wait_turn("t", "new", None))["status"] == "completed"
        await task

    asyncio.run(check())


def test_transport_rechecks_gate_after_waiting_for_write_lock(tmp_path):
    from unittest.mock import Mock

    async def check():
        client = AppServer(tmp_path)
        write = Mock()
        client.process = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()))
        await client.lock.acquire()
        task = asyncio.create_task(client.request("turn/start", {"threadId": "t"}))
        await asyncio.sleep(0)
        client.inference_open = False
        client.lock.release()
        with pytest.raises(ProtocolError, match="transport write"):
            await task
        write.assert_not_called()
        assert not client.pending

    asyncio.run(check())
