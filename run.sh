#!/usr/bin/env bash
set -euo pipefail

# Install dependencies (use a virtualenv if preferred; remove --user to install system-wide)
python3 -m pip install --user PyQt6 PyQt6-Fluent-Widgets pillow numpy websockets requests

# If first argument is not "h", relaunch this script in background and exit current process
if [ "${1-}" != "h" ]; then
  nohup "$0" h >/dev/null 2>&1 &
  exit 0
fi

# Start the application
python3 Main.pyw
