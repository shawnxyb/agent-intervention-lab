"""Posthoc public rechecks retain the original seal instead of adopting today's contract."""

import asyncio

import pytest
from revalidate_public import verify_archived


def test_archived_verification_reads_without_rewriting_and_rejects_tampering(run_factory):
    run, _, _ = run_factory("single")
    path = asyncio.run(run.execute())
    before = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
    summary = verify_archived(path)
    assert summary["status"] == "sealed"
    assert before == {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
    (path / "p0.patch").write_bytes(b"modified")
    with pytest.raises(ValueError, match="Archived evidence changed"):
        verify_archived(path)
