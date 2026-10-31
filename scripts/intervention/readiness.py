"""Current-source acceptance evidence; no models, containers or legacy prerequisites."""

from __future__ import annotations

import json
from pathlib import Path

from intervention.protocol import PROTOCOL, canonical_hash, implementation_manifest


def acceptance_status(root: Path) -> dict:
    fingerprint = canonical_hash(implementation_manifest(root))

    def inspect(relative: str, runtime: bool = False) -> dict:
        path = root / relative
        try:
            marker = json.loads(path.read_text())
            evidence_path = Path(marker["path"])
            if not evidence_path.is_absolute():
                evidence_path = root / evidence_path
            if not evidence_path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Evidence path leaves this project")
            evidence = json.loads(evidence_path.read_text())
            for record in (marker, evidence):
                if record.get("passed") is not True:
                    raise ValueError("Acceptance did not pass")
                if record.get("protocol_id") != PROTOCOL:
                    raise ValueError("Acceptance belongs to another protocol")
                if record.get("implementation_sha256") != fingerprint:
                    raise ValueError("Acceptance does not match current sources")
                if runtime and record.get("coordination_protocol_verified") is not True:
                    raise ValueError("Current coordination protocol is unverified")
                if runtime and record.get("funding_admission") != "live_funding_availability":
                    raise ValueError(
                        "Runtime acceptance must use current subscription-or-Credits admission"
                    )
            return {"passed": True, "path": relative}
        except (OSError, ValueError, TypeError, KeyError) as error:
            return {"passed": False, "path": relative, "reason": str(error)}

    return {
        "isolation": inspect("artifacts/isolation/latest.json"),
        "runtime": inspect(f"artifacts/runtime/{PROTOCOL}.json", runtime=True),
    }
