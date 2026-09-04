#!/bin/sh
set -eu

# QBot4K system installer for Linux hosts running systemd.
# Run this script from the root of an extracted QBot4K release.

SERVICE_USER="qbot4k"
SERVICE_GROUP="qbot4k"
INSTALL_ROOT="/opt/qbot4k"
RELEASE_ROOT="${INSTALL_ROOT}/releases"
CURRENT_LINK="${INSTALL_ROOT}/current"
COMPAT_DATA_DIR="${INSTALL_ROOT}/data"
CONFIG_DIR="/etc/qbot4k"
CONFIG_FILE="${CONFIG_DIR}/qbot4k.env"
STATE_DIR="/var/lib/qbot4k"
STATE_DATA_DIR="${STATE_DIR}/data"
BACKUP_DIR="/var/backups/qbot4k"
DENO_VERSION="2.9.4"
DENO_ROOT="${INSTALL_ROOT}/deno/${DENO_VERSION}"
DENO_BIN="${DENO_ROOT}/deno"
ALL_ROLES="web jobs analysis discord twitch"
POLKIT_RULE_DIR="/etc/polkit-1/rules.d"
POLKIT_RULE_FILE="${POLKIT_RULE_DIR}/49-qbot4k.rules"
START_SERVICE=1
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RELEASE_DIR=""
RELEASE_ACTIVATED=0
SERVICE_WAS_STOPPED=0
INSTALL_COMPLETE=0
PREVIOUS_RELEASE=""
ENABLED_ROLES=""

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--no-start]

Installs QBot4K under /opt/qbot4k, creates the qbot4k system account,
initializes persistent state, and enables qbot4k.service.

Options:
  --no-start  Register and enable the service without starting or restarting it
  -h, --help  Show this help
EOF
}

fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [ "$RELEASE_ACTIVATED" -eq 0 ] && [ -n "$RELEASE_DIR" ] && [ -d "$RELEASE_DIR" ]; then
        rm -rf -- "$RELEASE_DIR"
    fi
    if [ "$RELEASE_ACTIVATED" -eq 1 ] && [ "$INSTALL_COMPLETE" -eq 0 ] && [ -n "$PREVIOUS_RELEASE" ]; then
        ln -sfn "$PREVIOUS_RELEASE" "$CURRENT_LINK"
        for role in $ENABLED_ROLES; do
            systemctl restart "qbot4k-${role}.service" >/dev/null 2>&1 || true
        done
    elif [ "$SERVICE_WAS_STOPPED" -eq 1 ] && [ "$INSTALL_COMPLETE" -eq 0 ]; then
        for role in $ENABLED_ROLES; do
            systemctl start "qbot4k-${role}.service" >/dev/null 2>&1 || true
        done
    fi
}
trap cleanup EXIT HUP INT TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-start)
            START_SERVICE=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || fail "run as root (for example: sudo ./install.sh)"
[ -d "$SOURCE_DIR/src" ] || fail "src/ was not found beside install.sh"
[ -f "$SOURCE_DIR/deno.json" ] || fail "deno.json was not found beside install.sh"
[ -f "$SOURCE_DIR/runtime.ts" ] || fail "runtime.ts was not found beside install.sh"
[ -f "$SOURCE_DIR/backup_restore.ts" ] || fail "backup restore command is missing"
[ -f "$SOURCE_DIR/cutover_monitor.ts" ] || fail "cutover monitor command is missing"
[ -f "$SOURCE_DIR/cutover_preflight.ts" ] || fail "cutover preflight command is missing"
[ -d "$SOURCE_DIR/static" ] || fail "Fresh static assets were not found beside install.sh"
[ -f "$SOURCE_DIR/deploy/systemd.service.template" ] || fail "systemd service template is missing"
[ -x "$SOURCE_DIR/deploy/execute-cutover.sh" ] || fail "executable cutover sequence is missing"
[ -x "$SOURCE_DIR/deploy/switch-nginx-upstream.sh" ] || fail "executable nginx switch helper is missing"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is required"
command -v useradd >/dev/null 2>&1 || fail "useradd is required"
command -v groupadd >/dev/null 2>&1 || fail "groupadd is required"
command -v usermod >/dev/null 2>&1 || fail "usermod is required"
command -v getent >/dev/null 2>&1 || fail "getent is required"
command -v runuser >/dev/null 2>&1 || fail "runuser is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v unzip >/dev/null 2>&1 || fail "unzip is required"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"

