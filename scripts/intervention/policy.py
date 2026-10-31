"""Deterministic, unbounded supervision policy; no inference or coding tools."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Event:
    issue_id: str
    source: str
    kind: str
    summary: str
    evidence: str
    snapshot: str
    attempts: list[str] = field(default_factory=list)
    resolved: bool = False

    @property
    def revision(self) -> str:
        data = [self.kind, self.evidence, self.snapshot, sorted(set(self.attempts)), self.resolved]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    @property
    def eligible(self) -> bool:
        if self.resolved or not self.evidence.strip() or not self.snapshot:
            return False
        return self.kind in {"contract_change", "merge_conflict", "cross_task_blocker"}


class Policy:
    def __init__(self, name: str, interval_seconds: int = 60, clock=time.monotonic):
        if name not in {"static", "periodic", "adaptive"} or interval_seconds != 60:
            raise ValueError("Unknown policy or changed periodic interval")
        self.name, self.interval, self.clock = name, interval_seconds, clock
        self.started = None
        self.checks, self.last_tick = 0, 0
        self.events, self.handled, self.calls = {}, set(), set()
        self.skipped_ticks = []

    def start(self, started: float | None = None) -> None:
        if self.started is not None:
            raise ValueError("Development clock is already running")
        self.started = self.clock() if started is None else started

    def tool_completed(self, role: str, call_id: str) -> bool:
        key = (role, call_id)
        if key in self.calls:
            return False
        self.calls.add(key)
        return True

    def report(self, event: Event) -> bool:
        key = (event.source, event.issue_id)
        old = self.events.get(key)
        if old is not None and old.revision == event.revision:
            return False
        self.events[key] = event
        return True

    def poll(self, development_active: bool = True, busy: bool = False) -> dict | None:
        if not development_active or self.name == "static":
            return None
        cause = None
        if self.name == "periodic":
            if self.started is None:
                raise ValueError("Start the development clock before polling")
            tick = max(0, int((self.clock() - self.started) // self.interval))
            if tick <= self.last_tick:
                return None
            if tick > self.last_tick + 1:
                self.skipped_ticks.append(
                    {"first": self.last_tick + 1, "last": tick - 1, "reason": "missed"}
                )
            self.last_tick = tick
            if busy:
                self.skipped_ticks.append({"first": tick, "last": tick, "reason": "busy"})
                return None
            cause = f"elapsed_seconds:{tick * self.interval}"
        elif not busy:
            # Retain the latest revision per issue while busy; recheck resolution now.
            eligible = [
                e
                for e in self.events.values()
                if e.eligible and (e.source, e.issue_id, e.revision) not in self.handled
            ]
            if eligible:
                cause = f"event:{eligible[0].source}:{eligible[0].issue_id}"
        if cause is None:
            return None
        self.checks += 1
        events = list(self.events.values())
        self.handled.update((e.source, e.issue_id, e.revision) for e in events)
        return {"check": self.checks, "cause": cause, "events": [asdict(e) for e in events]}
