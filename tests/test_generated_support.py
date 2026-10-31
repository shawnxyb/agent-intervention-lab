"""Real local Git cleanup and snapshot checks; no containers or model calls."""

import subprocess
from types import SimpleNamespace

import pytest
from intervention import sandbox


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    class LocalWorkspace(sandbox.ContainerWorkspace):
        def __init__(self):
            self.root = tmp_path
            self.external = tmp_path.parent / (tmp_path.name + "-support")
            self.external.mkdir()
            self.image = "pinned-image"
            self.fail_baseline = False
            self.container = SimpleNamespace(
                reload=lambda: None,
                attrs={"Mounts": [], "HostConfig": {"NetworkMode": "none"}},
            )

        def execute(self, command, seconds=None, limit=16000):
            if command == "python -m pip list --format=json --disable-pip-version-check":
                return {
                    "exit_code": 0,
                    "output": '[{"name":"fixed","version":"1"}]',
                    "truncated": False,
                }
            if command == "python -m pip freeze --all":
                return {"exit_code": 0, "output": "fixed==1\n", "truncated": False}
            if command.startswith(("python -c", "python -m pytest")):
                return {
                    "exit_code": int(self.fail_baseline),
                    "output": "baseline",
                    "truncated": False,
                }
            command = command.replace("/tmp/base-files", str(self.external / "base-files"))
            command = command.replace(
                "/usr/local/bin/runner.sh", str(self.external / "absent-runner")
            )
            command = command.replace(
                "/tmp/experiment-snapshot.patch", str(tmp_path / ".git/snapshot.patch")
            )
            result = subprocess.run(
                command, shell=True, cwd=tmp_path, capture_output=True, text=True, check=False
            )
            return {
                "exit_code": result.returncode,
                "output": result.stdout + result.stderr,
                "truncated": False,
            }

        def read_file(self, path):
            if path == "/tmp/experiment-snapshot.patch":
                return (tmp_path / ".git/snapshot.patch").read_bytes()
            return (tmp_path / path.removeprefix("/workspace/repo/")).read_bytes()

        def put_files(self, files):
            for path, data in files.items():
                (tmp_path / path.removeprefix("workspace/repo/")).write_bytes(data)

    ws = LocalWorkspace()
    ws.checked("git init -qb main\ngit config user.name Test\ngit config user.email test@localhost")
    (tmp_path / "outlines").mkdir()
    (tmp_path / "outlines/__init__.py").write_text("# base\n")
    (tmp_path / ".gitignore").write_text("outlines/_version.py\n")
    ws.checked("git add .\ngit commit -qm base")
    base = ws.checked("git rev-parse HEAD").strip()
    monkeypatch.setattr(
        sandbox,
        "GENERATED_SUPPORT",
        {
            base: {
                "path": "outlines/_version.py",
                "module": "outlines",
                "baseline_tests": "tests/test_public.py",
            }
        },
    )
    (tmp_path / "outlines/_version.py").write_text('__version__ = "trusted-generated-version"\n')
    return ws, base


def test_clean_restores_exact_generated_bytes_without_candidate_diff(workspace):
    ws, base = workspace
    expected = (ws.root / "outlines/_version.py").read_bytes()
    meta = ws.prepare(base)
    assert ws.read_file("/workspace/repo/outlines/_version.py") == expected
    assert not ws.snapshot()
    assert meta["empty_initial_snapshot"] and meta["dependencies_unchanged"]
    assert meta["generated_support"][0]["origin"] == "pinned_image_before_cleanup"
    ws.checked("git clean -fd")  # Reusable integration cleanup must preserve support.
    assert ws.read_file("/workspace/repo/outlines/_version.py") == expected
    (ws.root / "outlines/__init__.py").write_text("# candidate\n")
    patch = ws.snapshot()
    assert b"candidate" in patch and b"_version.py" not in patch


def test_modified_or_force_tracked_support_is_rejected(workspace):
    ws, base = workspace
    ws.prepare(base)
    path = ws.root / "outlines/_version.py"
    original = path.read_bytes()
    path.write_text("changed")
    with pytest.raises(RuntimeError, match="support was modified"):
        ws.snapshot()
    path.write_bytes(original)
    ws.checked("git add -f outlines/_version.py")
    with pytest.raises(RuntimeError, match="Container setup failed"):
        ws.snapshot()


def test_missing_support_or_failed_baseline_blocks_preparation(workspace):
    ws, base = workspace
    ws.fail_baseline = True
    with pytest.raises(RuntimeError, match="Baseline environment smoke failed"):
        ws.prepare(base)


def test_missing_image_support_is_not_fabricated(workspace):
    ws, base = workspace
    (ws.root / "outlines/_version.py").unlink()
    with pytest.raises(RuntimeError, match="Container setup failed"):
        ws.prepare(base)
