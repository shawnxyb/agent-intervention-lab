import subprocess

from intervention.merge import merge_snapshots


class LocalFixture:
    def __init__(self, root):
        self.root = root
        self.checked(
            "git init -q -b main\ngit config user.name Test\ngit config user.email test@localhost"
        )
        (root / "one.py").write_text("value = 1\n")
        (root / "two.py").write_text("value = 1\n")
        self.checked("git add .\ngit commit -qm base")
        self.base = self.checked("git rev-parse HEAD").strip()

    def execute(self, command):
        for name in ("worker-a.patch", "worker-b.patch"):
            command = command.replace("/tmp/" + name, str(self.root.parent / name))
        p = subprocess.run(
            ["bash", "-c", command], cwd=self.root, capture_output=True, text=True, check=False
        )
        return {"exit_code": p.returncode, "output": p.stdout + p.stderr}

    def checked(self, command):
        result = self.execute(command)
        assert result["exit_code"] == 0, result
        return result["output"]

    def put_files(self, files):
        for name, content in files.items():
            (self.root.parent / name.split("/")[-1]).write_bytes(content)

    def patch(self, name, content):
        (self.root / name).write_text(content)
        patch = self.checked("git diff --binary").encode()
        self.checked("git reset --hard HEAD")
        return patch


def test_strict_merge_retains_both_changes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = LocalFixture(root)
    a = ws.patch("one.py", "value = 2\n")
    b = ws.patch("two.py", "value = 3\n")
    assert merge_snapshots(ws, a, b)["merge_ok"]
    assert (root / "one.py").read_text() == "value = 2\n"
    assert (root / "two.py").read_text() == "value = 3\n"


def test_conflict_is_preserved_not_replaced_by_lead_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = LocalFixture(root)
    a = ws.patch("one.py", "value = 2\n")
    b = ws.patch("one.py", "value = 3\n")
    result = merge_snapshots(ws, a, b)
    assert not result["merge_ok"]
    assert result["unmerged_index"]
    assert "<<<<<<<" in (root / "one.py").read_text()


def test_empty_contributions_are_valid(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = LocalFixture(root)
    assert merge_snapshots(ws, b"", b"")["merge_ok"]
