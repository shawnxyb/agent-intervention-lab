"""Startup checks reject stale/missing evidence without starting an experiment."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from check_codex import model_available
from intervention import controller
from intervention.protocol import PROTOCOL, canonical_hash, implementation_manifest
from intervention.readiness import acceptance_status


def evidence(root):
    fingerprint = canonical_hash(implementation_manifest(root))
    for category, filename in (
        ("isolation", "latest.json"),
        ("runtime", PROTOCOL + ".json"),
    ):
        folder = root / "artifacts" / category
        folder.mkdir(parents=True)
        summary = folder / "current/summary.json"
        summary.parent.mkdir()
        data = {
            "passed": True,
            "protocol_id": PROTOCOL,
            "implementation_sha256": fingerprint,
            "coordination_protocol_verified": True,
            "funding_admission": "live_funding_availability",
        }
        summary.write_text(json.dumps(data))
        (folder / filename).write_text(json.dumps({"path": str(summary), **data}))


def test_absent_live_acceptance_is_blocked_without_creating_run_or_container(tmp_path):
    status = acceptance_status(tmp_path)
    assert not any(check["passed"] for check in status.values())
    assert not list(tmp_path.iterdir())


def test_legacy_latest_runtime_cannot_substitute_for_current_protocol(tmp_path):
    folder = tmp_path / "artifacts/runtime"
    folder.mkdir(parents=True)
    (folder / "latest.json").write_text('{"passed":true}')
    assert not acceptance_status(tmp_path)["runtime"]["passed"]


def test_quota_admission_does_not_require_a_server_funding_marker(tmp_path):
    evidence(tmp_path)
    assert all(check["passed"] for check in acceptance_status(tmp_path).values())
    assert not (tmp_path / "artifacts/funding").exists()


@pytest.mark.parametrize(
    "mutation", ["source", "summary_protocol", "missing_summary", "outside_path"]
)
def test_live_evidence_must_match_current_sources_and_local_summary(tmp_path, mutation):
    evidence(tmp_path)
    assert all(c["passed"] for c in acceptance_status(tmp_path).values())
    if mutation == "source":
        directory = tmp_path / "scripts"
        directory.mkdir()
        (directory / "changed.py").write_text("changed")
    elif mutation == "summary_protocol":
        summary = tmp_path / "artifacts/runtime/current/summary.json"
        data = json.loads(summary.read_text())
        data["protocol_id"] = "obsolete"
        summary.write_text(json.dumps(data))
    elif mutation == "missing_summary":
        (tmp_path / "artifacts/runtime/current/summary.json").unlink()
    else:
        path = tmp_path / f"artifacts/runtime/{PROTOCOL}.json"
        data = json.loads(path.read_text())
        data["path"] = str(tmp_path.parent / "outside-summary.json")
        path.write_text(json.dumps(data))
    assert not acceptance_status(tmp_path)["runtime"]["passed"]


@pytest.mark.parametrize("record", ["marker", "summary"])
def test_runtime_acceptance_must_record_quota_admission(tmp_path, record):
    evidence(tmp_path)
    path = tmp_path / (
        f"artifacts/runtime/{PROTOCOL}.json"
        if record == "marker"
        else "artifacts/runtime/current/summary.json"
    )
    data = json.loads(path.read_text())
    data.pop("funding_admission")
    data["subscription_only_confirmed"] = True
    path.write_text(json.dumps(data))
    assert not acceptance_status(tmp_path)["runtime"]["passed"]


def test_qualification_blocks_before_run_construction_without_isolation(monkeypatch, tmp_path):
    import check_codex_runtime

    monkeypatch.setattr(check_codex_runtime, "ROOT", tmp_path)
    constructor = Mock()
    monkeypatch.setattr(check_codex_runtime, "RuntimeRun", constructor)
    with pytest.raises(RuntimeError, match="Isolation acceptance"):
        asyncio.run(check_codex_runtime.main())
    constructor.assert_not_called()
    marker = json.loads((tmp_path / f"artifacts/runtime/{PROTOCOL}.json").read_text())
    assert marker["model_inference_invoked"] is False
    assert marker["experiment_containers_started"] is False
    assert marker["passed"] is marker["coordination_protocol_verified"] is False
    assert marker["runs"] == []
    assert not (tmp_path / "runs").exists()


def test_qualification_direct_preflight_requires_isolation_but_not_prior_runtime(
    monkeypatch, tmp_path
):
    import check_codex_runtime

    monkeypatch.setattr(check_codex_runtime, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="Isolation acceptance"):
        check_codex_runtime.RuntimeRun.preflight(SimpleNamespace())
    evidence(tmp_path)
    (tmp_path / f"artifacts/runtime/{PROTOCOL}.json").unlink()
    check_codex_runtime.RuntimeRun.preflight(SimpleNamespace())


def test_controller_preflight_does_not_depend_on_deleted_legacy_files(monkeypatch, tmp_path):
    monkeypatch.setattr(controller, "ROOT", tmp_path)
    probe = SimpleNamespace(report={})
    with pytest.raises(RuntimeError, match="Live acceptance incomplete"):
        controller.Run.preflight(probe)
    evidence(tmp_path)
    controller.Run.preflight(probe)
    assert "runtime_acceptance_sha256" in probe.report
    assert "funding_acceptance_sha256" not in probe.report


@pytest.mark.parametrize(
    "models,expected",
    [
        ([{"model": "gpt-6-luna", "supported_efforts": ["high"]}], True),
        ([{"model": "gpt-6-luna", "supported_efforts": ["medium"]}], False),
        ([{"model": "gpt-6.1-sol", "supported_efforts": ["high"]}], False),
        ([{"model": "gpt-6-luna"}], False),
    ],
)
def test_exact_requested_model_and_effort_are_required(models, expected):
    assert model_available(models, "gpt-6-luna", "high") is expected


def test_failed_environment_refresh_does_not_reuse_stale_success(monkeypatch, tmp_path):
    import check_readiness
    from common import ROOT

    folder = tmp_path / "configs"
    folder.mkdir()
    (folder / "pilot.json").write_bytes((ROOT / "configs/pilot.json").read_bytes())
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "controller-validation.json").write_text(
        json.dumps(
            {
                "protocol_id": PROTOCOL,
                "implementation_sha256": canonical_hash(implementation_manifest(tmp_path)),
                "pytest": {"passed": 1, "failures": 0, "errors": 0},
            }
        )
    )
    folder = tmp_path / "artifacts/environment"
    folder.mkdir(parents=True)
    (folder / "status.json").write_text('{"checked_at":"2000-01-01", "ready":true}')
    monkeypatch.setattr(check_readiness, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_readiness.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1))
    )
    client = Mock()
    client.containers.list.return_value = []
    monkeypatch.setattr(check_readiness, "docker_client", Mock(return_value=client))
    monkeypatch.setattr("sys.argv", ["checker"])
    with pytest.raises(SystemExit):
        check_readiness.main()
    report = json.loads((tmp_path / "docs/start-readiness.json").read_text())
    assert not report["ready"] and not report["checks"]["environment"]["passed"]
    assert not (tmp_path / "runs").exists()
