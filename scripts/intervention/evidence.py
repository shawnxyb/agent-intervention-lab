"""Read only Git blobs and validate public-test targets, never model-supplied shell."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath


def public_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("Expected a repository-relative public path")
    if value in {"", "."} and allow_root:
        return ""
    path = PurePosixPath(value)
    forbidden = {".git", "feature.patch", "tests.patch", "combined.patch"}
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(part in forbidden for part in path.parts)
        or value.startswith("-")
    ):
        raise ValueError("Path is outside the public repository view")
    return str(path)


def files(workspace, tree: str) -> dict:
    output = workspace.checked(f"git ls-tree -rz --full-tree {shlex.quote(tree)}")
    entries = {}
    for record in output.split("\x00"):
        if not record:
            continue
        header, path = record.split("\t", 1)
        mode, kind, oid = header.split()
        try:
            public_path(path)
        except ValueError:
            continue
        # Git symlinks and submodules are never followed, including in candidate trees.
        if kind == "blob" and mode in {"100644", "100755"}:
            entries[path] = oid
    return entries


def inspect(workspace, tree: str, args: dict) -> dict:
    operation = args["operation"]
    path = public_path(args["path"], allow_root=operation != "read")
    entries = files(workspace, tree)
    selected = {
        p: oid for p, oid in entries.items() if not path or p == path or p.startswith(path + "/")
    }
    if operation == "list":
        return {"paths": sorted(selected)}
    if operation == "read":
        if path not in selected:
            raise ValueError("Not a regular file in this public snapshot")
        content = workspace.checked(f"git cat-file blob {selected[path]}")
        return {"path": path, "blob": selected[path], "content": content}
    if operation != "search" or not isinstance(args.get("query"), str) or not args["query"]:
        raise ValueError("Search requires a nonempty literal query")
    # One Git search, rather than a container round-trip per file. Filter results
    # against regular public blobs so links and benchmark patch artifacts stay excluded.
    command = (
        "git grep -n -I -F -e "
        + shlex.quote(args["query"])
        + " "
        + shlex.quote(tree)
        + " -- "
        + shlex.quote(path or ".")
    )
    found = workspace.execute(command)
    if found["exit_code"] not in {0, 1} or found.get("truncated"):
        raise ValueError("Search could not return complete evidence; narrow the repository path")
    matches = []
    for line in found["output"].splitlines():
        prefix, _, payload = line.partition(":")
        if prefix != tree:
            continue
        name, _, remainder = payload.partition(":")
        number, _, text = remainder.partition(":")
        if name in selected and number.isdecimal():
            matches.append({"path": name, "line": int(number), "text": text})
    return {"matches": matches}


def public_test_targets(workspace, tree: str, targets: list, language: str = "python") -> list[str]:
    if not isinstance(targets, list) or not targets:
        raise ValueError("Specify public pytest paths, not shell commands or pytest flags")
    entries = files(workspace, tree)
    if language == "go":
        return go_test_targets(entries, targets)
    result = []
    for target in targets:
        if not isinstance(target, str):
            raise TypeError("Test targets must be strings")
        filename, *selector = target.split("::")
        path = public_path(filename)
        parts = PurePosixPath(path).parts
        is_test = any(p in {"tests", "test", "testing"} for p in parts) or (
            parts[-1].startswith("test_") and parts[-1].endswith(".py")
        )
        exists = path in entries or any(p.startswith(path + "/") for p in entries)
        if not is_test or not exists or (selector and path not in entries):
            raise ValueError(
                "Only tracked public/self-authored test paths in this snapshot are allowed"
            )
        result.append(target)
    return result


def go_test_targets(entries: dict, targets: list) -> list[str]:
    """Accept tracked Go test files/packages, then run complete packages safely."""
    result = []
    for target in targets:
        if not isinstance(target, str):
            raise TypeError("Test targets must be strings")
        recursive = target.endswith("/...")
        value = target[:-4] if recursive else target
        path = public_path(value, allow_root=True)
        if path.endswith("_test.go") and path in entries:
            directory = str(PurePosixPath(path).parent)
            path = "" if directory == "." else directory
        elif path and path in entries:
            raise ValueError("Go public validation requires test files or package directories")
        prefix = path + "/" if path else ""
        tests = [p for p in entries if p.startswith(prefix) and p.endswith("_test.go")]
        if not recursive:
            tests = [p for p in tests if "/" not in p[len(prefix) :]]
        if not tests:
            raise ValueError("No tracked public Go tests in this package target")
        package = "./" + path if path else "."
        if recursive:
            package += "/..."
        if package not in result:
            result.append(package)
    return result
