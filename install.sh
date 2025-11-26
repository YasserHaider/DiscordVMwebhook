#!/usr/bin/env bash
set -euo pipefail

# Installs the logger as a systemd service on Ubuntu 24.04.
# This script assumes it is run from the repository root.

REPO_DIR="$(pwd)"
INSTALL_DIR="/opt/discord-logger"
SERVICE_FILE="/etc/systemd/system/azure-redbot-logger.service"

sudo mkdir -p "${INSTALL_DIR}"
sudo cp -r "${REPO_DIR}"/* "${INSTALL_DIR}"

# Python environment with required packages.
sudo python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install requests python-dotenv

# Ensure config and env are only readable by root.
sudo chmod 600 "${INSTALL_DIR}/config.json" "${INSTALL_DIR}/.env"

# Install systemd service.
sudo tee "${SERVICE_FILE}" > /dev/null <<'UNIT'
[Unit]
Description=Azure Redbot Discord Logger
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/discord-logger
ExecStart=/opt/discord-logger/venv/bin/python /opt/discord-logger/logger.py
Restart=on-failure
RestartSec=5
Environment="PYTHONUNBUFFERED=1"
# systemd sends SIGTERM first; the logger catches it to notify Discord before stopping.

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable azure-redbot-logger.service
sudo systemctl restart azure-redbot-logger.service

echo "Logger installed and service started."
