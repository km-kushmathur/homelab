#!/bin/bash
# Install this hook on each container that depends on the shared media mount.
set -euo pipefail

if [[ "${2:-}" == pre-start ]]; then
    exec /usr/bin/timeout 20 /usr/bin/python3 /opt/homelab/scripts/media_storage.py
fi
