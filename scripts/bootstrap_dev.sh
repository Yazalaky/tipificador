#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"

echo "[1/4] Validando herramientas base..."
command -v python3 >/dev/null 2>&1 || { echo "python3 no encontrado"; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm no encontrado"; exit 1; }

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Aviso: tesseract no esta instalado. OCR no funcionara hasta instalarlo."
fi

echo "[2/4] Preparando entorno backend..."
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
deactivate

echo "[3/4] Instalando dependencias frontend..."
cd "$FRONTEND_DIR"
npm install

echo "[4/4] Listo."
echo ""
echo "Siguiente paso:"
echo "Backend:  cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --port 8000"
echo "Frontend: cd frontend && npm run dev"