if [ ! -x "$DENO_BIN" ]; then
    case "$(uname -m)" in
        x86_64) deno_arch="x86_64-unknown-linux-gnu" ;;
        aarch64|arm64) deno_arch="aarch64-unknown-linux-gnu" ;;
        *) fail "unsupported Deno architecture: $(uname -m)" ;;
    esac
    deno_archive=$(mktemp)
    curl --fail --location --silent --show-error \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}.zip" \
        --output "$deno_archive"
    install -d -m 0755 -o root -g root "$DENO_ROOT"
    unzip -q "$deno_archive" -d "$DENO_ROOT"
    rm -f "$deno_archive"
    chmod 0755 "$DENO_BIN"
fi
[ "$("$DENO_BIN" --version | sed -n '1s/^deno \([^ ]*\).*/\1/p')" = "$DENO_VERSION" ] \
    || fail "Deno ${DENO_VERSION} is required"

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
fi

if ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
    NOLOGIN_SHELL=$(command -v nologin || true)
    [ -n "$NOLOGIN_SHELL" ] || NOLOGIN_SHELL="/usr/sbin/nologin"
    useradd \
        --system \
        --gid "$SERVICE_GROUP" \
        --home-dir "$STATE_DIR" \
        --shell "$NOLOGIN_SHELL" \
        --comment "QBot4K service account" \
        "$SERVICE_USER"
fi

SERVICE_GID=$(getent group "$SERVICE_GROUP" | cut -d: -f3)
case " $(id -G "$SERVICE_USER") " in
    *" $SERVICE_GID "*) ;;
    *) usermod --append --groups "$SERVICE_GROUP" "$SERVICE_USER" ;;
esac

install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$RELEASE_ROOT"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$COMPAT_DATA_DIR" "$STATE_DIR" "$STATE_DATA_DIR" "$BACKUP_DIR"
install -d -m 0750 -o root -g "$SERVICE_GROUP" "$CONFIG_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" \
    "$COMPAT_DATA_DIR" "$STATE_DIR" "$BACKUP_DIR" "$CONFIG_DIR"

RELEASE_DIR=$(mktemp -d "${RELEASE_ROOT}/release.XXXXXXXX")
for directory in src docs deploy static components routes islands _fresh; do
    [ -d "$SOURCE_DIR/$directory" ] || continue
    cp -a "$SOURCE_DIR/$directory" "$RELEASE_DIR/$directory"
done
for file in deno.json deno.lock main.ts runtime.ts cli.ts client.ts database_transfer.ts backup_restore.ts cutover_monitor.ts cutover_preflight.ts vite.config.ts playwright.config.ts; do
    [ -f "$SOURCE_DIR/$file" ] && install -m 0644 "$SOURCE_DIR/$file" "$RELEASE_DIR/$file"
done
install -m 0644 "$SOURCE_DIR/README.md" "$RELEASE_DIR/README.md"
install -m 0644 "$SOURCE_DIR/.env.example" "$RELEASE_DIR/.env.example"

(cd "$RELEASE_DIR" && "$DENO_BIN" install --frozen)

chown -R root:root "$RELEASE_DIR"
find "$RELEASE_DIR" -type d -exec chmod 0755 {} +
find "$RELEASE_DIR" -type f -exec chmod a-w {} +

