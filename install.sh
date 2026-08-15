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
UNIT_NAME="qbot4k.service"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}"
UNIT_DROPIN_DIR="/etc/systemd/system/${UNIT_NAME}.d"
UNIT_DROPIN_FILE="${UNIT_DROPIN_DIR}/zz-qbot4k-installer.conf"
POLKIT_RULE_DIR="/etc/polkit-1/rules.d"
POLKIT_RULE_FILE="${POLKIT_RULE_DIR}/49-qbot4k.rules"
START_SERVICE=1
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RELEASE_DIR=""
RELEASE_ACTIVATED=0
SERVICE_WAS_STOPPED=0
INSTALL_COMPLETE=0

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
    if [ "$SERVICE_WAS_STOPPED" -eq 1 ] && [ "$RELEASE_ACTIVATED" -eq 0 ] && [ "$INSTALL_COMPLETE" -eq 0 ]; then
        systemctl start "$UNIT_NAME" >/dev/null 2>&1 || true
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
[ -f "$SOURCE_DIR/requirements.txt" ] || fail "requirements.txt was not found beside install.sh"
[ -f "$SOURCE_DIR/deploy/qbot4k.service" ] || fail "deploy/qbot4k.service is missing"
[ -f "$SOURCE_DIR/deploy/zz-qbot4k-installer.conf" ] || fail "deployment override is missing"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is required"
command -v useradd >/dev/null 2>&1 || fail "useradd is required"
command -v groupadd >/dev/null 2>&1 || fail "groupadd is required"
command -v usermod >/dev/null 2>&1 || fail "usermod is required"
command -v getent >/dev/null 2>&1 || fail "getent is required"
command -v runuser >/dev/null 2>&1 || fail "runuser is required"

PYTHON_BIN=${QBOT_PYTHON_BIN:-}
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN=$(command -v "$candidate")
            break
        fi
    done
fi
[ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ] || fail "Python 3.11 or newer is required"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 3.11 or newer is required"
"$PYTHON_BIN" -m venv --help >/dev/null 2>&1 \
    || fail "Python venv support is required (install your distribution's python3-venv package)"

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
    "$COMPAT_DATA_DIR" "$STATE_DIR" "$BACKUP_DIR"

RELEASE_DIR=$(mktemp -d "${RELEASE_ROOT}/release.XXXXXXXX")
install -d -m 0755 "$RELEASE_DIR/src" "$RELEASE_DIR/docs" "$RELEASE_DIR/deploy"
cp -a "$SOURCE_DIR/src/." "$RELEASE_DIR/src/"
cp -a "$SOURCE_DIR/docs/." "$RELEASE_DIR/docs/"
cp -a "$SOURCE_DIR/deploy/." "$RELEASE_DIR/deploy/"
install -m 0644 "$SOURCE_DIR/README.md" "$RELEASE_DIR/README.md"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$RELEASE_DIR/requirements.txt"
install -m 0644 "$SOURCE_DIR/.env.example" "$RELEASE_DIR/.env.example"
find "$RELEASE_DIR" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$RELEASE_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

"$PYTHON_BIN" -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check \
    --requirement "$RELEASE_DIR/requirements.txt"

chown -R root:root "$RELEASE_DIR"
find "$RELEASE_DIR" -type d -exec chmod 0755 {} +
find "$RELEASE_DIR" -type f -exec chmod a-w {} +

if [ ! -e "$CONFIG_FILE" ]; then
    SESSION_SECRET=$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')
    INGEST_TOKEN=$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')
    umask 027
    cat >"$CONFIG_FILE" <<EOF
# Managed by the operator. install.sh preserves this file during upgrades.
QBOT_DATABASE_PATH=${STATE_DIR}/qbot4k.sqlite3
QBOT_BACKUP_DIR=${BACKUP_DIR}
QBOT_RAW_ARCHIVE_DIR=${STATE_DIR}/raw-events
QBOT_DEFAULT_COMMUNITY_SLUG=default

