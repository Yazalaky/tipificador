#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-tipificador-cloud-prod}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-tipificador-api}"

"${ROOT_DIR}/tools/deploy_backend.sh"

BACKEND_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format='value(status.url)')"

if [ -z "$BACKEND_URL" ]; then
  echo "No se pudo obtener la URL de Cloud Run para ${SERVICE}."
  exit 1
fi

echo "Backend URL: $BACKEND_URL"
curl --fail --silent --show-error "${BACKEND_URL}/health" >/dev/null

VITE_API_BASE="$BACKEND_URL" "${ROOT_DIR}/tools/deploy_frontend.sh"
