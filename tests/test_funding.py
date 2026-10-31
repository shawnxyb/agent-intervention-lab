import asyncio
from unittest.mock import AsyncMock

import pytest
from intervention.codex import AppServer
from intervention.funding import (
    FundingRequired,
    credits_available,
    experiment_start_status,
    funding_status,
    require_experiment_start,
    require_subscription,
    subscription_status,
)


def quota(percent):
    return {
        "rateLimits": {
            "primary": {"usedPercent": 0},
            "secondary": {"usedPercent": percent},
            "credits": {"hasCredits": True, "balance": "100"},
        }
    }


def start_quota(five_hour_used, weekly_used):
    snapshot = quota(weekly_used)
    snapshot["rateLimits"]["primary"].update(usedPercent=five_hour_used, windowDurationMins=300)
    snapshot["rateLimits"]["secondary"]["windowDurationMins"] = 10080
    return snapshot


@pytest.mark.parametrize(
    "five_hour_used,weekly_used,allowed",
    [
        (0, 0, True),
        (89, 96, True),
        (90, 97, True),
        (90.001, 0, True),
        (0, 97.001, True),
        (91, 98, True),
        (100, 0, False),
        (0, 100, False),
    ],
)
def test_no_start_reserve_threshold_but_exhausted_subscription_requires_credits(
    five_hour_used, weekly_used, allowed
):
    snapshot = start_quota(five_hour_used, weekly_used)
    check = experiment_start_status(snapshot)
    assert check["passed"] is allowed
    if allowed:
        assert require_experiment_start(snapshot)["remaining_percent"] == {
            "five_hour": 100 - five_hour_used,
            "weekly": 100 - weekly_used,
        }
    else:
        with pytest.raises(FundingRequired, match="New experiment refused"):
            require_experiment_start(snapshot)


@pytest.mark.parametrize("duration", [None, 60, "300"])
def test_quota_duration_does_not_impose_a_start_reserve(duration):
    snapshot = start_quota(0, 0)
    snapshot["rateLimits"]["primary"]["windowDurationMins"] = duration
    assert experiment_start_status(snapshot)["passed"]
    assert not experiment_start_status({})["passed"]


def test_start_reserve_uses_current_bucket_instead_of_stale_legacy_bucket():
    snapshot = start_quota(0, 0)
    snapshot["rateLimitsByLimitId"] = {"codex": start_quota(100, 0)["rateLimits"]}
    assert not experiment_start_status(snapshot)["passed"]


