"""Disposable coding containers with no host mounts, network, or credentials."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
from pathlib import Path

from validate_evaluators import archive

# Build-generated support pinned to the upstream base, never candidate source.
GENERATED_SUPPORT = {
    "4dd7802a8a5021f31dac75bc652f88d410a28675": {
        "path": "outlines/_version.py",
        "module": "outlines",
        "baseline_tests": "tests/types/test_custom_types.py",
    },
}


class ContainerWorkspace:
    def __init__(self, client, image: str, run_id: str, role: str):
        self.client = client
        self.image = image
        self.role = role
        self.lock = threading.Lock()
        self.container = client.containers.create(
            image=image,
            entrypoint="/bin/sleep",
            command=["infinity"],
            working_dir="/workspace/repo",
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            pids_limit=256,
            environment={
                "UV_OFFLINE": "true",
                "PIP_NO_INDEX": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
            labels={
                "academic-writing.project": "manager-intervention",
                "academic-writing.run": run_id,
                "academic-writing.role": role,
            },
        )
        self.container.start()

    def execute(self, command: str, seconds: int | None = None, limit: int = 16000) -> dict:
        with self.lock:
            argv = ["/bin/bash", "--noprofile", "--norc", "-c", command]
            if seconds is not None:
                argv = ["timeout", "-s", "TERM", "-k", "5", str(seconds), *argv]
            result = self.container.exec_run(argv, workdir="/workspace/repo")
        output = result.output.decode("utf-8", errors="replace")
        return {
            "exit_code": result.exit_code,
            "output": output[:limit],
            "truncated": len(output) > limit,
        }

    def checked(self, command: str, seconds: int | None = None) -> str:
        result = self.execute(command, seconds, 2_000_000)
        if result["exit_code"] != 0 or result["truncated"]:
            raise RuntimeError(f"Container setup failed: {result}")
        return result["output"]

    def prepare(self, expected_base: str) -> dict:
        original = self.checked("git rev-parse HEAD").strip()
        if original != expected_base:
            raise RuntimeError("Task image base does not match pinned configuration")
        self.python = (
            "python3" if expected_base == "9436cc85db0d35be06e66b91b4f74bbc0295b07b" else "python"
        )
        packages_before = self.checked(f"{self.python} -m pip freeze --all")
        versions_before = self.installed_versions()
        support = GENERATED_SUPPORT.get(expected_base)
        self.generated_support = {}
        if support:
            path = support["path"]
            self.checked(
                f"test ! -L outlines && test -f {path} && test ! -L {path} "
                f'&& test -z "$(git ls-files -- {path})"'
            )
            self.generated_support[path] = self.read_file("/workspace/repo/" + path)
        # Keep only the tracked base tree, not later refs or unreachable objects
        # from the upstream clone. Existing site-packages remain image-pinned.
        self.checked("""set -e
git ls-files -z > /tmp/base-files
git clean -fdx
rm -rf .git
git init -q -b main
git config user.name Experiment
git config user.email experiment@localhost
git add -f --pathspec-from-file=/tmp/base-files --pathspec-file-nul
git -c core.hooksPath=/dev/null commit -qm base
git remote remove origin 2>/dev/null || true
rm -f /usr/local/bin/runner.sh /tmp/base-files
""")
        support_records = []
        for path, content in self.generated_support.items():
            self.put_files({"workspace/repo/" + path: content})
            self.checked(f"echo '/{path}' >> .git/info/exclude")
            support_records.append(
                {
                    "path": path,
                    "origin": "pinned_image_before_cleanup",
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        self.base = self.checked("git rev-parse HEAD").strip()
        startup = {}
        if support:
            startup["import"] = self.execute(
                "python -c 'import outlines; import outlines._version as v; print(v.__version__)'"
            )
            startup["public_baseline"] = self.execute(
                "python -m pytest -q -- " + support["baseline_tests"]
            )
            if any(r["exit_code"] != 0 or r["truncated"] for r in startup.values()):
                raise RuntimeError("Baseline environment smoke failed: " + json.dumps(startup))
        if self.snapshot():
            raise RuntimeError("Environment initialization introduced a candidate diff")
        packages = self.checked(f"{self.python} -m pip freeze --all")
        versions_after = self.installed_versions()
        if versions_after != versions_before:
            raise RuntimeError("Environment initialization changed installed dependencies")
        self.container.reload()
        if self.container.attrs.get("Mounts"):
            raise RuntimeError("Worker unexpectedly has mounted host data")
        return {
            "source_base": original,
            "local_base": self.base,
            "image": self.image,
            "packages": packages,
            "packages_before_cleanup": packages_before,
            "installed_versions": versions_after,
            "network": self.container.attrs["HostConfig"]["NetworkMode"],
            "mounts": [],
            "generated_support": support_records,
            "startup_checks": startup,
            "empty_initial_snapshot": True,
            "dependencies_unchanged": True,
        }

    def installed_versions(self) -> list:
        # Removing upstream Git remotes changes pip freeze's editable source notation,
        # not the installed versions. Compare distributions independently of Git refs.
        packages = json.loads(
            self.checked(
                f"{getattr(self, 'python', 'python')} -m pip list --format=json --disable-pip-version-check"
            )
        )
        return sorted({(p["name"].lower().replace("_", "-"), p["version"]) for p in packages})

    def put_files(self, files: dict[str, bytes]) -> None:
        if any(Path(p).is_absolute() or ".." in Path(p).parts for p in files):
            raise ValueError("Only relative payload paths allowed")
        self.container.put_archive("/", archive(files))

    def read_file(self, path: str) -> bytes:
        chunks, _ = self.container.get_archive(path)
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as tar:
            members = tar.getmembers()
            if len(members) != 1 or not members[0].isfile():
                raise ValueError("Expected one regular evidence file")
            return tar.extractfile(members[0]).read()

    def snapshot(self) -> bytes:
        for path, expected in getattr(self, "generated_support", {}).items():
            self.checked(
                f'test -f {path} && test ! -L {path} && test -z "$(git ls-files -- {path})"'
            )
            if self.read_file("/workspace/repo/" + path) != expected:
                raise RuntimeError("Generated environment support was modified: " + path)
        self.checked(
            "git -c core.hooksPath=/dev/null add -A\n"
            f"git diff --cached --binary {self.base} > /tmp/experiment-snapshot.patch"
        )
        return self.read_file("/tmp/experiment-snapshot.patch")

    def close(self) -> None:
        self.container.remove(force=True)
