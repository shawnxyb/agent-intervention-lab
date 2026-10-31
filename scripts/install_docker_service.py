"""Register a dedicated user service; never modifies the system Docker service."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "academic-writing-docker.service"
MARKER = "# Managed by the academic-writing experiment repository."


def main() -> None:
    unit = Path.home() / ".config/systemd/user" / SERVICE
    if unit.exists() and not unit.read_text().startswith(MARKER):
        raise RuntimeError(f"Refusing to replace unrelated unit: {unit}")
    # systemd specifier expansion is separate from shell quoting.
    start = str(ROOT / "scripts/start_docker.sh").replace("%", "%%").replace('"', '\\"')
    content = f'''{MARKER}
[Unit]
Description=Rootless Docker for academic-writing experiments
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash "{start}"
Restart=no
TimeoutStartSec=120
TimeoutStopSec=60
KillMode=mixed
Delegate=yes
LimitNOFILE=1048576
TasksMax=infinity

[Install]
WantedBy=default.target
'''
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    # Start on demand, without enabling automatic startup for this mounted drive.
    subprocess.run(["systemctl", "--user", "start", SERVICE], check=True)
    print(f"Registered and started {SERVICE}")


if __name__ == "__main__":
    main()
