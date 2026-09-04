#!/bin/sh
set -eu

CONFIG=""
TARGET_PORT=""
PUBLIC_HEALTH_URL=""
NGINX_BIN="nginx"
SYSTEMCTL_BIN="systemctl"
CURL_BIN="curl"

usage() {
    cat <<'EOF'
Usage: switch-nginx-upstream.sh --config PATH --target-port PORT \
    --public-health-url URL [--nginx-bin PATH] [--systemctl-bin PATH] \
    [--curl-bin PATH]

Validates a loopback QBot4K web target, switches every generated nginx
proxy_pass to that port, reloads nginx, and rolls back if public readiness fails.
EOF
}

fail() {
    printf 'switch-nginx-upstream.sh: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config) CONFIG=${2-}; shift ;;
        --target-port) TARGET_PORT=${2-}; shift ;;
        --public-health-url) PUBLIC_HEALTH_URL=${2-}; shift ;;
        --nginx-bin) NGINX_BIN=${2-}; shift ;;
        --systemctl-bin) SYSTEMCTL_BIN=${2-}; shift ;;
        --curl-bin) CURL_BIN=${2-}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
    shift
done

[ -n "$CONFIG" ] || fail "--config is required"
[ -f "$CONFIG" ] || fail "nginx config does not exist: $CONFIG"
[ -n "$PUBLIC_HEALTH_URL" ] || fail "--public-health-url is required"
case "$TARGET_PORT" in
    ''|*[!0-9]*) fail "--target-port must be an integer" ;;
esac
[ "$TARGET_PORT" -ge 1 ] && [ "$TARGET_PORT" -le 65535 ] \
    || fail "--target-port must be between 1 and 65535"
START_MILLISECONDS=$(date +%s%3N)

elapsed_milliseconds() {
    now=$(date +%s%3N)
    printf '%s' "$((now - START_MILLISECONDS))"
}

CURRENT_PORTS=$(sed -n \
    's|.*proxy_pass http://127\.0\.0\.1:\([0-9][0-9]*\);.*|\1|p' \
    "$CONFIG" | sort -u)
[ -n "$CURRENT_PORTS" ] || fail "config has no generated loopback proxy_pass"
CURRENT_COUNT=$(printf '%s\n' "$CURRENT_PORTS" | wc -l | tr -d ' ')
[ "$CURRENT_COUNT" -eq 1 ] \
    || fail "config must use exactly one current loopback upstream port"
CURRENT_PORT=$CURRENT_PORTS
[ "$CURRENT_PORT" != "$TARGET_PORT" ] \
    || fail "target port is already active"

TARGET_HEALTH_URL="http://127.0.0.1:${TARGET_PORT}/health/ready"
"$CURL_BIN" --fail --silent --show-error --max-time 10 \
    "$TARGET_HEALTH_URL" >/dev/null \
    || fail "target readiness check failed: $TARGET_HEALTH_URL"

CONFIG_DIR=$(dirname "$CONFIG")
BACKUP=$(mktemp "${CONFIG_DIR}/.qbot4k-nginx-backup.XXXXXX")
CANDIDATE=$(mktemp "${CONFIG_DIR}/.qbot4k-nginx-candidate.XXXXXX")
cleanup() {
    rm -f "$BACKUP" "$CANDIDATE"
}
trap cleanup EXIT HUP INT TERM
cp -p "$CONFIG" "$BACKUP"
cp -p "$CONFIG" "$CANDIDATE"
sed -i \
    "s|proxy_pass http://127\\.0\\.0\\.1:${CURRENT_PORT};|proxy_pass http://127.0.0.1:${TARGET_PORT};|g" \
    "$CANDIDATE"
mv -f "$CANDIDATE" "$CONFIG"

rollback() {
    cp -p "$BACKUP" "$CONFIG"
    "$NGINX_BIN" -t >/dev/null 2>&1 || true
    "$SYSTEMCTL_BIN" reload nginx >/dev/null 2>&1 || true
    printf '{"result":"rolled_back","previous_port":%s,"attempted_port":%s,"duration_ms":%s}\n' \
        "$CURRENT_PORT" "$TARGET_PORT" "$(elapsed_milliseconds)" >&2
}

if ! "$NGINX_BIN" -t; then
    rollback
    fail "nginx validation failed; restored port $CURRENT_PORT"
fi
if ! "$SYSTEMCTL_BIN" reload nginx; then
    rollback
    fail "nginx reload failed; restored port $CURRENT_PORT"
fi
if ! "$CURL_BIN" --fail --silent --show-error --max-time 10 \
    "$PUBLIC_HEALTH_URL" >/dev/null; then
    rollback
    fail "public readiness failed; restored port $CURRENT_PORT"
fi

printf '{"result":"switched","previous_port":%s,"active_port":%s,"public_health_url":"%s","duration_ms":%s}\n' \
    "$CURRENT_PORT" "$TARGET_PORT" "$PUBLIC_HEALTH_URL" \
    "$(elapsed_milliseconds)"
