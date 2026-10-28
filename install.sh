#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Executa com sudo/root: bash install.sh" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip git
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Criado .env. Preenche as credenciais antes de executar."
else
  echo ".env já existe; não foi alterado."
fi
echo "Instalação concluída. Edita $APP_DIR/.env e executa: $APP_DIR/.venv/bin/python -m mcat"
