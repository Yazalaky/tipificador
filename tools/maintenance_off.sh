#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"

CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-tipificador-api}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
ENABLE_BACKEND="${ENABLE_BACKEND:-1}"

cd "$FRONTEND_DIR"

if grep -q '"/maintenance.html"' firebase.json; then
  sed -i 's#"destination": "/maintenance.html"#"destination": "/index.html"#' firebase.json
fi

npm run build
firebase deploy --only hosting

if [ "$ENABLE_BACKEND" = "1" ]; then
  gcloud run services add-iam-policy-binding "$CLOUD_RUN_SERVICE" \
    --region "$CLOUD_RUN_REGION" \
    --member="allUsers" \
    --role="roles/run.invoker" || true
fi
