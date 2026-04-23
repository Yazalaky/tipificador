#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"

FIREBASE_PROJECT="${FIREBASE_PROJECT:-tipificador-cloud}"

cd "$FRONTEND_DIR"

if [ -z "${VITE_API_BASE:-}" ]; then
  echo "VITE_API_BASE no esta definido."
  echo "Usa bash tools/deploy_all.sh o exporta VITE_API_BASE con la URL publica del backend."
  exit 1
fi

if [[ "$VITE_API_BASE" =~ ^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?(/.*)?$ ]]; then
  echo "VITE_API_BASE apunta a localhost/127.0.0.1 y no debe desplegarse asi."
  echo "Aborto el deploy del frontend para evitar publicar una app que no puede conectarse al backend."
  exit 1
fi

npm run build
firebase deploy --only hosting --project "$FIREBASE_PROJECT"
