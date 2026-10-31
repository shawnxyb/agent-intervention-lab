#!/usr/bin/env bash
# One-time administrator step required by Ubuntu's user-namespace policy.
# Only adds the named profile for this project's RootlessKit executable.
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then
  printf '%s\n' 'Run this script with sudo in your own terminal.' >&2
  exit 1
fi
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EXPERIMENT_ROOTLESSKIT="$EXPERIMENT_ROOT/.tools/docker/bin/rootlesskit"
EXPERIMENT_PROFILE=/etc/apparmor.d/academic-writing-rootlesskit
EXPERIMENT_MARKER='# Managed by the academic-writing experiment repository.'
if [[ ! -x "$EXPERIMENT_ROOTLESSKIT" ]]; then
  printf '%s\n' 'Install the project tools with scripts/bootstrap.py first.' >&2
  exit 1
fi
if [[ -f "$EXPERIMENT_PROFILE" ]] && ! head -n 1 "$EXPERIMENT_PROFILE" | grep -Fxq "$EXPERIMENT_MARKER"; then
  printf '%s\n' 'Refusing to replace an unrelated AppArmor profile.' >&2
  exit 1
fi
case "$EXPERIMENT_ROOTLESSKIT" in
  *'"'*|*'\\'*|*$'\n'*) printf '%s\n' 'Unsupported characters in project path.' >&2; exit 1 ;;
esac
cat > "$EXPERIMENT_PROFILE" <<EOF
$EXPERIMENT_MARKER
abi <abi/4.0>,
include <tunables/global>

"$EXPERIMENT_ROOTLESSKIT" flags=(unconfined) {
  userns,
}
EOF
chmod 0644 "$EXPERIMENT_PROFILE"
apparmor_parser -r "$EXPERIMENT_PROFILE"
printf '%s\n' 'Installed and loaded the project-specific RootlessKit profile.'
printf '%s\n' 'The global AppArmor and user-namespace settings were not changed.'
