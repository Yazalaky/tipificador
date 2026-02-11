#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"

CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-tipificador-api}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
DISABLE_BACKEND="${DISABLE_BACKEND:-1}"

cd "$FRONTEND_DIR"

if grep -q '"/index.html"' firebase.json; then
  sed -i 's#"destination": "/index.html"#"destination": "/maintenance.html"#' firebase.json
fi

npm run build
if [ -f "dist/maintenance.html" ]; then
  cp dist/maintenance.html dist/index.html
fi
firebase deploy --only hosting

if [ "$DISABLE_BACKEND" = "1" ]; then
  gcloud run services remove-iam-policy-binding "$CLOUD_RUN_SERVICE" \
    --region "$CLOUD_RUN_REGION" \
    --member="allUsers" \
    --role="roles/run.invoker" || true
fi