if [ ! -e "$CONFIG_FILE" ]; then
    SESSION_SECRET=$(openssl rand -hex 32)
    INGEST_TOKEN=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')
    umask 027
    cat >"$CONFIG_FILE" <<EOF
# Managed by the operator. install.sh preserves this file during upgrades.
QBOT_DATABASE_URL=postgresql://qbot4k@127.0.0.1/qbot4k
QBOT_BACKUP_DIR=${BACKUP_DIR}
QBOT_RAW_ARCHIVE_DIR=${STATE_DIR}/raw-events
QBOT_DEFAULT_COMMUNITY_SLUG=default

# Safe initial profile. Add web after configuring Discord OAuth below.
QBOT_ENABLED_SERVICES=jobs,analysis
QBOT_DASHBOARD_HOST=127.0.0.1
QBOT_DASHBOARD_PORT=8080
QBOT_WEB_READ_ONLY=false
QBOT_LOG_LEVEL=INFO
QBOT_DASHBOARD_SESSION_SECRET=${SESSION_SECRET}
QBOT_INGEST_API_TOKEN=${INGEST_TOKEN}

# Required before adding web to QBOT_ENABLED_SERVICES.
QBOT_DISCORD_OAUTH_CLIENT_ID=
QBOT_DISCORD_OAUTH_CLIENT_SECRET=
QBOT_DISCORD_OAUTH_REDIRECT_URI=
QBOT_OPERATOR_GUILD_IDS=
QBOT_LEGAL_ORGANIZATION_NAME=
QBOT_LEGAL_CONTACT_EMAIL=
QBOT_LEGAL_JURISDICTION=
QBOT_LEGAL_EFFECTIVE_DATE=

# Optional collection services and credentials.
QBOT_DISCORD_BOT_TOKEN=
QBOT_DISCORD_GUILD_IDS=
QBOT_TWITCH_BOT_TOKEN=
QBOT_TWITCH_REFRESH_TOKEN=
QBOT_TWITCH_CLIENT_ID=
QBOT_TWITCH_CLIENT_SECRET=
QBOT_TWITCH_CHANNELS=its_not_qwerty
QBOT_TWITCH_EVENTSUB_SECRET=
QBOT_TWITCH_EVENTSUB_CALLBACK_URL=

# Safety and scheduling.
QBOT_MAINTENANCE_INTERVAL_SECONDS=60
QBOT_ANALYTICS_INTERVAL_SECONDS=300
QBOT_BACKUP_INTERVAL_SECONDS=3600
QBOT_BACKUP_RETENTION_COUNT=48
QBOT_AUDIT_RETENTION_DAYS=90
EOF
    chown "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    printf 'Created %s with generated secrets and a non-web initial profile.\n' "$CONFIG_FILE"
else
    printf 'Preserved existing configuration: %s\n' "$CONFIG_FILE"
fi

# Keep runtime token rotation functional across upgrades.
chown "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

run_as_service() {
    command_name=$1
    (
        cd "$STATE_DIR"
        runuser -u "$SERVICE_USER" -- \
            "$DENO_BIN" task --config "$RELEASE_DIR/deno.json" \
            "$command_name" "--env-file=$CONFIG_FILE"
    )
}
run_as_service check-config

ENABLED_ROLES=$(sed -n 's/^[[:space:]]*QBOT_ENABLED_SERVICES=//p' "$CONFIG_FILE" | tail -n 1 | tr ',' ' ')
[ -n "$ENABLED_ROLES" ] || fail "QBOT_ENABLED_SERVICES must select at least one role"
WEB_PORT=$(sed -n 's/^[[:space:]]*QBOT_DASHBOARD_PORT=//p' "$CONFIG_FILE" | tail -n 1)
case "$WEB_PORT" in
    ''|*[!0-9]*) fail "QBOT_DASHBOARD_PORT must be an integer" ;;
esac
[ "$WEB_PORT" -ge 1 ] && [ "$WEB_PORT" -le 65535 ] \
    || fail "QBOT_DASHBOARD_PORT must be between 1 and 65535"
for role in $ENABLED_ROLES; do
    case " $ALL_ROLES " in
        *" $role "*) ;;
        *) fail "unsupported QBOT_ENABLED_SERVICES role: $role" ;;
    esac
done

for role in $ALL_ROLES; do
    if systemctl is-active --quiet "qbot4k-${role}.service"; then
        systemctl stop "qbot4k-${role}.service"
        SERVICE_WAS_STOPPED=1
    fi
done
run_as_service migrate

if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
    fail "$CURRENT_LINK exists and is not a symbolic link; move it aside before installing"
fi
[ -L "$CURRENT_LINK" ] && PREVIOUS_RELEASE=$(readlink -f "$CURRENT_LINK")
NEXT_LINK="${INSTALL_ROOT}/.current.$$"
ln -s "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
RELEASE_ACTIVATED=1

for role in $ALL_ROLES; do
    RENDERED_UNIT="${RELEASE_DIR}/qbot4k-${role}.service"
    if [ "$role" = web ]; then
        health_check="ExecStartPost=${DENO_BIN} eval --allow-net=127.0.0.1:${WEB_PORT} \"const r=await fetch('http://127.0.0.1:${WEB_PORT}/health/ready');if(!r.ok)Deno.exit(1)\""
    else
        health_check=""
    fi
    sed \
        -e 's|__DESCRIPTION__|QBot4K intelligence platform|g' \
        -e 's|__SERVICE_NAME__|qbot4k|g' \
        -e "s|__ROLE__|${role}|g" \
        -e 's|__APP_USER__|qbot4k|g' \
        -e 's|__APP_GROUP__|qbot4k|g' \
        -e 's|__APP_DIR__|/opt/qbot4k/current|g' \
        -e 's|__CONFIG_DIR__|/etc/qbot4k|g' \
        -e "s|__PORT__|${WEB_PORT}|g" \
        -e 's|__ENV_FILE__|/etc/qbot4k/qbot4k.env|g' \
        -e "s|__START_COMMAND__|${DENO_BIN} task --config /opt/qbot4k/current/deno.json role:${role}|g" \
        -e "s|__DENO__|${DENO_BIN}|g" \
        -e "s|__HEALTH_CHECK__|${health_check}|g" \
        "$RELEASE_DIR/deploy/systemd.service.template" >"$RENDERED_UNIT"
    install -m 0644 "$RENDERED_UNIT" "/etc/systemd/system/qbot4k-${role}.service"
done
if [ -d "$POLKIT_RULE_DIR" ]; then
    install -m 0644 "$RELEASE_DIR/deploy/49-qbot4k.rules" "$POLKIT_RULE_FILE"
fi
systemctl daemon-reload
for role in $ENABLED_ROLES; do
    systemctl enable "qbot4k-${role}.service" >/dev/null
done

if [ "$START_SERVICE" -eq 1 ]; then
    for role in $ENABLED_ROLES; do
        unit_name="qbot4k-${role}.service"
        systemctl restart "$unit_name"
        systemctl is-active --quiet "$unit_name" \
            || fail "$unit_name did not become active; inspect: journalctl -u $unit_name -n 100"
    done
    SERVICE_WAS_STOPPED=0
    printf 'Installed and started roles: %s.\n' "$ENABLED_ROLES"
else
    printf 'Installed and enabled roles without starting: %s.\n' "$ENABLED_ROLES"
fi

INSTALL_COMPLETE=1

printf 'Active release: %s\n' "$RELEASE_DIR"
printf 'Configuration: %s\n' "$CONFIG_FILE"
printf 'Status: systemctl status qbot4k-{%s}.service\n' "$(printf '%s' "$ENABLED_ROLES" | tr ' ' ',')"
