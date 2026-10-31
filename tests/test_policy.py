from intervention.policy import Event, Policy


def issue(**changes):
    args = {
        "issue_id": "shared-interface",
        "source": "worker_a",
        "kind": "contract_change",
        "summary": "Signature changed",
        "evidence": "public-test.log: incompatible call",
        "snapshot": "sha1",
    }
    return Event(**(args | changes))


def test_three_policies_route_same_evidence_differently():
    policies = {name: Policy(name, clock=lambda: 60) for name in ("static", "periodic", "adaptive")}
    for policy in policies.values():
        policy.start(60)
        policy.tool_completed("worker_a", "a1")
        policy.report(issue())
    assert policies["static"].poll() is None
    assert policies["periodic"].poll() is None
    assert policies["adaptive"].poll()["cause"].startswith("event:")
    for policy in policies.values():
        policy.tool_completed("worker_b", "b1")
    assert policies["periodic"].poll() is None  # Tools never advance the clock.
    policies["periodic"].clock = lambda: 120
    assert policies["periodic"].poll()["cause"] == "elapsed_seconds:60"
    assert policies["adaptive"].poll() is None
    assert policies["static"].poll() is None


def test_rewording_and_duplicate_notifications_do_not_consume_budget():
    policy = Policy("adaptive")
    assert policy.tool_completed("worker_a", "call1")
    assert not policy.tool_completed("worker_a", "call1")
    policy.report(issue())
    assert policy.poll()
    assert not policy.report(issue(summary="Same problem in different words"))
    assert policy.poll() is None
    policy.report(issue(snapshot="sha2"))
    assert policy.poll()
    policy.report(issue(snapshot="sha3"))
    assert policy.poll()["check"] == 3
    assert len(policy.calls) == 1


def test_resolved_issue_and_ordinary_failure_do_not_trigger():
    policy = Policy("adaptive")
    policy.report(issue())
    policy.report(issue(resolved=True))
    assert policy.poll() is None
    policy.report(issue(kind="status", resolved=False))
    assert policy.poll() is None
    policy.report(issue(kind="cross_task_blocker", evidence="", attempts=[]))
    assert policy.poll() is None
    policy.report(issue(kind="cross_task_blocker", attempts=[]))
    assert policy.poll()


def test_no_forced_checkpoint_after_work_has_finished():
    policy = Policy("periodic", clock=lambda: 2000)
    policy.start(0)
    assert policy.poll(False) is None
    assert policy.poll(True)["check"] == 1
    assert policy.poll(True) is None


def test_periodic_boundary_busy_skip_and_no_check_count_cap():
    now = [0.0]
    policy = Policy("periodic", clock=lambda: now[0])
    policy.start()
    for moment in (0, 59.9):
        now[0] = moment
        assert policy.poll() is None
    now[0] = 60
    assert policy.poll()["check"] == 1
    now[0] = 119.9
    assert policy.poll() is None
    now[0] = 120
    assert policy.poll(busy=True) is None
    now[0] = 121
    assert policy.poll() is None  # No catch-up when the Manager becomes idle.
    now[0] = 180
    assert policy.poll()["check"] == 2
    now[0] = 240
    assert policy.poll()["check"] == 3
    now[0] = 1980
    assert policy.poll()["check"] == 4  # No 1800-second development limit.
    assert policy.poll() is None
    assert policy.skipped_ticks[0] == {"first": 2, "last": 2, "reason": "busy"}


def test_adaptive_busy_coalesces_latest_and_revalidates_resolution():
    policy = Policy("adaptive")
    policy.report(issue())
    assert policy.poll(busy=True) is None
    policy.report(issue(snapshot="new", resolved=True))
    assert policy.poll() is None
    policy.report(issue(snapshot="newest"))
    assert policy.poll(busy=True) is None
    trigger = policy.poll()
    assert trigger["events"][0]["snapshot"] == "newest"
    assert policy.poll() is None
