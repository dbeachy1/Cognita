#!/bin/sh
# Keep this WSL client alive for the unit lifetime; otherwise WSL may stop the
# dedicated distro while systemd still has the application containers running.
set -eu
unit=cognita-compose.service
systemctl start "$unit"
while systemctl is-active --quiet "$unit"; do
    sleep 2
done
