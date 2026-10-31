"""Check account/protocol readiness without creating a thread or invoking a model."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from intervention.funding import experiment_start_status, subscription_status

ROOT = Path(__file__).resolve().parents[1]


def model_available(models: list[dict], model: str, effort: str) -> bool:
    return any(
        row.get("model") == model and effort in row.get("supported_efforts", []) for row in models
    )


def main() -> None:
    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    report = {"version": version, "model_inference_invoked": False, "thread_created": False}
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=ROOT,
        bufsize=0,
    )
    assert process.stdin is not None and process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b""

    def send(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    def request(identifier: int, method: str, params: dict) -> dict:
        nonlocal pending
        send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == identifier:
                    if "error" in message:
                        # Do not print account or token payloads from protocol responses.
                        raise RuntimeError(f"{method} returned an error")
                    return message.get("result", {})
            if process.poll() is not None:
                raise RuntimeError("Codex app-server exited before completing the check")
            if selector.select(timeout=0.5):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app-server closed its output")
                pending += chunk
        raise TimeoutError(f"Timed out waiting for {method}")

    try:
        request(
            1,
            "initialize",
            {"clientInfo": {"name": "manager_intervention_env_check", "version": "0.1.0"}},
        )
        send({"method": "initialized"})
        account = request(2, "account/read", {"refreshToken": False})
        account_data = account.get("account") or {}
        report["account_type"] = account_data.get("type")
        report["models"] = []
        cursor, seen, identifier = None, set(), 3
        while True:
            params = {"includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            models = request(identifier, "model/list", params)
            identifier += 1
            report["models"].extend(
                {
                    "id": item.get("id"),
                    "model": item.get("model"),
                    "supported_efforts": [
                        e.get("reasoningEffort") for e in item.get("supportedReasoningEfforts", [])
                    ],
                }
                for item in models.get("data", [])
            )
            cursor = models.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("Model catalog pagination repeated a cursor")
            seen.add(cursor)
        config = json.loads((ROOT / "configs/pilot.json").read_text())
        report["target_model"] = config["model"]
        report["target_effort"] = config["reasoning_effort"]
        report["target_model_available"] = model_available(
            report["models"], config["model"], config["reasoning_effort"]
        )
        limits = request(identifier, "account/rateLimits/read", {})
        status = subscription_status(limits)
        bucket = (
            (limits.get("rateLimitsByLimitId") or {}).get("codex") or limits.get("rateLimits") or {}
        )
        report["subscription_status"] = status
        report["experiment_start_quota"] = experiment_start_status(
            limits, config["funding"]["allow_existing_credits"]
        )
        report["quota_used_percent"] = {
            key: (bucket.get(key) or {}).get("usedPercent") if status != "unknown" else None
            for key in ("primary", "secondary")
        }
        report["checked_at"] = datetime.now(UTC).isoformat()
        report["ready"] = report["account_type"] == "chatgpt" and report["target_model_available"]
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    path = ROOT / "artifacts/environment/codex.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
