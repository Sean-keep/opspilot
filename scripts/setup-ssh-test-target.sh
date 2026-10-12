#!/usr/bin/env bash
#
# Create a LOCAL, DISPOSABLE SSH target for OpsPilot integration tests.
#
# This is for a development machine only. It installs openssh-server, creates a
# dedicated non-root user `opstest`, restricts that user to a small sudo
# whitelist, and hands you a private key OpsPilot can use to connect.
#
# It NEVER touches production hosts. Remote execution tests must only ever hit
# this target (see docs/local-install.md and the project security model).
#
# Hard rules honoured here:
#   - the test user is NOT root and cannot become root freely
#   - sudo is a whitelist of argv-separated commands, not `sudo -s`
#   - nothing existing is wiped: if the user or the app roots already exist we
#     leave them alone rather than re-initialise them
#   - no container runtime is used or required
#
# Usage:
#   sudo bash scripts/setup-ssh-test-target.sh
#   sudo bash scripts/setup-ssh-test-target.sh --key-dir /etc/opspilot
#
set -euo pipefail

KEY_DIR=/etc/opspilot/ssh-test
TARGET_USER=opstest
APP_ROOT=/opt/opspilot/apps
TEST_ROOT=/opt/opspilot/test

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-dir) KEY_DIR="$2"; shift 2 ;;
    --user)    TARGET_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '[setup-ssh-test-target] %s\n' "$*"; }
