#!/usr/bin/env bash
# Install the monitor as a systemd service so it runs 24/7 and restarts on
# reboot or crash. Run after setup.sh, with sudo:
#
#   sudo ./install_service.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./install_service.sh" >&2
    exit 1
fi

# Run the service as the user who owns this checkout, not root.
RUN_USER="${SUDO_USER:-$(id -un)}"
APP_DIR="$(pwd)"
PYTHON="${APP_DIR}/.venv/bin/python"
SERVICE_NAME="crypto-range-monitor"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -x "${PYTHON}" ]; then
    echo "Virtualenv not found at ${PYTHON}. Run ./setup.sh first." >&2
    exit 1
fi

echo "Writing ${UNIT} (runs as ${RUN_USER})..."
cat > "${UNIT}" <<EOF
[Unit]
Description=Crypto Range Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON} ${APP_DIR}/crypto_range_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "Service '${SERVICE_NAME}' installed, enabled, and started."
echo "  Status:  systemctl status ${SERVICE_NAME}"
echo "  Live log: journalctl -u ${SERVICE_NAME} -f"
echo "  File log: ${APP_DIR}/crypto_range_monitor.log"
echo "  Stop:    sudo systemctl stop ${SERVICE_NAME}"