def test_reserve_does_not_stop_turns_of_an_already_running_experiment(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        client.request = AsyncMock(return_value=start_quota(95, 99))
        await client.check_funding()
        assert client.inference_open and client.funding_error is None
        result = await client.check_funding(starting_experiment=True)
        assert result["passed"] and result["minimum_remaining_percent"] == {}
        assert client.inference_open
        assert all(c.args[0] == "account/rateLimits/read" for c in client.request.await_args_list)

    asyncio.run(check())


def test_controller_refuses_unavailable_funding_before_docker_and_model_calls(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from intervention import controller

    async def check():
        server = AppServer(tmp_path)
        server.start = AsyncMock()
        server.request = AsyncMock(return_value=start_quota(100, 0))
        probe = SimpleNamespace(preflight=lambda: None, server=server, report={})
        docker = Mock()
        monkeypatch.setattr(controller, "docker_client", docker)
        with pytest.raises(FundingRequired, match="No confirmed usable"):
            await controller.Run.setup(probe)
        docker.assert_not_called()
        assert server.started_turns == set()
        assert [c.args[0] for c in server.request.await_args_list] == ["account/rateLimits/read"]

    asyncio.run(check())


def test_exhausted_subscription_does_not_imply_credit_authorization():
    with pytest.raises(FundingRequired):
        require_subscription(quota(100), False)
    require_subscription(quota(100), True)
    assert require_experiment_start(quota(100), True)["credits_allowed"]
    require_subscription(quota(30), False)
    with pytest.raises(FundingRequired):
        require_subscription({}, False)


def test_current_limit_map_overrides_legacy_bucket():
    snapshot = quota(0)
    snapshot["rateLimitsByLimitId"] = {"codex": quota(100)["rateLimits"]}
    assert subscription_status(snapshot) == "exhausted"


def test_model_turn_never_starts_when_subscription_is_exhausted(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        client.request = AsyncMock(return_value=quota(100))
        with pytest.raises(FundingRequired):
            await client.turn("thread", "Do work")
        assert [call.args[0] for call in client.request.await_args_list] == [
            "account/rateLimits/read"
        ]

    asyncio.run(check())


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "0", -1, True])
def test_missing_or_invalid_applicable_window_is_unknown(value):
    snapshot = quota(0)
    snapshot["rateLimits"]["primary"] = {"usedPercent": value}
    assert subscription_status(snapshot) == "unknown"


def test_missing_window_is_not_assumed_available():
    assert subscription_status({"rateLimits": {"primary": {"usedPercent": 0}}}) == "unknown"


def test_funding_stop_is_latched_even_if_a_later_request_would_succeed(tmp_path):
    async def check():
        client = AppServer(tmp_path)
        client.request = AsyncMock(side_effect=[quota(100), quota(0)])
        with pytest.raises(FundingRequired):
            await client.check_funding()
        with pytest.raises(FundingRequired):
            await client.check_funding()
        assert client.request.await_count == 1
        assert client.inference_open is False

    asyncio.run(check())


@pytest.mark.parametrize(
    "snapshot",
    [
        {"rateLimits": None},
        {"rateLimitsByLimitId": {"codex": None}},
        {"rateLimitsByLimitId": ["malformed"]},
    ],
)
def test_unavailable_limit_objects_are_unknown_funding_not_transport_errors(snapshot):
    assert subscription_status(snapshot) == "unknown"
    with pytest.raises(FundingRequired):
        require_subscription(snapshot)


@pytest.mark.parametrize("subscription_used", [99.999, 100])
def test_authorized_credits_admit_start_and_followup_turns(tmp_path, subscription_used):
    async def check():
        client = AppServer(tmp_path, allow_existing_credits=True)
        client.request = AsyncMock(return_value=start_quota(subscription_used, 100))
        result = await client.check_funding(starting_experiment=True)
        assert result["passed"] and result["credits_available"]
        assert result["minimum_remaining_percent"] == {}
        await client.check_funding()
        assert client.inference_open and client.funding_error is None

    asyncio.run(check())


@pytest.mark.parametrize(
    "credits,available",
    [
        ({"hasCredits": True, "balance": "2.5", "unlimited": False}, True),
        ({"hasCredits": True, "balance": None, "unlimited": False}, True),
        ({"hasCredits": False, "balance": "0", "unlimited": True}, True),
        ({"hasCredits": True, "balance": "0"}, False),
        ({"hasCredits": True, "balance": "-1"}, False),
        ({"hasCredits": True, "balance": "NaN"}, False),
        ({"hasCredits": True, "balance": "Infinity"}, False),
        ({"hasCredits": True, "balance": "bad"}, False),
        ({"hasCredits": False, "balance": "10"}, False),
        ({}, False),
    ],
)
def test_credit_availability_requires_confirmed_usable_balance(credits, available):
    snapshot = quota(100)
    snapshot["rateLimits"]["credits"] = credits
    assert credits_available(snapshot) is available
    assert funding_status(snapshot, True)["passed"] is available
    if not available:
        with pytest.raises(FundingRequired):
            require_subscription(snapshot, True)


def test_credit_status_uses_applicable_bucket_and_allows_unknown_subscription():
    snapshot = quota(100)
    snapshot["rateLimits"].pop("primary")
    assert funding_status(snapshot, True)["passed"]
    snapshot["rateLimitsByLimitId"] = {"codex": {"credits": {"hasCredits": False}}}
    assert not funding_status(snapshot, True)["passed"]
    assert not funding_status({}, True)["passed"]
