#!/usr/bin/env bash
# Verified official binary used by local/CI integration tests. No running service
# is installed: fixtures launch a loopback process and always terminate it.
set -euo pipefail
target_dir="${1:?usage: install_test_cerbos.sh TARGET_DIRECTORY}"
mkdir -p "$target_dir"
archive="$target_dir/cerbos_0.57.0_Linux_x86_64.tar.gz"
curl --fail --location --retry 3 --output "$archive" \
  https://github.com/cerbos/cerbos/releases/download/v0.57.0/cerbos_0.57.0_Linux_x86_64.tar.gz
printf '%s  %s\n' baec1b2b29a5f748528c16aa3cf1191a09ba6207add4e8e0d752e07cbffbf659 "$archive" | sha256sum --check --status
tar -xzf "$archive" -C "$target_dir" cerbos
"$target_dir/cerbos" --version
