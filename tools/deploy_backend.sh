#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"

PROJECT_ID="${PROJECT_ID:-tipificador-cloud-prod}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-tipificador-api}"
REPO="${REPO:-tipificador}"
IMAGE_TAG="${IMAGE_TAG:-prod}"

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/api:${IMAGE_TAG}"

cd "$BACKEND_DIR"

BUILD_ID="$(
  gcloud builds submit     --project "$PROJECT_ID"     --async     --tag "$IMAGE_URI"     --format="value(id)"
)"

if [ -z "$BUILD_ID" ]; then
  echo "No se pudo obtener el ID de Cloud Build."
  exit 1
fi

echo "Cloud Build ID: $BUILD_ID"

for attempt in $(seq 1 180); do
  BUILD_STATUS="$(
    gcloud builds describe "$BUILD_ID"       --project "$PROJECT_ID"       --format="value(status)"
  )"

  echo "Cloud Build status: $BUILD_STATUS"

  case "$BUILD_STATUS" in
    SUCCESS)
      break
      ;;
    QUEUED|WORKING|PENDING)
      sleep 10
      ;;
    FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED)
      echo "Cloud Build terminó con estado $BUILD_STATUS."
      exit 1
      ;;
    *)
      echo "Estado inesperado de Cloud Build: $BUILD_STATUS"
      exit 1
      ;;
  esac

  if [ "$attempt" -eq 180 ]; then
    echo "Cloud Build no terminó dentro de 30 minutos."
    exit 1
  fi
done
gcloud run deploy "$SERVICE" \
  --image "$IMAGE_URI" \
  --region "$REGION" \
  --platform managed \
  --no-cpu-throttling \
  --update-env-vars TIPIFICADOR_OCR_WORKERS=1,TIPIFICADOR_OCR_PAGE_TIMEOUT_SECONDS=120,TIPIFICADOR_BATCH_PACKAGE_TIMEOUT_SECONDS=600,TIPIFICADOR_BATCH_STALE_SECONDS=900 \
  --allow-unauthenticated
