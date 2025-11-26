#!/usr/bin/env bash
set -euo pipefail

# Installs the logger as a systemd service on Ubuntu 24.04.
# This script assumes it is run from the repository root.

REPO_DIR="$(pwd)"
INSTALL_DIR="/opt/discord-logger"
SERVICE_FILE="/etc/systemd/system/azure-redbot-logger.service"

sudo mkdir -p "${INSTALL_DIR}"
sudo cp "${REPO_DIR}/logger.py" "${REPO_DIR}/config.json" "${REPO_DIR}/.env" "${INSTALL_DIR}/"

# Python environment with required packages.
sudo python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install requests python-dotenv psutil

# Ensure config and env are only readable by root.
sudo chmod 600 "${INSTALL_DIR}/config.json" "${INSTALL_DIR}/.env"

# Install systemd service.
sudo tee "${SERVICE_FILE}" > /dev/null <<'UNIT'
[Unit]
Description=Azure Redbot Discord Logger
After=network.target

[Service]
ExecStart=/opt/discord-logger/venv/bin/python /opt/discord-logger/logger.py
WorkingDirectory=/opt/discord-logger
Restart=always
RestartSec=3
User=azureuser
KillSignal=SIGTERM
TimeoutStopSec=10
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable azure-redbot-logger.service
sudo systemctl restart azure-redbot-logger.service

echo "Logger installed and service started."
