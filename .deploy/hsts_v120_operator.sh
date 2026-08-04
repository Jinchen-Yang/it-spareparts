#!/usr/bin/env bash
# Operator-side SSH wrapper. An ambiguous SSH result is always reconciled read-only.
set -Eeuo pipefail
umask 077

OPERATOR_TEST_MODE=${HSTS_OPERATOR_TEST_MODE:-0}
if [ "$OPERATOR_TEST_MODE" = 1 ]; then
  [ "$EUID" -ne 0 ] || {
    printf 'FATAL: root may not enable HSTS operator test mode\n' >&2
    exit 1
  }
  COMMAND_DIR=${HSTS_OPERATOR_COMMAND_DIR:?}
else
  COMMAND_DIR=
fi
readonly OPERATOR_TEST_MODE COMMAND_DIR
readonly REMOTE_ROOT=/var/lib/it-spareparts-release-control/current/hsts-v120-root.sh
export PATH="${COMMAND_DIR:+$COMMAND_DIR:}/usr/local/bin:/usr/bin:/bin"
unset CDPATH ENV BASH_ENV

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

remote_action() {
  local ssh_target=$1
  local action=$2
  local target=$3
  local generation=$4
  local edge_generation=$5
  timeout --kill-after=5s 30s \
    ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -- "$ssh_target" \
    sudo -n "$REMOTE_ROOT" "$action" "$target" "$generation" \
    "$edge_generation" </dev/null
}

resume_exact_transition() {
  local ssh_target=$1 target=$2 generation=$3 edge_generation=$4
  local action=$5 expected=$6 output status action_status
  set +e
  remote_action "$ssh_target" "$action" "$target" "$generation" \
    "$edge_generation" >/dev/null
  action_status=$?
  output=$(
    remote_action "$ssh_target" inspect "$target" "$generation" \
      "$edge_generation"
  )
  status=$?
  set -e
  [ "$status" -eq 0 ] && [ "$output" = "$expected" ] || {
    printf 'RECONCILED resume-%s-unconfirmed action_status=%s inspect_status=%s\n' \
      "$action" "$action_status" "$status"
    return 69
  }
  printf 'RECONCILED %s resumed-%s\n' "$output" "$action"
}

reconcile() {
  local ssh_target=$1
  local target=$2
  local generation=$3
  local edge_generation=$4
  local context=$5
  local output
  local status
  set +e
  output=$(
    remote_action "$ssh_target" inspect "$target" "$generation" \
      "$edge_generation"
  )
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    printf 'RECONCILED unreachable status=%s\n' "$status"
    return 69
  fi
  case "$output" in
    exact-promoted)
      if [ "$context" = promote ] || [ "$context" = reconcile ]; then
        printf 'RECONCILED exact-promoted continue-verification\n'
        return 0
      fi
      printf 'RECONCILED exact-promoted unexpected-for-%s\n' "$context"
      return 76
      ;;
    exact-pre)
      if [ "$context" = prepare ]; then
        printf 'RECONCILED exact-pre prepare-complete\n'
        return 0
      fi
      if [ "$context" = reconcile ]; then
        printf 'RECONCILED exact-pre observed\n'
        return 0
      fi
      printf 'RECONCILED exact-pre retry-safe\n'
      return 75
      ;;
    exact-rolled-back)
      if [ "$context" = rollback ]; then
        printf 'RECONCILED exact-rolled-back rollback-complete\n'
        return 0
      fi
      if [ "$context" = reconcile ]; then
        printf 'RECONCILED exact-rolled-back observed\n'
        return 0
      fi
      printf 'RECONCILED exact-rolled-back already-rolled-back\n'
      return 76
      ;;
    exact-promote-pending)
      [ "$context" = promote ] || {
        printf 'RECONCILED exact-promote-pending unexpected-for-%s\n' \
          "$context"
        return 76
      }
      resume_exact_transition "$ssh_target" "$target" "$generation" \
        "$edge_generation" promote exact-promoted
      ;;
    exact-rollback-pending)
      [ "$context" = rollback ] || {
        printf 'RECONCILED exact-rollback-pending unexpected-for-%s\n' \
          "$context"
        return 76
      }
      resume_exact_transition "$ssh_target" "$target" "$generation" \
        "$edge_generation" rollback exact-rolled-back
      ;;
    divergent-or-unknown)
      printf 'RECONCILED divergent-or-unknown manual-stop\n'
      return 78
      ;;
    *)
      printf 'RECONCILED unreachable status=%s\n' "$status"
      return 69
      ;;
  esac
}

[ "$#" -eq 5 ] \
  || fatal "usage: hsts_v120_operator.sh <prepare|promote|rollback|reconcile> <ssh target> <target SHA> <generation> <edge generation>"
ACTION=$1
SSH_TARGET=$2
TARGET_COMMIT=$3
GENERATION=$4
EDGE_GENERATION=$5
[[ "$ACTION" =~ ^(prepare|promote|rollback|reconcile)$ ]] \
  || fatal "invalid action"
[[ "$SSH_TARGET" =~ ^[A-Za-z0-9._-]+$ ]] || fatal "invalid SSH target"
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[[ "$GENERATION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]] \
  || fatal "invalid HSTS generation"
[[ "$EDGE_GENERATION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]] \
  || fatal "invalid edge generation"

if [ "$ACTION" != reconcile ]; then
  set +e
  remote_action "$SSH_TARGET" "$ACTION" "$TARGET_COMMIT" "$GENERATION" \
    "$EDGE_GENERATION" \
    >/dev/null
  set -e
fi
reconcile "$SSH_TARGET" "$TARGET_COMMIT" "$GENERATION" \
  "$EDGE_GENERATION" "$ACTION"