# Safe initial profile. Add web after configuring Discord OAuth below.
QBOT_ENABLED_SERVICES=jobs,analysis
QBOT_DASHBOARD_HOST=127.0.0.1
QBOT_DASHBOARD_PORT=8080
QBOT_LOG_LEVEL=INFO
QBOT_DASHBOARD_SESSION_SECRET=${SESSION_SECRET}
QBOT_INGEST_API_TOKEN=${INGEST_TOKEN}

# Required before adding web to QBOT_ENABLED_SERVICES.
QBOT_DISCORD_OAUTH_CLIENT_ID=
QBOT_DISCORD_OAUTH_CLIENT_SECRET=
QBOT_DISCORD_OAUTH_REDIRECT_URI=
QBOT_OPERATOR_GUILD_IDS=

# Optional collection services and credentials.
QBOT_DISCORD_BOT_TOKEN=
QBOT_DISCORD_GUILD_IDS=
QBOT_DISCORD_ALLOW_BOT_MESSAGES=false
QBOT_TWITCH_BOT_TOKEN=
QBOT_TWITCH_REFRESH_TOKEN=
QBOT_TWITCH_CLIENT_ID=
QBOT_TWITCH_CLIENT_SECRET=
QBOT_TWITCH_CHANNELS=its_not_qwerty
QBOT_TWITCH_EVENTSUB_SECRET=
QBOT_TWITCH_EVENTSUB_CALLBACK_URL=

# Safety and scheduling.
QBOT_MODERATION_SHADOW_MODE=true
QBOT_MAINTENANCE_INTERVAL_SECONDS=60
QBOT_ANALYTICS_INTERVAL_SECONDS=300
QBOT_BACKUP_INTERVAL_SECONDS=3600
QBOT_BACKUP_RETENTION_COUNT=48
QBOT_MESSAGE_RETENTION_DAYS=30
QBOT_AUDIT_RETENTION_DAYS=90
EOF
    chown root:"$SERVICE_GROUP" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    printf 'Created %s with generated secrets and a non-web initial profile.\n' "$CONFIG_FILE"
else
    printf 'Preserved existing configuration: %s\n' "$CONFIG_FILE"
fi

run_as_service() {
    command_name=$1
    (
        cd "$STATE_DIR"
        runuser -u "$SERVICE_USER" -- \
            "$RELEASE_DIR/.venv/bin/python" \
            "$RELEASE_DIR/src/__main__.py" \
            --env-file "$CONFIG_FILE" "$command_name"
    )
}
run_as_service check-config

if systemctl is-active --quiet "$UNIT_NAME"; then
    systemctl stop "$UNIT_NAME"
    SERVICE_WAS_STOPPED=1
fi
run_as_service init-db

if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
    fail "$CURRENT_LINK exists and is not a symbolic link; move it aside before installing"
fi
NEXT_LINK="${INSTALL_ROOT}/.current.$$"
ln -s "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
RELEASE_ACTIVATED=1

install -m 0644 "$RELEASE_DIR/deploy/qbot4k.service" "$UNIT_FILE"
install -d -m 0755 -o root -g root "$UNIT_DROPIN_DIR"
install -m 0644 \
    "$RELEASE_DIR/deploy/zz-qbot4k-installer.conf" "$UNIT_DROPIN_FILE"
if [ -d "$POLKIT_RULE_DIR" ]; then
    install -m 0644 "$RELEASE_DIR/deploy/49-qbot4k.rules" "$POLKIT_RULE_FILE"
fi
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null

if [ "$START_SERVICE" -eq 1 ]; then
    systemctl restart "$UNIT_NAME"
    systemctl is-active --quiet "$UNIT_NAME" \
        || fail "$UNIT_NAME did not become active; inspect: journalctl -u $UNIT_NAME -n 100"
    SERVICE_WAS_STOPPED=0
    printf 'Installed and started %s.\n' "$UNIT_NAME"
else
    printf 'Installed and enabled %s without starting it.\n' "$UNIT_NAME"
fi

INSTALL_COMPLETE=1

printf 'Active release: %s\n' "$RELEASE_DIR"
printf 'Configuration: %s\n' "$CONFIG_FILE"
printf 'Status: systemctl status %s\n' "$UNIT_NAME"
