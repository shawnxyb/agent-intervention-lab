#!/usr/bin/env bash
# Installs project tools and selected benchmark images; never invokes an LLM.
set -euo pipefail
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$EXPERIMENT_ROOT"
EXPERIMENT_VALIDATE=0
if (( $# > 1 )); then
  printf '%s\n' 'Usage: bash setup_env.sh [--validate]' >&2
  exit 2
fi
case "${1:-}" in
  --validate) EXPERIMENT_VALIDATE=1 ;;
  '') ;;
  *) printf '%s\n' 'Usage: bash setup_env.sh [--validate]' >&2; exit 2 ;;
esac
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  printf '%s\n' 'This locked environment requires Linux x86_64.' >&2
  exit 1
fi
for EXPERIMENT_COMMAND in uv git codex systemctl newuidmap newgidmap; do
  command -v "$EXPERIMENT_COMMAND" >/dev/null || {
    printf 'Required command not found: %s\n' "$EXPERIMENT_COMMAND" >&2
    exit 1
  }
done
uv sync --frozen
uv run --frozen python scripts/bootstrap.py
source ./env.sh
uv run --frozen python scripts/install_docker_service.py
EXPERIMENT_DOCKER_READY=0
for ((EXPERIMENT_ATTEMPT=0; EXPERIMENT_ATTEMPT<30; EXPERIMENT_ATTEMPT++)); do
  if docker info >/dev/null 2>&1; then
    EXPERIMENT_DOCKER_READY=1
    break
  fi
  if ! systemctl --user is-active --quiet academic-writing-docker.service; then
    break
  fi
  sleep 1
done
if [[ "$EXPERIMENT_DOCKER_READY" != 1 ]]; then
  printf '%s\n' 'Rootless Docker is not ready. Inspect: journalctl --user -u academic-writing-docker.service -n 30' >&2
  printf '%s\n' 'If Ubuntu AppArmor blocks RootlessKit, run the documented one-time scripts/configure_rootless_apparmor.sh with sudo.' >&2
  exit 1
fi
uv run --frozen python scripts/prepare_images.py --tasks dirty_equals click outlines
if [[ "$EXPERIMENT_VALIDATE" == 1 ]]; then
  uv run --frozen python scripts/validate_evaluators.py
  uv run --frozen python scripts/check_environment.py --require-controls
else
  uv run --frozen python scripts/check_environment.py
fi
