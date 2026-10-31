"""Install pinned, project-local tools and evaluator source without starting experiments."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"
DOWNLOADS = [
    (
        "docker-29.9.1.tgz",
        "https://download.docker.com/linux/static/stable/x86_64/docker-29.9.1.tgz",
        "c158ecc55adb3afb01bbd3539ed3b997d20c26dbdc418080506cc1bf5243ff57",
    ),
    (
        "docker-rootless-extras-29.9.1.tgz",
        "https://download.docker.com/linux/static/stable/x86_64/docker-rootless-extras-29.9.1.tgz",
        "c87668896a3fced2e715324e9fb6a0634aba3e5d8408a471f6bc23dc476662af",
    ),
    (
        "cooperbench-63b9d44.tar.gz",
        f"https://codeload.github.com/cooperbench/CooperBench/tar.gz/{REVISION}",
        "f2af4c7d721987e008e01a03a1f69500a448f7c273cc2bd91e6e093f09c6a8e6",
    ),
]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    cache = ROOT / ".cache/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    bin_dir = ROOT / ".tools/docker/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for filename, url, expected in DOWNLOADS:
        archive = cache / filename
        if not archive.exists():
            partial = archive.with_suffix(archive.suffix + ".part")
            with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out)
            partial.replace(archive)
        actual = digest(archive)
        if actual != expected:
            raise RuntimeError(f"Checksum mismatch for {filename}: {actual}")
        if filename.startswith("cooperbench"):
            destination = ROOT / "vendor/CooperBench"
            marker = destination / ".source-revision"
            if destination.exists():
                if not marker.exists() or marker.read_text().strip() != REVISION:
                    raise RuntimeError(f"Refusing to overwrite unrecognized source: {destination}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(archive) as source:
                    source.extractall(destination.parent, filter="data")
                (destination.parent / f"CooperBench-{REVISION}").rename(destination)
                marker.write_text(REVISION + "\n")
            # A revision marker alone does not detect edited evaluator files.
            with tarfile.open(archive) as source:
                prefix = f"CooperBench-{REVISION}/"
                for member in source.getmembers():
                    if not member.isfile():
                        continue
                    relative = member.name.removeprefix(prefix)
                    installed = destination / relative
                    stream = source.extractfile(member)
                    assert stream is not None
                    expected_file = hashlib.file_digest(stream, "sha256").hexdigest()
                    if not installed.is_file() or digest(installed) != expected_file:
                        raise RuntimeError(f"Pinned upstream source was modified: {relative}")
        else:
            extracted = ROOT / ".cache/unpacked" / filename.removesuffix(".tgz")
            extracted.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive) as source:
                source.extractall(extracted, filter="data")
            for source in extracted.glob("*/*"):
                if source.is_file():
                    target = bin_dir / source.name
                    if not target.exists() or digest(target) != digest(source):
                        shutil.copy2(source, target)
                    target.chmod(0o755)
        records.append({"file": filename, "url": url, "sha256": actual})
        print(f"Verified and installed: {filename}", flush=True)
    manifest = ROOT / "artifacts/environment/downloads.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
