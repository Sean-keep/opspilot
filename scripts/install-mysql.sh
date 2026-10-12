#!/usr/bin/env bash
# Install and prepare a local MySQL instance for OpsPilot's control-plane
# metadata. Intended for a fresh Ubuntu 22.04/24.04 or Debian 12 host.
#
# Safety rules this script obeys (mirroring the platform's own policy):
#   * never initialise, reset, or wipe an existing data directory
#   * never reset an existing root password
#   * never run an irreversible upgrade
#   * fail loudly — no step is allowed to fail silently
#
# Usage:
#   sudo ./scripts/install-mysql.sh
#   OPSPILOT_DB_PASSWORD=... sudo -E ./scripts/install-mysql.sh

set -euo pipefail

log()  { printf '[install-mysql] %s\n' "$*"; }
fail() { printf '[install-mysql] ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 1. Preconditions
# --------------------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || fail "must run as root (mysql install needs it)"

DB_NAME="${OPSPILOT_DB_NAME:-opspilot}"
TEST_DB_NAME="${OPSPILOT_TEST_DB_NAME:-opspilot_test}"
DB_USER="${OPSPILOT_DB_USER:-opspilot}"
DB_HOST_BIND="${OPSPILOT_DB_HOST:-127.0.0.1}"

if [[ -d /var/lib/mysql ]]; then
  if [[ -n "$(ls -A /var/lib/mysql 2>/dev/null)" ]]; then
    log "existing MySQL data directory found at /var/lib/mysql — leaving it untouched"
    EXISTING_DATADIR=1
  fi
fi

if command -v mysql >/dev/null 2>&1; then
  log "mysql client already present: $(mysql --version)"
fi

# --------------------------------------------------------------------------
# 2. Install (idempotent)
# --------------------------------------------------------------------------
if ! dpkg -s mysql-server >/dev/null 2>&1; then
  log "installing mysql-server via apt"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq mysql-server
else
  log "mysql-server already installed"
fi

# --------------------------------------------------------------------------
# 3. Start
# --------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now mysql
  systemctl --no-pager --full status mysql | head -n 5 || true
else
  fail "systemctl is required to start mysql; start it manually and re-run"
fi

for _ in $(seq 1 30); do
  if mysqladmin ping --silent >/dev/null 2>&1; then break; fi
  sleep 1
done
mysqladmin ping --silent >/dev/null 2>&1 || fail "mysql did not become ready in 30s"

log "mysql is up: $(mysql -N -e 'SELECT VERSION()')"

# --------------------------------------------------------------------------
# 4. Create databases and the application user
#    These are additive. Nothing here drops or re-initialises a data dir.
# --------------------------------------------------------------------------
if [[ -z "${OPSPILOT_DB_PASSWORD:-}" ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "password for MySQL user '${DB_USER}': " OPSPILOT_DB_PASSWORD
    echo
  else
    fail "set OPSPILOT_DB_PASSWORD in the environment (non-interactive run)"
  fi
fi
[[ -n "${OPSPILOT_DB_PASSWORD}" ]] || fail "empty database password"

log "creating databases '${DB_NAME}' and '${TEST_DB_NAME}' (additive, no drops)"
mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS \`${TEST_DB_NAME}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${OPSPILOT_DB_PASSWORD}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${OPSPILOT_DB_PASSWORD}';

GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.*      TO '${DB_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.*      TO '${DB_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${TEST_DB_NAME}\`.* TO '${DB_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`${TEST_DB_NAME}\`.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

# --------------------------------------------------------------------------
# 5. Verify the application user can connect
# --------------------------------------------------------------------------
mysql -h"${DB_HOST_BIND}" -u"${DB_USER}" -p"${OPSPILOT_DB_PASSWORD}" -N \
  -e "SELECT 'connect ok'" "${DB_NAME}" >/dev/null \
  || fail "application user could not connect to ${DB_NAME}"

log "done. Next:"
log "  1. copy .env.example to .env and set OPSPILOT_DB_PASSWORD"
log "  2. alembic upgrade head"
log "  3. python3 scripts/bootstrap.py"
