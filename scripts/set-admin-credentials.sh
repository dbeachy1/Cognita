#!/usr/bin/env bash
# Set the Cognita admin username + password, then restart the server so the new
# credentials take effect. Thin launcher — the logic lives in
# scripts/set-admin-credentials.py (which writes only the password HASH).
#
#   ./scripts/set-admin-credentials.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

# 1. Prompt + write the new credentials (aborts here on empty/mismatch).
"$PY" "$REPO/scripts/set-admin-credentials.py" "$@"

# 2. Restart Cognita so the admin app reloads the credentials (they are read at
#    startup — there is deliberately NO live password-reset endpoint).
echo
echo "Restarting Cognita for the change to take effect..."

# Match the repo's venv plus "cognita serve", which covers BOTH launch forms:
#   console script:  <repo>/.venv/bin/python3 <repo>/.venv/bin/cognita serve   (systemd on kei)
#   module form:     <repo>/.venv/bin/python -m cognita serve                  (manual)
# It cannot match this wrapper (no .venv/bin in its argv) nor the credential-setting
# python call (its argv has no "cognita serve").
SERVE_PAT="$REPO/.venv/bin/.*cognita serve"

# Prefer systemd when the server is managed by it — killing the process directly would
# just make the supervisor restart it, and on kei the unit is a --user unit (NOT a system
# one, and NOT under sudo). Checked before the pgrep path so the managed case always wins.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl --user list-unit-files cognita.service >/dev/null 2>&1 \
   && [ -n "$(systemctl --user list-unit-files cognita.service --no-legend 2>/dev/null)" ]; then
    echo "(systemd --user unit detected — restarting cognita.service.)"
    systemctl --user restart cognita.service
    for _ in $(seq 1 20); do
        [ "$(systemctl --user is-active cognita.service 2>/dev/null)" = "active" ] && break
        sleep 1
    done
    if [ "$(systemctl --user is-active cognita.service 2>/dev/null)" = "active" ]; then
        echo "Done. Cognita restarted with the new admin credentials."
        echo "(Watch it come up: tail -f $REPO/logs/cognita.log)"
        exit 0
    fi
    echo "systemctl --user restart cognita.service did not come up active." >&2
    echo "Check: systemctl --user status cognita.service" >&2
    exit 1
fi

pid="$(pgrep -f "$SERVE_PAT" || true)"
if [ -n "$pid" ]; then
    if ! kill $pid 2>/dev/null; then
        echo "Could not stop Cognita (pid $pid): permission denied." >&2
        echo "It may be running as another user or as a service. Restart it yourself" >&2
        echo "for the change to take effect, e.g.:  sudo kill $pid   (then relaunch)" >&2
        echo "or, if you run it under systemd:      systemctl --user restart cognita" >&2
        exit 1
    fi
    for _ in $(seq 1 15); do kill -0 $pid 2>/dev/null || break; sleep 1; done
else
    echo "(Cognita was not running — starting it.)"
fi

# Start it back up, fully detached (survives this shell / an SSH disconnect).
cd "$REPO"
setsid "$PY" -m cognita serve >/dev/null 2>&1 </dev/null &
sleep 3

if pgrep -f "$SERVE_PAT" >/dev/null; then
    echo "Done. Cognita restarted with the new admin credentials."
    echo "(Watch it come up: tail -f $REPO/logs/cognita.log)"
else
    echo "Started Cognita but it isn't showing yet — check $REPO/logs/cognita.log." >&2
    exit 1
fi
