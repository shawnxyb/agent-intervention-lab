"""Verify the real host sandbox and mount-free Worker containers, without inference."""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, configuration, docker_client, image_locks, save_json
from intervention.codex import AppServer
from intervention.protocol import PROTOCOL, canonical_hash, implementation_manifest
from intervention.sandbox import ContainerWorkspace


async def main() -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/isolation" / stamp
    server = AppServer(output)
    client = docker_client()
    workers = []
    results = {
        "protocol_id": PROTOCOL,
        "implementation_sha256": canonical_hash(implementation_manifest(ROOT)),
    }
    try:
        await server.start()
        with tempfile.TemporaryDirectory(prefix="intervention-private-") as private:
            outside = Path(private) / "sentinel"
            outside.write_text("PRIVATE_SENTINEL")
            inside = Path(server.host_directory.name) / "sentinel"
            inside.write_text("ALLOWED_SENTINEL")
            targets = {
                "outside_canary": outside,
                "repository": ROOT / "README.md",
                "answers": ROOT / "vendor",
                "other_runs": ROOT / "artifacts",
                "evaluator_cache": ROOT / ".cache/evaluator",
                "auth_store": Path.home() / ".codex/auth.json",
                "docker_socket": Path(f"/run/user/{__import__('os').getuid()}")
                / "academic-writing-docker/docker.sock",
            }
            # Only read synthetic sentinel content. For sensitive paths test
            # accessibility and fail if readable; never print their contents.
            command = "cat " + shlex.quote(str(inside))
            for key, target in targets.items():
                command += f"; if test -r {shlex.quote(str(target))}; then echo LEAK_{key}; fi"
            probe = await server.request(
                "command/exec",
                {
                    "command": ["/bin/sh", "-c", command],
                    "cwd": server.host_directory.name,
                    "permissionProfile": "experiment-host",
                    "timeoutMs": 10000,
                },
            )
            results["host_sandbox"] = probe
            if probe["exitCode"] != 0 or probe["stdout"] != "ALLOWED_SENTINEL":
                raise RuntimeError(f"Host sandbox did not isolate reads: {probe}")
        task = next(t for t in configuration()["tasks"] if t["name"] == "click")
        image = image_locks()["click"]["reference"]
        for role in ("worker_a", "worker_b"):
            worker = ContainerWorkspace(client, image, stamp, role)
            workers.append(worker)
            meta = worker.prepare(task["base_commit"])
            worker.put_files({f"workspace/repo/{role}.sentinel": role.encode()})
            results[role] = meta
        for i, worker in enumerate(workers):
            other = workers[1 - i].role
            result = worker.execute(
                "python - <<'PY'\nimport os, socket\n"
                f"assert not os.path.exists('/workspace/repo/{other}.sentinel')\n"
                f"assert not os.path.exists({str(ROOT)!r})\n"
                "assert not os.path.exists('/var/run/docker.sock')\n"
                "assert not os.path.exists('/root/.codex/auth.json')\n"
                "s=socket.socket(); s.settimeout(0.2)\n"
                "assert s.connect_ex(('1.1.1.1',443)) != 0\n"
                "print('ISOLATED')\nPY"
            )
            results[worker.role]["probe"] = result
            if result["exit_code"] != 0 or result["output"].strip() != "ISOLATED":
                raise RuntimeError(f"Container isolation failed: {result}")
        results.update(passed=True, model_inference_invoked=False)
    except Exception as error:
        results.update(passed=False, error=str(error), model_inference_invoked=False)
        raise
    finally:
        for worker in workers:
            worker.close()
        await server.close()
        client.close()
        save_json(output / "summary.json", results)
        save_json(
            ROOT / "artifacts/isolation/latest.json",
            {
                "path": str(output / "summary.json"),
                "passed": results.get("passed", False),
                "protocol_id": PROTOCOL,
                "implementation_sha256": results["implementation_sha256"],
            },
        )
        print(json.dumps({"output": str(output), "passed": results.get("passed", False)}))


if __name__ == "__main__":
    asyncio.run(main())