fail() { printf '[setup-ssh-test-target] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "must run as root (sudo)"

# ---------------------------------------------------------------------------
# 1. openssh-server
# ---------------------------------------------------------------------------
if ! dpkg -s openssh-server >/dev/null 2>&1; then
  log "installing openssh-server"
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y openssh-server >/dev/null
fi

if ! systemctl is-active --quiet ssh; then
  log "starting ssh"
  systemctl enable --now ssh >/dev/null
fi
log "sshd active: $(systemctl is-active ssh)"

HOST_KEY=/etc/ssh/ssh_host_ed25519_key.pub
[[ -f $HOST_KEY ]] || fail "no ed25519 host key at $HOST_KEY"
log "host key fingerprint: $(ssh-keygen -lf "$HOST_KEY")"

# ---------------------------------------------------------------------------
# 2. test user (additive — never reset an existing account)
# ---------------------------------------------------------------------------
if id "$TARGET_USER" >/dev/null 2>&1; then
  log "user $TARGET_USER already exists — leaving it alone (no password reset)"
else
  log "creating user $TARGET_USER"
  useradd --create-home --shell /bin/bash "$TARGET_USER"
fi

# ---------------------------------------------------------------------------
# 3. application / test roots (additive — never wipe existing data)
#
# Owned by the test user itself, so every ordinary file operation under these
# two prefixes needs NO sudo at all. Root is only required for systemd unit
# files and service control — see step 5.
# ---------------------------------------------------------------------------
for dir in "$APP_ROOT" "$TEST_ROOT"; do
  if [[ -e $dir ]]; then
    log "$dir already exists — leaving contents alone"
  else
    mkdir -p "$dir"
    log "created $dir"
  fi
  chown "$TARGET_USER:$TARGET_USER" "$dir"
  chmod 0755 "$dir"
done

# ---------------------------------------------------------------------------
# 4. SSH key for OpsPilot -> opstest (key auth only, no password)
# ---------------------------------------------------------------------------
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

KEY_FILE="$KEY_DIR/opstest_ed25519"
if [[ -f $KEY_FILE ]]; then
  log "key $KEY_FILE already exists — keeping it"
else
  log "generating key $KEY_FILE"
  ssh-keygen -t ed25519 -N '' -C "opspilot-test-target-${TARGET_USER}" -f "$KEY_FILE" >/dev/null
fi
chmod 600 "$KEY_FILE"
chmod 644 "$KEY_FILE.pub"

AUTH_KEYS="/home/$TARGET_USER/.ssh/authorized_keys"
install -d -m 700 -o "$TARGET_USER" -g "$TARGET_USER" "/home/$TARGET_USER/.ssh"
if [[ -f $AUTH_KEYS ]] && grep -qF "$(cat "$KEY_FILE.pub")" "$AUTH_KEYS"; then
  log "authorized_keys already contains this key"
else
  install -m 600 -o "$TARGET_USER" -g "$TARGET_USER" /dev/null "$AUTH_KEYS.tmp"
  cat "$KEY_FILE.pub" >> "$AUTH_KEYS.tmp"
  # Keep any pre-existing keys; we only append.
  if [[ -f $AUTH_KEYS ]]; then cat "$AUTH_KEYS" >> "$AUTH_KEYS.tmp"; fi
  mv "$AUTH_KEYS.tmp" "$AUTH_KEYS"
  chown "$TARGET_USER:$TARGET_USER" "$AUTH_KEYS"
  chmod 600 "$AUTH_KEYS"
  log "installed authorized key"
fi

# ---------------------------------------------------------------------------
# 5. Restricted sudo whitelist
#
# argv-separated, NOPASSWD, specific binaries only. `sudo -s`, `bash`, and
# arbitrary shell are deliberately NOT granted.
#
# Deliberately narrow: data under $APP_ROOT and $TEST_ROOT needs no root at
# all (step 3 made them user-owned). Root is granted only for
#   - service control and inspection (systemctl)
#   - installing a systemd unit file, which lives in /etc/systemd/system
#   - pointing the `current` symlink at a release (atomic switch needs root
#     because the unit runs as a different service account in production)
#
# NOT granted: chmod / chown / cp / mv / rm / dd / sh / bash / sudo -s.
# A broader whitelist would make this "non-root" test user meaningless.
# ---------------------------------------------------------------------------
SUDO_FILE="/etc/sudoers.d/opspilot-${TARGET_USER}"
log "writing sudo whitelist $SUDO_FILE"

cat > "$SUDO_FILE.tmp" <<EOF
# Managed by scripts/setup-ssh-test-target.sh — local DISPOSABLE test target.
# NOPASSWD whitelist for the OpsPilot integration-test user.
# Commands are argv-separated; there is no shell escape through these.
Defaults:${TARGET_USER} !requiretty

Cmnd_Alias OPSPILOT_UNIT = \\
    /usr/bin/systemctl is-active *, \\
    /usr/bin/systemctl status *, \\
    /usr/bin/systemctl show *, \\
    /usr/bin/systemctl restart *, \\
    /usr/bin/systemctl start *, \\
    /usr/bin/systemctl stop *, \\
    /usr/bin/systemctl cat *, \\
    /usr/bin/systemctl daemon-reload

# Installing a generated unit file. The destination is restricted to the
# systemd unit directory — sudoers matches the trailing argument here.
Cmnd_Alias OPSPILOT_UNIT_INSTALL = \\
    /usr/bin/install -m * -o * -g * * /etc/systemd/system/*, \\
    /usr/bin/install -m * * /etc/systemd/system/*

# Atomic release switch: ln -sfn <release> current.tmp && mv -T current.tmp current
Cmnd_Alias OPSPILOT_LINK = \\
    /usr/bin/ln -sfn * ${APP_ROOT}/*, \\
    /bin/mv -T ${APP_ROOT}/* ${APP_ROOT}/*

${TARGET_USER} ALL=(root) NOPASSWD: OPSPILOT_UNIT, OPSPILOT_UNIT_INSTALL, OPSPILOT_LINK
EOF

if visudo -cf "$SUDO_FILE.tmp" >/dev/null; then
  mv "$SUDO_FILE.tmp" "$SUDO_FILE"
  chmod 440 "$SUDO_FILE"
  log "sudoers snippet installed"
else
  rm -f "$SUDO_FILE.tmp"
  fail "generated sudoers snippet failed visudo -c; nothing installed"
fi

# ---------------------------------------------------------------------------
# 6. Smoke test: can we actually SSH in as the test user, non-interactively?
# ---------------------------------------------------------------------------
log "smoking the connection"
if ssh -i "$KEY_FILE" \
       -o BatchMode=yes \
       -o StrictHostKeyChecking=no \
       -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout=5 \
       "${TARGET_USER}@127.0.0.1" 'id -un && pwd' >/dev/null 2>&1; then
  log "ssh login OK"
else
  # StrictHostKeyChecking=no is ONLY for this local smoke; OpsPilot itself must
  # verify host keys and never use AutoAddPolicy. See docs/security-model.md.
  fail "ssh login as ${TARGET_USER}@127.0.0.1 failed"
fi

cat <<EOF

------------------------------------------------------------
Local SSH test target ready.

  user        : ${TARGET_USER}   (non-root)
  ssh         : ${TARGET_USER}@127.0.0.1:22
  host key    : $(ssh-keygen -lf "$HOST_KEY")
  client key  : ${KEY_FILE}   (mode 600)
  app root    : ${APP_ROOT}   (${TARGET_USER} owned — no sudo needed to write)
  test root   : ${TEST_ROOT}   (${TARGET_USER} owned — no sudo needed to write)
  sudo        : NOPASSWD whitelist (systemctl + unit install + symlink switch),
                see ${SUDO_FILE}

Add to .env for the S1 integration tests:

  OPSPILOT_SSH_USER=${TARGET_USER}
  OPSPILOT_SSH_KEY_FILE=${KEY_FILE}
  OPSPILOT_TEST_SSH_HOST=127.0.0.1
  OPSPILOT_TEST_SSH_PORT=22

This target is DISPOSABLE. Do not point production runs at it.
------------------------------------------------------------
EOF
