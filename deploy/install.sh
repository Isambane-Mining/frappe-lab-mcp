#!/usr/bin/env bash
# Install or upgrade frappe-lab-mcp into /opt (root-owned, read-only to developers),
# restart registered instances, then optionally register a developer.
#
#   sudo deploy/install.sh              # install/upgrade, then offer to configure a user
#   sudo deploy/install.sh juan         # install/upgrade, then configure juan
#
# Per-user management afterwards:
#   sudo /opt/frappe-lab-mcp/.venv/bin/frappe-lab-mcp-admin {configure|list|rotate-secret|remove|restart|nginx-map}
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
command -v supervisorctl >/dev/null || { echo "supervisor is not installed" >&2; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/frappe-lab-mcp
UV="${UV:-$(command -v uv || true)}"
[ -n "$UV" ] || UV="$(ls /home/*/*/env/bin/uv 2>/dev/null | head -1 || true)"

echo "== Installing code to $DEST"
install -d -m 755 "$DEST"
rsync -a --delete \
    --exclude .venv --exclude .git --exclude __pycache__ --exclude '*.egg-info' \
    --exclude 'config.yaml' --exclude '*.local.yaml' --exclude 'state/' \
    "$SRC/" "$DEST/src/"
if [ -n "$UV" ]; then
    [ -x "$DEST/.venv/bin/python" ] || "$UV" venv -q --python /usr/bin/python3 "$DEST/.venv"
    "$UV" pip install -q --python "$DEST/.venv/bin/python" --reinstall-package frappe-lab-mcp "$DEST/src"
else
    [ -x "$DEST/.venv/bin/python" ] || /usr/bin/python3 -m venv "$DEST/.venv"
    "$DEST/.venv/bin/pip" install -q --upgrade "$DEST/src"
fi
chown -R root:root "$DEST"
chmod -R go-w "$DEST"

install -d -m 755 /etc/frappe-lab-mcp
install -d -m 751 /etc/frappe-lab-mcp/instances
install -d -m 755 /var/log/frappe-lab-mcp
[ -f /etc/frappe-lab-mcp/nginx-users.map ] || \
    printf '# managed by frappe-lab-mcp-admin: <user> <port>;\n' > /etc/frappe-lab-mcp/nginx-users.map

ADMIN="$DEST/.venv/bin/frappe-lab-mcp-admin"
running=$(supervisorctl status 2>/dev/null | awk '/^frappe-lab-mcp-/ {print $1}' || true)
for prog in $running; do
    supervisorctl restart "$prog" >/dev/null && echo "restarted $prog"
done
echo "Installed. Admin tool: $ADMIN"

if [ -n "${1:-}" ]; then
    exec "$ADMIN" configure "$1"
elif [ -t 0 ]; then
    read -r -p "Register/reconfigure a developer now? Linux user (blank to skip): " who
    [ -z "$who" ] || exec "$ADMIN" configure "$who"
fi
