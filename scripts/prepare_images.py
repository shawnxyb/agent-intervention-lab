"""Pull only the selected CooperBench images, by immutable platform digest."""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import docker
from common import ROOT, docker_client, image_locks, save_json


def main() -> None:
    locks = image_locks()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", choices=locks, default=list(locks))
    args = parser.parse_args()
    client = docker_client(timeout=1800)
    info = client.info()
    if "name=rootless" not in info["SecurityOptions"]:
        raise RuntimeError("Expected the dedicated rootless Docker daemon")
    storage = Path(info["DockerRootDir"])
    report = {"checked_at": datetime.now(UTC).isoformat(), "images": []}
    try:
        for name in args.tasks:
            lock = locks[name]
            reference = lock["reference"]
            try:
                image = client.images.get(reference)
                print(f"Already installed: {name}", flush=True)
            except docker.errors.ImageNotFound:
                # Allow headroom for unpacking and disposable test containers.
                required = lock["compressed_bytes"] * 3 + 2 * 1024**3
                free = shutil.disk_usage(storage).free
                if free < required:
                    raise RuntimeError(
                        f"Insufficient disk space for {name}: "
                        f"{free / 1024**3:.1f} GiB free, {required / 1024**3:.1f} GiB required"
                    )
                print(
                    f"Pulling {name} ({lock['compressed_bytes'] / 1024**2:.1f} MiB compressed)",
                    flush=True,
                )
                last_update = 0.0
                for event in client.api.pull(
                    reference, stream=True, decode=True, platform=lock["platform"]
                ):
                    if "error" in event:
                        raise RuntimeError(event["error"])
                    if time.monotonic() - last_update > 20:
                        print(f"  {name}: {event.get('status', 'working')}", flush=True)
                        last_update = time.monotonic()
                image = client.images.get(reference)
            if image.attrs["Os"] != "linux" or image.attrs["Architecture"] != "amd64":
                raise RuntimeError(f"Unexpected image platform for {name}")
            if reference not in image.attrs.get("RepoDigests", []):
                raise RuntimeError(f"Digest could not be confirmed for {name}")
            report["images"].append(
                {
                    "name": name,
                    "reference": reference,
                    "image_id": image.id,
                    "size_bytes": image.attrs["Size"],
                }
            )
            save_json(ROOT / "artifacts/environment/images.json", report)
            print(f"Ready: {name} ({image.attrs['Size'] / 1024**2:.1f} MiB unpacked)", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
