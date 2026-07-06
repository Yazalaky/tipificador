#!/usr/bin/env bash

set -euo pipefail

BATCH_ID="${1:-}"

if [[ ! "$BATCH_ID" =~ ^[a-fA-F0-9]{32}$ ]]; then
  echo "ERROR: batch inválido. Debe contener 32 caracteres hexadecimales." >&2
  exit 2
fi

exec env -i   HOME="/home/sistemas"   HERMES_REAL_HOME="/home/sistemas"   PATH="/usr/bin:/usr/bin:/usr/local/bin:/usr/bin:/bin"   LANG="C.UTF-8"   "/usr/bin/python3"   "/home/sistemas/projects/tipificador/tools/diagnose_batch.py"   "$BATCH_ID"
