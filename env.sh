#!/usr/bin/env bash
# Source this file in the current shell. It does not start models or containers.
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export EXPERIMENT_ROOT
export PATH="$EXPERIMENT_ROOT/.venv/bin:$EXPERIMENT_ROOT/.tools/docker/bin:$PATH"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/academic-writing-docker/docker.sock"
export UV_LINK_MODE=copy
