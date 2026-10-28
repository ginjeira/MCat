#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then echo "Executa como root." >&2; exit 1; fi
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${MCAT_USER:-mcat}"
if ! id "$APP_USER" >/dev/null 2>&1; then useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"; fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data" "$APP_DIR/.env" 2>/dev/null || true
cat >/etc/systemd/system/mcat.service <<EOF
[Unit]
Description=MCat Telegram playlist catalog PoC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m mcat
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data $APP_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "Serviço criado. Antes de o ativar, confirma que o primeiro login Telegram foi concluído e que a sessão pertence a $APP_USER."
echo "Para ativar: systemctl enable --now mcat"
