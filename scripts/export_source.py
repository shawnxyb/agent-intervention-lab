"""Export a code-only tree without local results, credentials or Git history."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selected_files(root: Path) -> list[Path]:
    files = set()
    for folder in ["scripts", "tests", "configs", ".agents/skills", "prompts"]:
        files.update(
            p
            for p in (root / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
    files.update((root / "docs/github").glob("*.md"))
    files.update((root / "docs/environment-dependencies").glob("*.freeze.txt"))
    files.update((root / "batches/evaluation-repairs").glob("*.py"))
    for name in [
        "README.md",
        ".gitignore",
        ".gitattributes",
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "env.sh",
        "setup_env.sh",
    ]:
        files.add(root / name)
    return sorted(files)


def export(output: Path) -> dict:
    if output.exists():
        raise ValueError("Refusing to overwrite an existing export")
    source = selected_files(ROOT)
    sensitive = re.compile(
        r"sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    )
    for path in source:
        if path.is_symlink():
            raise ValueError(f"Do not export symlinks: {path.relative_to(ROOT)}")
        if sensitive.search(path.read_text()):
            raise ValueError(f"Possible credential in {path.relative_to(ROOT)}")
    output.mkdir(parents=True)
    records = {}
    for path in source:
        relative = path.relative_to(ROOT)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        records[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    report = {"purpose": "code_only_github_export", "files": records, "git_history_included": False}
    (output / "SOURCE_MANIFEST.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = export(args.output.resolve())
    print(json.dumps({"files": len(report["files"]), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
