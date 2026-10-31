#!/usr/bin/env bash
set -euo pipefail
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$EXPERIMENT_ROOT/.tools/docker/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
EXPERIMENT_RUNTIME="${XDG_RUNTIME_DIR:?User runtime directory is required}/academic-writing-docker"
EXPERIMENT_DOCKER_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/academic-writing-experiment/docker"
mkdir -p "$EXPERIMENT_RUNTIME" "$EXPERIMENT_DOCKER_DATA"
export DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR="$EXPERIMENT_RUNTIME/rootlesskit"
export DOCKERD_ROOTLESS_ROOTLESSKIT_NET=gvisor-tap-vsock
export DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=builtin
exec dockerd-rootless.sh \
  --config-file "$EXPERIMENT_ROOT/configs/docker-daemon.json" \
  --host "unix://$EXPERIMENT_RUNTIME/docker.sock" \
  --data-root "$EXPERIMENT_DOCKER_DATA" \
  --exec-root "$EXPERIMENT_RUNTIME/exec" \
  --pidfile "$EXPERIMENT_RUNTIME/docker.pid" \
  --storage-driver overlay2
