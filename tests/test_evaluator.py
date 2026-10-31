import json

import pytest
from evaluate_candidates import verify_sealed
from intervention.merge import digest


def test_unfinished_run_cannot_be_evaluated(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"status": "infrastructure_interrupted"}))
    with pytest.raises(ValueError, match="not sealed"):
        verify_sealed(tmp_path)


def test_conflicted_p0_needs_no_fake_patch_or_fake_test_result(tmp_path):
    patch = b"candidate"
    (tmp_path / "p1.patch").write_bytes(patch)
    summary = {"status": "sealed", "p0": {"merge_ok": False}, "p1": {"sha256": digest(patch)}}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="Legacy"):
        verify_sealed(tmp_path)
    assert not (tmp_path / "p0.patch").exists()
