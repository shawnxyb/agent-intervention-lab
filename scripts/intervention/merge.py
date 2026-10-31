"""Strict three-way integration; never substitute a lead-only candidate."""

from __future__ import annotations

import hashlib


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def merge_snapshots(workspace, patch_a: bytes, patch_b: bytes) -> dict:
    workspace.checked(f"git reset --hard {workspace.base}\ngit clean -fd")
    workspace.put_files({"tmp/worker-a.patch": patch_a, "tmp/worker-b.patch": patch_b})
    # An empty patch is a valid no-change contribution.
    first = workspace.execute(
        "if test -s /tmp/worker-a.patch; then git apply --index /tmp/worker-a.patch; fi"
    )
    if first["exit_code"] != 0:
        raise RuntimeError(f"Worker A patch not applicable to base: {first}")
    workspace.checked("git -c core.hooksPath=/dev/null commit --allow-empty -qm worker-a")
    second = workspace.execute(
        "if test -s /tmp/worker-b.patch; then git apply --3way --index /tmp/worker-b.patch; fi"
    )
    unmerged = workspace.checked("git ls-files -u")
    if second["exit_code"] != 0 and not unmerged:
        raise RuntimeError(f"Patch integration error, not a content conflict: {second}")
    return {
        "merge_ok": second["exit_code"] == 0 and not unmerged,
        "a_sha256": digest(patch_a),
        "b_sha256": digest(patch_b),
        "merge_output": second["output"],
        "unmerged_index": unmerged,
        "snapshot_pair": digest(patch_a + b"\0" + patch_b),
    }
