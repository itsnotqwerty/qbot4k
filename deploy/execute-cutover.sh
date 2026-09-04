#!/bin/sh
set -eu

DRAIN_COMMAND=""
OWNERSHIP_COMMAND=""
PREFLIGHT_COMMAND=""
SWITCH_COMMAND=""
VERIFY_COMMAND=""

usage() {
    cat <<'EOF'
Usage: execute-cutover.sh --drain-command PATH --ownership-command PATH \
    --preflight-command PATH --switch-command PATH --verify-command PATH

Runs an audited cutover sequence in dependency order and stops at the first
failed stage. Each command must be an executable file with its own arguments
and environment already configured.
EOF
}

fail() {
    printf 'execute-cutover.sh: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --drain-command) DRAIN_COMMAND=${2-}; shift ;;
        --ownership-command) OWNERSHIP_COMMAND=${2-}; shift ;;
        --preflight-command) PREFLIGHT_COMMAND=${2-}; shift ;;
        --switch-command) SWITCH_COMMAND=${2-}; shift ;;
        --verify-command) VERIFY_COMMAND=${2-}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
    shift
done

for command_path in \
    "$DRAIN_COMMAND" \
    "$OWNERSHIP_COMMAND" \
    "$PREFLIGHT_COMMAND" \
    "$SWITCH_COMMAND" \
    "$VERIFY_COMMAND"
do
    [ -n "$command_path" ] || fail "all five command paths are required"
    [ -x "$command_path" ] || fail "command is not executable: $command_path"
done

START_MILLISECONDS=$(date +%s%3N)
run_stage() {
    stage=$1
    command_path=$2
    "$command_path" || fail "$stage stage failed"
}

run_stage drain "$DRAIN_COMMAND"
run_stage ownership "$OWNERSHIP_COMMAND"
run_stage preflight "$PREFLIGHT_COMMAND"
run_stage switch "$SWITCH_COMMAND"
run_stage verify "$VERIFY_COMMAND"

END_MILLISECONDS=$(date +%s%3N)
printf '{"result":"cutover_complete","duration_ms":%s}\n' \
    "$((END_MILLISECONDS - START_MILLISECONDS))"