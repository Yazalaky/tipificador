#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${ROOT_DIR}/tools/deploy_backend.sh"
"${ROOT_DIR}/tools/deploy_frontend.sh"
