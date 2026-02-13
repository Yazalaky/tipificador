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

gcloud builds submit --tag "$IMAGE_URI"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE_URI" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated
