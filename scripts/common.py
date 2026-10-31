"""Shared paths and the dedicated rootless Docker connection."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[1]


def configuration() -> dict:
    return tomllib.loads((ROOT / "configs/environment.toml").read_text())


def image_locks() -> dict[str, dict]:
    records = json.loads((ROOT / "configs/images.lock.json").read_text())
    return {record["name"]: record for record in records}


def docker_client(timeout: int | None = 600) -> docker.DockerClient:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    socket = runtime / "academic-writing-docker/docker.sock"
    # An unrelated DOCKER_HOST must never redirect this project's checks.
    return docker.DockerClient(base_url=f"unix://{socket}", timeout=timeout)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evaluator_cache(task_name: str, lock: dict) -> Path:
    key = lock["digest"].removeprefix("sha256:")[:16]
    return ROOT / ".cache/evaluator" / f"{task_name}-{key}" / "uv"


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
