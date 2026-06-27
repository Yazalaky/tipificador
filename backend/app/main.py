import os
import re
import json
import uuid
import time
import logging
import unicodedata
import shutil
import zipfile
import subprocess
import concurrent.futures
import threading
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Dict, List, Literal, NamedTuple, Optional, Tuple

import fitz  # PyMuPDF
import google.auth
from google.api_core import exceptions as google_exceptions
from google.auth.transport.requests import Request
from google.cloud import storage
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Header, Form
from fastapi.responses import Response, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


logger = logging.getLogger("tipificador")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
logger.propagate = False


# ----------------------------
# Config
# ----------------------------
JOB_ROOT = os.environ.get("TIPIFICADOR_JOB_ROOT", "/tmp/tipificador_jobs")
os.makedirs(JOB_ROOT, exist_ok=True)
BATCH_ROOT = os.path.join(JOB_ROOT, "batches")
os.makedirs(BATCH_ROOT, exist_ok=True)

CATEGORIES = ["CRC", "FEV", "HEV", "OPF", "PDE"]
Category = Literal["CRC", "FEV", "HEV", "OPF", "PDE"]
SERVICE_IDS = {"cuidador", "otros_servicios"}

THUMB_WIDTH = 240
VIEW_WIDTH = 1100

MAX_FILE_BYTES = int(os.environ.get("TIPIFICADOR_MAX_FILE_BYTES", "104857600"))  # 100MB
MAX_FILES = int(os.environ.get("TIPIFICADOR_MAX_FILES", "80"))
JOB_TTL_SECONDS = int(os.environ.get("TIPIFICADOR_JOB_TTL_SECONDS", "21600"))  # 6 hours
CACHE_VIEW = os.environ.get("TIPIFICADOR_CACHE_VIEW", "1").lower() not in {"0", "false", "no"}
OCR_ENABLED = os.environ.get("TIPIFICADOR_OCR_ENABLED", "1").lower() not in {"0", "false", "no"}
OCR_LANG = os.environ.get("TIPIFICADOR_OCR_LANG", "spa+eng")
OCR_DPI = int(os.environ.get("TIPIFICADOR_OCR_DPI", "300"))
OCR_PSM = os.environ.get("TIPIFICADOR_OCR_PSM", "4")
OCR_HEADER_RATIO = float(os.environ.get("TIPIFICADOR_OCR_HEADER_RATIO", "0.35"))
OCR_HEADER_DPI = int(os.environ.get("TIPIFICADOR_OCR_HEADER_DPI", str(min(200, OCR_DPI))))
OCR_MIN_TEXT_LEN = int(os.environ.get("TIPIFICADOR_OCR_MIN_TEXT_LEN", "40"))
OCR_KEEP_IMAGES = os.environ.get("TIPIFICADOR_OCR_KEEP_IMAGES", "0").lower() in {"1", "true", "yes"}
OCR_WORKERS = int(os.environ.get("TIPIFICADOR_OCR_WORKERS", "4"))
OCR_PAGE_TIMEOUT_SECONDS = int(os.environ.get("TIPIFICADOR_OCR_PAGE_TIMEOUT_SECONDS", "120"))
PDF_REWRITE_ENABLED = os.environ.get("TIPIFICADOR_PDF_REWRITE_ENABLED", "1").lower() not in {"0", "false", "no"}
MAX_BATCH_PACKAGES = int(os.environ.get("TIPIFICADOR_MAX_BATCH_PACKAGES", "20"))
MAX_BATCH_BYTES = int(os.environ.get("TIPIFICADOR_MAX_BATCH_BYTES", "524288000"))  # 500MB
BATCH_PACKAGE_TIMEOUT_SECONDS = int(os.environ.get("TIPIFICADOR_BATCH_PACKAGE_TIMEOUT_SECONDS", "600"))
BATCH_STALE_SECONDS = int(os.environ.get("TIPIFICADOR_BATCH_STALE_SECONDS", "900"))
GCS_BUCKET = os.environ.get("TIPIFICADOR_GCS_BUCKET", "").strip()
GCS_UPLOAD_PREFIX = os.environ.get("TIPIFICADOR_GCS_UPLOAD_PREFIX", "uploads/").strip()
GCS_RESULTS_PREFIX = os.environ.get("TIPIFICADOR_GCS_RESULTS_PREFIX", "results/").strip()
GCS_SIGNED_URL_EXP_SECONDS = int(os.environ.get("TIPIFICADOR_GCS_SIGNED_URL_EXP_SECONDS", "3600"))
GCS_SIGNER_EMAIL = os.environ.get("TIPIFICADOR_GCS_SIGNER_EMAIL", "").strip()
CLEANUP_TOKEN = os.environ.get("TIPIFICADOR_CLEANUP_TOKEN", "").strip()
CLEANUP_AGE_MINUTES = int(os.environ.get("TIPIFICADOR_CLEANUP_AGE_MINUTES", "30"))

BATCH_META_REVISION_FIELD = "metaRevision"
BATCH_META_SYNC_ERROR_FIELD = "metaSyncError"
BATCH_META_GCS_RETRY_ATTEMPTS = 5
BATCH_META_GCS_NONFINAL_ATTEMPTS = 1
BATCH_META_GCS_RETRY_DELAY_SECONDS = 0.15
BATCH_META_GCS_MAX_DELAY_SECONDS = 2.0

_GCS_PRECONDITION_FAILED = getattr(google_exceptions, "PreconditionFailed", None)
_GCS_TRANSIENT_EXC_TYPES = (
    google_exceptions.ServiceUnavailable,
    google_exceptions.GatewayTimeout,
    google_exceptions.InternalServerError,
    google_exceptions.DeadlineExceeded,
)


class BatchMetaPersistenceError(RuntimeError):
    pass


class BatchMetaGCSResult(NamedTuple):
    success: bool
    final_meta: dict
    observed_generation: Optional[int]
    error: Optional[Exception]
    error_kind: Optional[str]


class BatchMetaVerificationError(BatchMetaPersistenceError):
    pass

_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_NIT_RE = re.compile(
    r"\bNIT\b\s*[:\-]?\s*([0-9\.\, ]{6,15}(?:\s*-\s*\d)?)",
    flags=re.IGNORECASE,
)
_OCFE_RE = re.compile(r"\bOCFE\s*(\d{3,})\b", flags=re.IGNORECASE)
_INVOICE_RE = re.compile(r"\b([A-Z]{3,6})\s*(\d{3,})\b")
_INVOICE_HINTS = ("FACTURA", "ELECTR", "VENTA", "N°", "NO.", "NRO", "CUFE", "BUFE")
_FECHA_CREACION_RE = re.compile(
    r"FECHA\s*DE\s*CREA(?:CION|CIÓN)\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
    flags=re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
_TIME_RE = re.compile(r"\b\d{2}:\d{2}\b")
_FEV_HINTS = ("FACTURA ELECTRONICA DE VENTA", "FACTURA ELECTRÓNICA DE VENTA")
_NC_HINTS = ("NOTA DE CREDITO ELECTRONICA", "NOTA DE CRÉDITO ELECTRONICA")
_AUTO_RULES_STRONG: List[Tuple[str, Tuple[str, ...]]] = [
    ("PDE", ("AUTORIZACION SERVICIOS", "AUTORIZACION DE SERVICIOS")),
    ("OPF", ("ORDEN MEDICA", "ORDEN MÉDICA")),
    (
        "CRC",
        (
            "REGISTRO DE ATENCION DOMICILIARIA",
            "REGISTRO DE ATENCIÓN DOMICILIARIA",
        ),
    ),
    (
        "HEV",
        (
            "CERTIFICACION PRESTACION DE SERVICIOS",
            "CERTIFICACION PRESTACION DE SERVICIOS POR CONCEPTO",
            "CERTIFICACION DETALLE DE CARGOS",
        ),
    ),
    (
        "HEV",
        (
            "REGISTRO DE ACTIVIDADES DE CUIDADO",
            "REGISTRO DE ACTIVIDADES DE CUIDADOR",
        ),
    ),
    (
        "HEV",
        (
            "HISTORIA CLINICA",
            "HISTORIA CLÍNICA",
            "TRABAJO SOCIAL",
        ),
    ),
    ("FEV", ("FACTURA ELECTRONICA DE VENTA", "NOTA DE CREDITO ELECTRONICA", "NOTA DE CRÉDITO ELECTRONICA", "DETALLE DE CARGOS", "FACTURA OCFE")),
]
_AUTO_RULES_FIXED: List[Tuple[str, Tuple[str, ...]]] = [
    (cat, patterns) for cat, patterns in _AUTO_RULES_STRONG if cat in {"CRC", "FEV", "PDE"}
]


# ----------------------------
# Helpers
# ----------------------------
def _job_dir(job_id: str) -> str:
    return os.path.join(JOB_ROOT, job_id)


def _batch_dir(batch_id: str) -> str:
    return os.path.join(BATCH_ROOT, batch_id)


def _meta_path(job_id: str) -> str:
    return os.path.join(_job_dir(job_id), "meta.json")


def _assert_job_id(job_id: str) -> None:
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise HTTPException(status_code=404, detail="Job no existe o expiró.")


def _load_meta(job_id: str) -> dict:
    _assert_job_id(job_id)
    path = _meta_path(job_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Job no existe o expiró.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_meta(job_id: str, meta: dict) -> None:
    with open(_meta_path(job_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _batch_meta_path(batch_id: str) -> str:
    return os.path.join(_batch_dir(batch_id), "meta.json")


def _batch_meta_object_name(batch_id: str) -> str:
    return f"{_normalize_prefix(GCS_RESULTS_PREFIX)}{batch_id}/meta.json"


def _meta_revision(meta: Optional[dict]) -> int:
    if not meta:
        return 0
    try:
        return int(meta.get(BATCH_META_REVISION_FIELD) or 0)
    except (TypeError, ValueError):
        return 0


# Orden de progreso para estados de paquete. Valor mayor = más avanzado.
_PACKAGE_STATUS_ORDER = {
    "pending": 0,
    "processing": 1,
    "done": 2,
    "error": 2,
    "cancelled": 2,
}


# Orden para estados de batch. Los terminales comparten el mismo rango máximo.
_BATCH_STATUS_ORDER = {
    "ready": 0,
    "pending": 1,
    "processing": 2,
    "cancelling": 3,
    "partial": 4,
    "error": 4,
    "cancelled": 4,
    "done": 4,
}

_BATCH_TERMINAL_STATUSES = {"done", "partial", "error", "cancelled"}
_BATCH_NONTERMINAL_STATUSES = {"ready", "pending", "processing", "cancelling"}


def _package_status_rank(status: Optional[str]) -> int:
    return _PACKAGE_STATUS_ORDER.get(status, 0)


def _batch_status_rank(status: Optional[str]) -> int:
    return _BATCH_STATUS_ORDER.get(status, 0)


def _is_terminal_batch_status(status: Optional[str]) -> bool:
    return status in _BATCH_TERMINAL_STATUSES


def _is_cancelling_batch_status(status: Optional[str]) -> bool:
    return status == "cancelling"


def _merge_batch_status(local_status: Optional[str], remote_status: Optional[str]) -> Optional[str]:
    """Merge explícito de estados del batch sin perder cancelaciones ni terminales."""
    if _is_terminal_batch_status(remote_status):
        return remote_status
    if _is_terminal_batch_status(local_status):
        return local_status
    if _is_cancelling_batch_status(local_status) or _is_cancelling_batch_status(remote_status):
        return "cancelling"
    if _batch_status_rank(local_status) >= _batch_status_rank(remote_status):
        return local_status
    return remote_status


def _merge_batch_cancel_requested(local_meta: dict, remote_meta: dict) -> bool:
    return bool(local_meta.get("cancelRequested", False) or remote_meta.get("cancelRequested", False))


def _gcs_error_code(exc: Exception) -> Optional[int]:
    """Extrae el código HTTP de una excepción de GCS, si lo tiene."""
    code = getattr(exc, "code", None)
    if code is None:
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _is_precondition_failure(exc: Exception) -> bool:
    code = _gcs_error_code(exc)
    if code == 412:
        return True
    precondition_cls = getattr(google_exceptions, "PreconditionFailed", None)
    return bool(precondition_cls and isinstance(exc, precondition_cls))


def _is_transient_gcs_error(exc: Exception) -> bool:
    """Errores de GCS que vale la pena reintentar con backoff."""
    code = _gcs_error_code(exc)
    if code is not None:
        # 412 es precondición fallida: se resuelve con merge, no reintento ciego.
        if code == 412:
            return False
        # 5xx y 429 son transitorios.
        if code >= 500 or code == 429:
            return True
    if isinstance(
        exc,
        (
            google_exceptions.ServiceUnavailable,
            google_exceptions.GatewayTimeout,
            google_exceptions.InternalServerError,
            google_exceptions.DeadlineExceeded,
        ),
    ):
        return True
    for linked in (getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(linked, Exception) and _is_transient_gcs_error(linked):
            return True
    # Errores de red/transporte sin código explícito.
    text = str(exc).lower()
    if any(x in text for x in ("timeout", "timed out", "connection", "network", "temporary", "retry")):
        return True
    return False


def _is_permanent_gcs_error(exc: Exception) -> bool:
    """Errores de GCS que no mejorarán reintentando."""
    code = _gcs_error_code(exc)
    if code is not None:
        # 412 no es permanente: se maneja con merge.
        if code == 412:
            return False
        if 400 <= code < 500:
            return True
    if isinstance(
        exc,
        (
            google_exceptions.Forbidden,
            google_exceptions.Unauthorized,
            google_exceptions.BadRequest,
        ),
    ):
        return True
    for linked in (getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(linked, Exception) and _is_permanent_gcs_error(linked):
            return True
    return False


def _merge_package(local_pkg: dict, remote_pkg: dict) -> dict:
    """
    Combina un paquete local con su versión remota preservando el mayor progreso.
    Reglas:
      - 'done' nunca pierde ante 'error' o 'cancelled'.
      - 'error'/'cancelled' no sobrescriben 'done'.
      - Entre estados no terminales, gana el más avanzado (processing > pending).
      - Se preservan campos finales importantes cuando el remoto está 'done'.
      - No se pierden nuevos campos locales que el remoto no tenga.
    """
    local_status = local_pkg.get("status")
    remote_status = remote_pkg.get("status")
    local_rank = _package_status_rank(local_status)
    remote_rank = _package_status_rank(remote_status)

    # Determinar la base ganadora.
    if local_status == "done":
        winner = "local"
    elif remote_status == "done":
        winner = "remote"
    elif local_rank > remote_rank:
        winner = "local"
    elif remote_rank > local_rank:
        winner = "remote"
    else:
        # Mismo rango: preferir local para conservar heartbeats más recientes,
        # pero sin perder campos finales del remoto si éste ya terminó.
        winner = "local"

    base_pkg = dict(local_pkg) if winner == "local" else dict(remote_pkg)
    other_pkg = remote_pkg if winner == "local" else local_pkg

    # Aplicar el status ganador (puede ser el mismo).
    if local_status == "done" or remote_status == "done":
        base_pkg["status"] = "done"
    elif winner == "local":
        base_pkg["status"] = local_status
    else:
        base_pkg["status"] = remote_status

    # Fusionar campos del otro lado sin perder información.
    for key, value in other_pkg.items():
        if value is None:
            continue
        current = base_pkg.get(key)
        if current is None:
            base_pkg[key] = value
        elif key == "lastHeartbeatAt":
            base_pkg[key] = max(current, value)
        elif key in ("finishedAt", "elapsedSeconds") and current is not None:
            # Conservar el valor más reciente/largo entre ambos.
            try:
                base_pkg[key] = max(current, value)
            except TypeError:
                pass
        # Para campos finales, si el remoto está done ya se respetan abajo.

    # Si el remoto está done, nunca perder sus campos finales.
    if remote_status == "done":
        for key in (
            "finishedAt",
            "elapsedSeconds",
            "gcsResult",
            "gcsAllZip",
            "allZip",
            "sourceGcsPath",
            "jobId",
            "resultFile",
            "downloadName",
        ):
            if remote_pkg.get(key) is not None:
                base_pkg[key] = remote_pkg[key]

    return base_pkg


def _merge_batch_meta(local_meta: dict, remote_meta: dict) -> dict:
    """
    Combina metadata local con remota tras un conflicto de concurrencia en GCS.
    El resultado tiene metaRevision = max(local, remoto) + 1.
    Garantiza que estados terminales del batch no regresen a estados iniciales.
    """
    if not local_meta and not remote_meta:
        return {}
    if not local_meta:
        merged = deepcopy(remote_meta)
        merged[BATCH_META_REVISION_FIELD] = _meta_revision(remote_meta) + 1
        return merged
    if not remote_meta:
        merged = deepcopy(local_meta)
        merged[BATCH_META_REVISION_FIELD] = _meta_revision(local_meta) + 1
        return merged

    merged = deepcopy(remote_meta)

    # Fusionar campos de nivel batch. El remoto tiene prioridad por ser el
    # estado confirmado en GCS, excepto que el local tenga información más
    # reciente que el remoto no posea.
    for key, value in local_meta.items():
        if value is None:
            continue
        remote_value = remote_meta.get(key)
        if key == "cancelRequested":
            merged[key] = _merge_batch_cancel_requested(local_meta, remote_meta)
        elif remote_value is None:
            merged[key] = value
        elif key == "lastHeartbeatAt":
            merged[key] = max(remote_value, value)
        elif key in ("finishedAt", "elapsedSeconds") and remote_value is not None:
            try:
                merged[key] = max(remote_value, value)
            except TypeError:
                pass

    # Preservar campos finales del batch si el remoto ya terminó.
    if remote_meta.get("status") == "done":
        for key in ("finishedAt", "elapsedSeconds", "gcsResult", "gcsAllZip", "allZip", "sourceGcsPath"):
            if remote_meta.get(key) is not None:
                merged[key] = remote_meta[key]

    # Fusionar paquetes por nombre.
    local_packages = {p.get("name"): p for p in local_meta.get("packages", []) if p.get("name")}
    remote_packages = {p.get("name"): p for p in remote_meta.get("packages", []) if p.get("name")}
    all_names = set(local_packages) | set(remote_packages)
    merged_packages = []
    for name in sorted(all_names):
        if name in local_packages and name in remote_packages:
            merged_packages.append(_merge_package(local_packages[name], remote_packages[name]))
        elif name in local_packages:
            merged_packages.append(deepcopy(local_packages[name]))
        else:
            merged_packages.append(deepcopy(remote_packages[name]))
    merged["packages"] = merged_packages

    # Evitar regresión de estado terminal.
    local_status = local_meta.get("status")
    remote_status = remote_meta.get("status")
    merged["status"] = _merge_batch_status(local_status, remote_status)

    # Recalcular conteo de estado global por si el merge cambió algo.
    done_count = sum(1 for p in merged_packages if p.get("status") == "done")
    error_count = sum(1 for p in merged_packages if p.get("status") == "error")
    cancelled_count = sum(1 for p in merged_packages if p.get("status") == "cancelled")
    pending_count = sum(1 for p in merged_packages if p.get("status") in {"pending", "processing"})
    if merged.get("status") not in _BATCH_TERMINAL_STATUSES | {"cancelling"}:
        if pending_count:
            merged["status"] = "processing"
        elif error_count and done_count:
            merged["status"] = "partial"
        elif error_count and not done_count:
            merged["status"] = "error"
        elif cancelled_count and not done_count and not error_count:
            merged["status"] = "cancelled"
        elif done_count:
            merged["status"] = "done"

    merged[BATCH_META_REVISION_FIELD] = max(_meta_revision(local_meta), _meta_revision(remote_meta)) + 1
    merged.pop(BATCH_META_SYNC_ERROR_FIELD, None)
    return merged


def _load_batch_meta_from_disk(batch_id: str) -> Optional[dict]:
    path = _batch_meta_path(batch_id)
    if not os.path.exists(path):
        return None
    for _ in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            time.sleep(0.05)
    return None


def _read_batch_meta_record_from_gcs(
    batch_id: str,
    *,
    cache_local: bool = True,
    strict: bool = False,
) -> Tuple[Optional[dict], Optional[int]]:
    if not _gcs_enabled():
        return None, None
    try:
        client = _gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(_batch_meta_object_name(batch_id))
        try:
            blob.reload()
        except Exception as exc:
            code = _gcs_error_code(exc)
            if code == 404 or isinstance(exc, (FileNotFoundError, google_exceptions.NotFound)):
                return None, None
            if strict:
                raise BatchMetaPersistenceError(
                    f"No se pudo leer metadata remota del batch {batch_id}"
                ) from exc
            return None, None
        generation = getattr(blob, "generation", None)
        try:
            generation = int(generation) if generation is not None else 0
        except (TypeError, ValueError):
            generation = 0
        data = blob.download_as_text(encoding="utf-8")
        meta = json.loads(data)
        if cache_local:
            os.makedirs(_batch_dir(batch_id), exist_ok=True)
            with open(_batch_meta_path(batch_id), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        return meta, generation or None
    except Exception as exc:
        if strict:
            raise BatchMetaPersistenceError(
                f"No se pudo leer metadata remota del batch {batch_id}"
            ) from exc
        return None, None


def _read_batch_meta_from_gcs(batch_id: str, *, cache_local: bool = True) -> Optional[dict]:
    meta, _ = _read_batch_meta_record_from_gcs(batch_id, cache_local=cache_local, strict=False)
    return meta


def _load_batch_meta_from_gcs(batch_id: str) -> Optional[dict]:
    return _read_batch_meta_from_gcs(batch_id, cache_local=True)


def _save_batch_meta_to_gcs(
    batch_id: str,
    meta: dict,
    *,
    final: bool = False,
    generation: Optional[int] = None,
) -> BatchMetaGCSResult:
    """Persist batch metadata to GCS using generation-based CAS.

    Contract:
      - final_meta: latest metadata after merging any remote state read during the attempt.
      - success: whether the final merged metadata reached GCS.
      - observed_generation: last observed object generation (None when the object is absent).
      - error / error_kind: describe the last failure when success is False.

    Failure handling:
      - PreconditionFailed (or 412 in older/test envs): re-read remote, merge, and retry CAS.
      - Transient Google API errors: backoff and retry.
      - Permanent / unknown errors: fail explicitly.

    On non-final failure, the caller can safely persist final_meta locally without
    regressing to a stale pre-merge snapshot.
    """
    if not _gcs_enabled():
        return BatchMetaGCSResult(True, deepcopy(meta), generation, None, None)

    attempts = BATCH_META_GCS_RETRY_ATTEMPTS if final else BATCH_META_GCS_NONFINAL_ATTEMPTS
    last_error: Optional[Exception] = None
    error_kind: Optional[str] = None
    working_meta = deepcopy(meta)
    remote_meta: Optional[dict] = None
    remote_generation = generation

    if remote_generation is None:
        for attempt in range(1, attempts + 1):
            try:
                remote_meta, remote_generation = _read_batch_meta_record_from_gcs(
                    batch_id,
                    cache_local=False,
                    strict=True,
                )
                break
            except BatchMetaPersistenceError as exc:
                last_error = exc
                error_kind = "read_error"
                if _is_transient_gcs_error(exc) and attempt < attempts:
                    logger.warning(
                        "No se pudo leer metadata remota del batch %s desde GCS (intento %s/%s): %s",
                        batch_id,
                        attempt,
                        attempts,
                        exc,
                    )
                    time.sleep(min(BATCH_META_GCS_MAX_DELAY_SECONDS, BATCH_META_GCS_RETRY_DELAY_SECONDS * attempt))
                    continue
                if final:
                    raise BatchMetaPersistenceError(
                        f"No se pudo leer metadata remota del batch {batch_id} en GCS"
                    ) from exc
                return BatchMetaGCSResult(False, deepcopy(working_meta), remote_generation, exc, error_kind)

    if remote_meta is not None:
        working_meta = _merge_batch_meta(working_meta, remote_meta)
    else:
        working_meta = _merge_batch_meta(working_meta, {})
    if remote_generation is None:
        remote_generation = 0

    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(_batch_meta_object_name(batch_id))

    if _GCS_PRECONDITION_FAILED is not None:
        def _attempt_upload(payload: str, expected_generation: int) -> tuple[str, Optional[Exception]]:
            try:
                blob.upload_from_string(
                    payload,
                    content_type="application/json; charset=utf-8",
                    if_generation_match=expected_generation or 0,
                )
                return "success", None
            except _GCS_PRECONDITION_FAILED as exc:
                return "precondition", exc
            except _GCS_TRANSIENT_EXC_TYPES as exc:
                return "transient", exc
            except Exception as exc:
                if _is_transient_gcs_error(exc):
                    return "transient", exc
                if _is_permanent_gcs_error(exc):
                    return "permanent", exc
                return "unknown", exc
    else:
        def _attempt_upload(payload: str, expected_generation: int) -> tuple[str, Optional[Exception]]:
            try:
                blob.upload_from_string(
                    payload,
                    content_type="application/json; charset=utf-8",
                    if_generation_match=expected_generation or 0,
                )
                return "success", None
            except _GCS_TRANSIENT_EXC_TYPES as exc:
                return "transient", exc
            except Exception as exc:
                if _is_precondition_failure(exc):
                    return "precondition", exc
                if _is_transient_gcs_error(exc):
                    return "transient", exc
                if _is_permanent_gcs_error(exc):
                    return "permanent", exc
                return "unknown", exc

    payload = json.dumps(working_meta, ensure_ascii=False, indent=2)
    for attempt in range(1, attempts + 1):
        status, exc = _attempt_upload(payload, remote_generation)
        if status == "success":
            return BatchMetaGCSResult(True, deepcopy(working_meta), remote_generation, None, None)

        last_error = exc
        error_kind = status

        if status == "precondition":
            try:
                remote_meta, remote_generation = _read_batch_meta_record_from_gcs(
                    batch_id,
                    cache_local=False,
                    strict=True,
                )
            except BatchMetaPersistenceError as read_exc:
                last_error = read_exc
                error_kind = "read_error"
                logger.exception(
                    "No se pudo releer metadata remota del batch %s tras un conflicto CAS",
                    batch_id,
                )
                if final:
                    raise BatchMetaPersistenceError(
                        f"No se pudo releer metadata remota del batch {batch_id} tras un conflicto CAS"
                    ) from read_exc
                return BatchMetaGCSResult(False, deepcopy(working_meta), remote_generation, read_exc, error_kind)

            if remote_meta is not None:
                working_meta = _merge_batch_meta(working_meta, remote_meta)
            else:
                working_meta = _merge_batch_meta(working_meta, {})
            remote_generation = remote_generation or 0
            payload = json.dumps(working_meta, ensure_ascii=False, indent=2)
            logger.warning(
                "Conflicto de precondición al persistir metadata del batch %s; reintentando con la generación %s",
                batch_id,
                remote_generation,
            )
            continue

        if status == "transient":
            logger.exception(
                "No se pudo persistir metadata del batch %s en GCS (intento %s/%s)",
                batch_id,
                attempt,
                attempts,
            )
            if attempt < attempts:
                time.sleep(min(BATCH_META_GCS_MAX_DELAY_SECONDS, BATCH_META_GCS_RETRY_DELAY_SECONDS * attempt))
                continue
            if final:
                raise BatchMetaPersistenceError(
                    f"No se pudo persistir la metadata final del batch {batch_id} en GCS"
                ) from exc
            return BatchMetaGCSResult(False, deepcopy(working_meta), remote_generation, exc, error_kind)

        logger.exception(
            "No se pudo persistir metadata del batch %s en GCS (error %s)",
            batch_id,
            status,
        )
        if final:
            raise BatchMetaPersistenceError(
                f"No se pudo persistir metadata del batch {batch_id} en GCS"
            ) from exc
        return BatchMetaGCSResult(False, deepcopy(working_meta), remote_generation, exc, error_kind)

    if final:
        raise BatchMetaPersistenceError(
            f"No se pudo persistir la metadata final del batch {batch_id} en GCS"
        ) from last_error
    return BatchMetaGCSResult(False, deepcopy(working_meta), remote_generation, last_error, error_kind)


def _load_batch_meta(batch_id: str) -> dict:
    path = _batch_meta_path(batch_id)
    meta = _load_batch_meta_from_disk(batch_id)
    if meta is not None:
        return meta
    meta = _load_batch_meta_from_gcs(batch_id)
    if meta is not None:
        return meta
    raise HTTPException(status_code=404, detail="Batch no existe o expiró.")


def _load_batch_meta_latest(batch_id: str, *, cache_local: bool = False) -> dict:
    local_meta = _load_batch_meta_from_disk(batch_id)
    gcs_meta = _read_batch_meta_from_gcs(batch_id, cache_local=cache_local)
    candidates = [meta for meta in (local_meta, gcs_meta) if meta is not None]
    if not candidates:
        raise HTTPException(status_code=404, detail="Batch no existe o expiró.")
    return max(candidates, key=_meta_revision)


def _persist_batch_meta_to_disk(batch_id: str, meta: dict) -> None:
    os.makedirs(_batch_dir(batch_id), exist_ok=True)
    path = _batch_meta_path(batch_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _save_batch_meta(batch_id: str, meta: dict, *, final: bool = False, verify: bool = False) -> dict:
    os.makedirs(_batch_dir(batch_id), exist_ok=True)
    if final:
        verify = True

    gcs_result = _save_batch_meta_to_gcs(batch_id, meta, final=final)
    persisted = deepcopy(gcs_result.final_meta)

    if not gcs_result.success and gcs_result.error is not None:
        sync_error = f"No se pudo persistir metadata del batch {batch_id} en GCS: {gcs_result.error}"
        persisted[BATCH_META_SYNC_ERROR_FIELD] = sync_error
        logger.warning(sync_error)

    _persist_batch_meta_to_disk(batch_id, persisted)

    if final and gcs_result.success and verify:
        verified, _ = _read_batch_meta_record_from_gcs(batch_id, cache_local=False, strict=True)
        if verified is None or _meta_revision(verified) != _meta_revision(persisted) or verified != persisted:
            message = (
                f"Verificación final de metadata falló para batch {batch_id}: "
                f"esperaba revisión {persisted[BATCH_META_REVISION_FIELD]}"
            )
            logger.error(message)
            persisted[BATCH_META_SYNC_ERROR_FIELD] = message
            meta.clear()
            meta.update(persisted)
            raise BatchMetaVerificationError(message)

    meta.clear()
    meta.update(persisted)
    return persisted


def _live_elapsed_seconds(
started_at, elapsed_seconds, status):
    if elapsed_seconds is not None and isinstance(elapsed_seconds, (int, float)):
        return elapsed_seconds
    if status in {"processing", "cancelling"} and started_at:
        return round(time.time() - started_at, 3)
    return elapsed_seconds


def _gcs_enabled() -> bool:
    return bool(GCS_BUCKET)


def _gcs_client() -> storage.Client:
    return storage.Client()


def _normalize_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _safe_object_name(name: str) -> str:
    base = os.path.basename(name or "batch.zip")
    base = base.replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def _parse_gcs_path(path: str) -> Tuple[str, str]:
    if path.startswith("gs://"):
        parts = path[5:].split("/", 1)
        bucket = parts[0]
        obj = parts[1] if len(parts) > 1 else ""
        return bucket, obj
    return GCS_BUCKET, path.lstrip("/")


def _delete_gcs_object(gcs_path: str) -> None:
    if not gcs_path or not _gcs_enabled():
        return
    bucket_name, object_name = _parse_gcs_path(gcs_path)
    if not bucket_name or not object_name:
        return
    try:
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        if blob.exists():
            blob.delete()
    except Exception:
        # Best-effort cleanup; do not fail batch creation
        return


def _persist_batch_source_zip(batch_id: str, zip_path: str) -> Optional[str]:
    if not _gcs_enabled() or not os.path.exists(zip_path):
        return None
    try:
        client = _gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        object_name = f"{_normalize_prefix(GCS_RESULTS_PREFIX)}{batch_id}/source.zip"
        blob = bucket.blob(object_name)
        blob.upload_from_filename(zip_path, content_type="application/zip")
        return f"gs://{GCS_BUCKET}/{object_name}"
    except Exception:
        return None


def _cleanup_gcs_results(max_age_minutes: int) -> int:
    if not _gcs_enabled():
        return 0
    age_minutes = max(1, int(max_age_minutes))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)
    prefix = _normalize_prefix(GCS_RESULTS_PREFIX)
    deleted = 0
    for blob in client.list_blobs(bucket, prefix=prefix):
        updated = blob.updated or blob.time_created
        if not updated:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated <= cutoff:
            try:
                blob.delete()
                deleted += 1
            except Exception:
                continue
    return deleted


def _get_signer_email(credentials) -> str:
    if GCS_SIGNER_EMAIL:
        return GCS_SIGNER_EMAIL
    return getattr(credentials, "service_account_email", "") or ""


def _signed_url(
    blob: storage.Blob,
    method: str,
    content_type: Optional[str] = None,
    download_name: Optional[str] = None,
) -> str:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not credentials.valid:
        credentials.refresh(Request())
    signer_email = _get_signer_email(credentials)
    if not signer_email:
        raise HTTPException(status_code=500, detail="GCS signer email no configurado.")
    disposition = None
    if download_name:
        disposition = f'attachment; filename="{download_name}"'
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=GCS_SIGNED_URL_EXP_SECONDS),
        method=method,
        content_type=content_type,
        response_disposition=disposition,
        service_account_email=signer_email,
        access_token=credentials.token,
    )


def _generate_upload_url(object_name: str, content_type: str = "application/zip") -> str:
    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    return _signed_url(blob, method="PUT", content_type=content_type)


def _generate_download_url(object_name: str, download_name: Optional[str] = None) -> str:
    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    return _signed_url(blob, method="GET", download_name=download_name)


def _restore_batch_input_from_gcs(batch_id: str, source_gcs_path: str) -> None:
    if not _gcs_enabled():
        raise HTTPException(status_code=400, detail="GCS no está configurado en el servidor.")
    bucket_name, object_name = _parse_gcs_path(source_gcs_path)
    if bucket_name != GCS_BUCKET or not object_name:
        raise HTTPException(status_code=400, detail="Objeto GCS inválido.")

    client = _gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    if not blob.exists():
        raise HTTPException(status_code=404, detail="Objeto no encontrado en GCS.")

    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)
    zip_path = os.path.join(bdir, "batch.zip")
    input_dir = os.path.join(bdir, "input")
    if os.path.exists(input_dir):
        shutil.rmtree(input_dir, ignore_errors=True)
    os.makedirs(input_dir, exist_ok=True)

    blob.download_to_filename(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        _safe_extract_zip(zf, input_dir)


def _reconcile_batch_meta(batch_id: str, meta: dict, *, persist: bool = True) -> dict:
    changed = False
    stale_found = False
    now = time.time()
    if meta.get("status") in {"processing", "cancelling"}:
        for pkg in meta.get("packages", []):
            if pkg.get("status") != "processing":
                continue
            heartbeat = pkg.get("lastHeartbeatAt") or pkg.get("startedAt") or meta.get("startedAt")
            if heartbeat and (now - float(heartbeat)) > BATCH_STALE_SECONDS:
                stage = pkg.get("currentStage") or (pkg.get("audit") or {}).get("stage") or "desconocida"
                pkg["status"] = "error"
                pkg["finishedAt"] = now
                pkg["elapsedSeconds"] = round(now - (pkg.get("startedAt") or now), 3)
                pkg["error"] = (
                    f"Paquete quedó sin avance por más de {BATCH_STALE_SECONDS}s "
                    f"en etapa {stage}. Posible reinicio o crash del backend."
                )
                pkg["currentStage"] = "stale_error"
                pkg["lastHeartbeatAt"] = now
                changed = True
                stale_found = True

        if stale_found:
            for pkg in meta.get("packages", []):
                if pkg.get("status") == "pending":
                    pkg["status"] = "error"
                    pkg["finishedAt"] = now
                    pkg["elapsedSeconds"] = None
                    pkg["error"] = "Paquete no procesado porque el worker del lote se detuvo antes de iniciarlo. Usa Reintentar errores."
                    pkg["currentStage"] = "not_started_after_stale"
                    pkg["lastHeartbeatAt"] = now
                    changed = True

    results_dir = os.path.join(_batch_dir(batch_id), "results")
    if os.path.isdir(results_dir):
        for pkg in meta.get("packages", []):
            if pkg.get("status") == "done":
                continue
            result_file = pkg.get("resultFile") or f"{pkg.get('name')}.zip"
            result_path = os.path.join(results_dir, result_file)
            if os.path.exists(result_path):
                pkg["resultFile"] = result_file
                pkg["status"] = "done"
                pkg["error"] = None
                changed = True

        all_path = os.path.join(results_dir, "all.zip")
        if os.path.exists(all_path) and meta.get("allZip") != "all.zip":
            meta["allZip"] = "all.zip"
            changed = True

    if changed:
        done_count = sum(1 for p in meta.get("packages", []) if p.get("status") == "done")
        error_count = sum(1 for p in meta.get("packages", []) if p.get("status") == "error")
        pending_count = sum(
            1 for p in meta.get("packages", []) if p.get("status") in {"pending", "processing"}
        )
        current_status = meta.get("status")
        if current_status not in _BATCH_TERMINAL_STATUSES | {"cancelling"}:
            if pending_count:
                meta["status"] = "processing"
            elif error_count and done_count:
                meta["status"] = "partial"
            elif error_count and not done_count:
                meta["status"] = "error"
            elif done_count:
                meta["status"] = "done"
            else:
                meta["status"] = current_status or "pending"
        if persist:
            _save_batch_meta(batch_id, meta)
    return meta


def _cleanup_expired_jobs() -> None:
    now = time.time()
    for name in os.listdir(JOB_ROOT):
        if not _JOB_ID_RE.fullmatch(name or ""):
            continue
        jdir = os.path.join(JOB_ROOT, name)
        meta_path = os.path.join(jdir, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            created_at = float(meta.get("createdAt", 0))
        except Exception:
            created_at = 0
        if created_at and (now - created_at) > JOB_TTL_SECONDS:
            shutil.rmtree(jdir, ignore_errors=True)


async def _save_upload_file_limited(uf: UploadFile, dest_path: str, max_bytes: int) -> None:
    total = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = await uf.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out.close()
                try:
                    os.remove(dest_path)
                except FileNotFoundError:
                    pass
                raise HTTPException(status_code=413, detail="Archivo demasiado grande.")
            out.write(chunk)
    await uf.close()


def _is_probably_pdf(uf: UploadFile) -> bool:
    name = (uf.filename or "").lower()
    if not name.endswith(".pdf"):
        return False
    ctype = (uf.content_type or "").lower()
    if ctype and "pdf" not in ctype:
        return False
    return True


def _safe_extract_zip(zf: zipfile.ZipFile, dest_dir: str) -> None:
    for member in zf.infolist():
        name = member.filename
        if not name or name.endswith("/"):
            continue
        norm = os.path.normpath(name)
        if norm.startswith("..") or os.path.isabs(norm):
            raise HTTPException(status_code=400, detail="ZIP inválido: rutas inseguras.")
        target = os.path.join(dest_dir, norm)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _collect_pdf_paths(root: str) -> List[str]:
    pdfs: List[str] = []
    for base, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".pdf"):
                pdfs.append(os.path.join(base, name))
    return sorted(pdfs)


def _rewrite_pdf_structure_inplace(path: str) -> None:
    """
    Reescribe el PDF para normalizar estructura interna (xref/objetos).
    Esto reduce errores de insercion de paginas en lotes con PDFs escaneados.
    """
    if not PDF_REWRITE_ENABLED:
        return
    tmp_path = f"{path}.normalized.pdf"
    doc: Optional[fitz.Document] = None
    try:
        doc = fitz.open(path)
        doc.save(tmp_path, garbage=4, deflate=True, clean=True)
        doc.close()
        doc = None
        # Validar que el PDF normalizado abre correctamente antes de reemplazar.
        check = fitz.open(tmp_path)
        check.close()
        os.replace(tmp_path, path)
    except Exception:
        # Fallback seguro: conservar original si falla la normalizacion.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    finally:
        if doc is not None:
            doc.close()


def _create_job_from_pdf_paths(pdf_paths: List[str]) -> Tuple[str, int]:
    if not pdf_paths:
        raise HTTPException(status_code=400, detail="Paquete sin PDFs.")
    if len(pdf_paths) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"Máximo {MAX_FILES} PDFs por paquete.")

    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    os.makedirs(os.path.join(jdir, "pdfs"), exist_ok=True)
    os.makedirs(os.path.join(jdir, "cache"), exist_ok=True)

    page_map: List[List[int]] = []
    total_pages = 0

    try:
        for i, path in enumerate(pdf_paths):
            if not path.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail=f"Archivo no PDF: {os.path.basename(path)}")
            if os.path.getsize(path) > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail="Archivo demasiado grande.")

            src_path = os.path.join(jdir, "pdfs", f"src_{i}.pdf")
            shutil.copyfile(path, src_path)
            _rewrite_pdf_structure_inplace(src_path)

            try:
                doc = fitz.open(src_path)
            except Exception:
                raise HTTPException(status_code=400, detail=f"PDF inválido o corrupto: {os.path.basename(path)}")

            for p in range(doc.page_count):
                page_map.append([i, p])
            total_pages += doc.page_count
            doc.close()

        meta = {
            "jobId": job_id,
            "files": len(pdf_paths),
            "totalPages": total_pages,
            "page_map": page_map,
            "createdAt": time.time(),
        }
        _save_meta(job_id, meta)
    except Exception:
        shutil.rmtree(jdir, ignore_errors=True)
        raise

    return job_id, total_pages


def _render_page_image(doc: fitz.Document, page_index: int, width: int) -> bytes:
    page = doc.load_page(page_index)
    # Escala para aproximar ancho deseado
    rect = page.rect
    zoom = width / rect.width
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix.tobytes("png")


def _normalize_nit(nit_raw: str) -> str:
    """
    Recibe cosas como:
      - '900204617-5'
      - '900.204.617 - 5'
      - '900204617'
    y devuelve SOLO el NIT base:
      - '900204617'
    """
    s = (nit_raw or "").strip().upper()

    # Quita puntos, comas y espacios
    s = s.replace(".", "").replace(",", "").replace(" ", "")

    # Si viene con DV (ej: 900204617-5), toma solo lo anterior al guion
    if "-" in s:
        s = s.split("-")[0]

    # Deja solo dígitos
    s = "".join(ch for ch in s if ch.isdigit())
    return s


def _normalize_invoice_code(code_raw: str) -> Optional[str]:
    s = (code_raw or "").strip().upper().replace(" ", "")
    if not s:
        return None
    if s.isdigit():
        return f"OCFE{s}"
    # OCFE or other prefix (ECUC, etc.) + digits
    m = re.search(r"\b([A-Z]{3,6})\s*(\d{3,})\b", s)
    if not m:
        return None
    prefix = m.group(1)
    if prefix in {"NIT", "CUDE"}:
        return None
    digits = re.sub(r"\D", "", m.group(2))
    if not digits:
        return None
    return f"{prefix}{digits}"


def _normalize_service(service_raw: Optional[str]) -> str:
    s = (service_raw or "cuidador").strip().lower()
    return s if s in SERVICE_IDS else "cuidador"


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _page_kind(text: str) -> str:
    upper = _strip_accents(text or "").upper()
    if any(h in upper for h in _FEV_HINTS):
        return "fev"
    if any(h in upper for h in _NC_HINTS):
        return "nc"
    return "other"


def _normalize_ocr_text(text: str) -> str:
    return _strip_accents(text or "").upper()


def _has_crc_table_hint(text: str) -> bool:
    if not text:
        return False
    t = _normalize_ocr_text(text)
    dates = len(_DATE_RE.findall(t))
    times = len(_TIME_RE.findall(t))
    has_cuidador = "ATENCION CUIDADOR" in t or "CUIDADOR" in t
    has_fecha_creacion = "FECHA CREACION" in t
    header_keys = ("FECHA", "HORA", "TURNO", "SERVICIO", "PRESTADOR", "NOMBRE", "TUTOR", "PACIENTE", "FIRMA")
    header_hits = sum(1 for k in header_keys if k in t)
    fallback_rows = has_cuidador and dates >= 2 and times >= 2 and not has_fecha_creacion
    fallback_headers = has_cuidador and header_hits >= 5 and not has_fecha_creacion

    if (
        "SERVICIO" in t
        and "PRESTADOR" in t
        and ("TURNO" in t and ("HORA" in t or "HORARIO" in t))
        and ("NOMBRE" in t and ("TUTOR" in t or "PACIENTE" in t))
        and "FIRMA" in t
        and ("N." in t or "N°" in t or "NO." in t or "NRO" in t)
        and ("ATENCION CUIDADOR" in t or "CUIDADOR" in t)
    ):
        return True

    return fallback_headers or fallback_rows


def _looks_like_otros_servicios_pde(t: str) -> bool:
    if not t:
        return False
    has_authorization_header = (
        "AUTORIZACION DE SERVICIOS" in t
        or "AUTORIZACION SERVICIOS" in t
    )
    has_order_number = (
        "NUMERO DE ORDEN" in t
        or "NUMERO ORDEN" in t
        or "NRO DE ORDEN" in t
        or "NO. ORDEN" in t
    )
    has_fomag_header = "FOMAG" in t or "FONDO NACIONAL DE PRESTACIONES SOCIALES DEL MAGISTERIO" in t
    has_provider_block = "NOMBRE PRESTADOR" in t and ("COD HABILITACION" in t or "DIAGNOSTICO DX" in t)
    has_signature_block = (
        ("FIRMA DEL MEDICO QUE ORDENA" in t and "FIRMA DEL USUARIO" in t)
        or "FIRMA DE QUIEN TRANSCRIBE" in t
    )
    has_network_fields = "IPS PRIMARIA" in t or "GESTION DE RED" in t
    has_authorization_metadata = (
        "NO. SOLICITUD" in t
        or "NO. AUTORIZACION" in t
        or "CODIGO EPS" in t
    )
    has_patient_context = (
        "AFILIADO" in t
        or "UBICACION DEL PACIENTE" in t
        or "DX:" in t
        or "DX " in t
    )
    has_request_flow = (
        "SOLICITADO POR" in t
        or "ORDENADO POR" in t
        or "REMITIDO A" in t
    )
    has_service_table = (
        "CODIGO" in t
        and "DESCRIPCION" in t
        and ("CANT" in t or "CANTIDAD" in t)
    )

    signals = sum(
        [
            1 if has_fomag_header else 0,
            1 if has_provider_block else 0,
            1 if has_signature_block else 0,
            1 if has_network_fields else 0,
        ]
    )
    authorization_signals = sum(
        [
            1 if has_authorization_metadata else 0,
            1 if has_patient_context else 0,
            1 if has_request_flow else 0,
            1 if has_service_table else 0,
        ]
    )

    if has_authorization_header and authorization_signals >= 2:
        return True

    return has_order_number and signals >= 2


def _looks_like_otros_servicios_crc_terapias(t: str) -> bool:
    if not t:
        return False

    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", t)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    has_registro_individual = bool(re.search(r"REGISTR[O0]\s+INDIVID", cleaned))
    has_prestacion_servicios = "PRESTAC" in cleaned and "SERVICI" in cleaned
    has_terapia_context = "TERAPI" in cleaned or "APOYO TERAPEUT" in cleaned
    has_terapia_fields = "TIPO DE TERAPIA" in cleaned or "GAT REH" in cleaned
    has_control_table = (
        "SESION" in cleaned
        and "FIRMA" in cleaned
        and ("DOCUMENTO" in cleaned or "N DOCUMENTO" in cleaned or "NRO DOCUMENTO" in cleaned)
    )

    score = sum(
        [
            1 if has_registro_individual else 0,
            1 if has_prestacion_servicios else 0,
            1 if has_terapia_context else 0,
            1 if has_terapia_fields else 0,
            1 if has_control_table else 0,
        ]
    )
    if has_registro_individual and has_terapia_context and (has_prestacion_servicios or has_control_table):
        return True
    return has_terapia_context and score >= 3


def _looks_like_otros_servicios_crc_medicamentos_header(t: str) -> bool:
    if not t:
        return False

    has_med_admin_header = "ADMINISTRACION DE MEDICAMENTOS" in t
    has_patient_block = (
        "DOCUMENTO" in t
        and "NOMBRE" in t
        and ("TIPO DE USUARIO" in t or "ERP" in t or "FECHA DE NACIMIENTO" in t)
    )
    has_med_table = (
        "FECHA" in t
        and "HORA" in t
        and "MEDICAMENTO" in t
        and "DOSIS" in t
        and "FRECUENCIA" in t
    )
    has_pharma_context = (
        "FORMA FARMACEUTICA" in t
        or "VIA" in t
        or "VIA INTRAVENOSA" in t
        or "VIA INTRAMUSCULAR" in t
    )
    has_execution_trace = (
        "PRESTO EL SERVICIO" in t
        or "PRESTÓ EL SERVICIO" in t
        or ("N." in t and "FIRMA" in t)
    )

    signals = sum(
        [
            1 if has_patient_block else 0,
            1 if has_med_table else 0,
            1 if has_pharma_context else 0,
            1 if has_execution_trace else 0,
        ]
    )
    return has_med_admin_header and signals >= 2


def _looks_like_otros_servicios_crc_medicamentos_continuation(t: str) -> bool:
    if not t:
        return False

    has_sequence_column = "N." in t or "NO." in t or "NO " in t or "NO\t" in t
    has_datetime_columns = "FECHA" in t and "HORA" in t
    has_med_table = (
        "MEDICAMENTO" in t
        and "FORMA FARMACEUTICA" in t
        and "DOSIS" in t
        and "FRECUENCIA" in t
    )
    has_route_and_execution = (
        "VIA" in t
        and ("PRESTO EL SERVICIO" in t or "PRESTÓ EL SERVICIO" in t)
    )
    has_admin_context = (
        "SONDA DE GASTROSTOMIA" in t
        or "CAPSULA" in t
        or "CAPSULA BLANDA" in t
        or "TABLETAS" in t
        or "PASTA" in t
        or "OTRAS SOLUCIONES" in t
        or "MG" in t
        or "ML" in t
        or "GOTAS" in t
    )
    has_sequence_rows = bool(
        re.search(r"\b(?:[1-9]|[1-4][0-9]|5[0-9])\b", t)
    )

    score = sum(
        [
            1 if has_sequence_column else 0,
            1 if has_datetime_columns else 0,
            1 if has_med_table else 0,
            1 if has_route_and_execution else 0,
            1 if has_admin_context else 0,
            1 if has_sequence_rows else 0,
        ]
    )

    return has_datetime_columns and has_med_table and has_route_and_execution and score >= 5


def _looks_like_otros_servicios_crc_medicamentos(t: str) -> bool:
    return (
        _looks_like_otros_servicios_crc_medicamentos_header(t)
        or _looks_like_otros_servicios_crc_medicamentos_continuation(t)
    )


def _looks_like_otros_servicios_hev_nota_enfermeria(t: str) -> bool:
    if not t:
        return False

    has_header = "NOTA DE ENFERMERIA" in t
    has_sequence_column = "N." in t or "NO." in t or "NO " in t or "NO\t" in t
    has_datetime_columns = "FECHA" in t and "HORA" in t
    has_staff_columns = "PRESTADOR" in t and "FIRMA PRESTADOR" in t
    has_nursing_context = (
        "AUXILIAR DE ENFERMERIA" in t
        or "ENFERMERIA" in t
        or "ENFERMERA" in t
    )
    narrative_hits = sum(
        [
            1 if "PACIENTE" in t else 0,
            1 if "SE REALIZA" in t else 0,
            1 if "SE ADMINISTRA" in t else 0,
            1 if "SE OBSERVA" in t else 0,
            1 if "LLEGA AL DOMICILIO" in t else 0,
            1 if "SIN COMPLICACIONES" in t else 0,
            1 if "AUXILIAR DE ENFERMERIA" in t else 0,
        ]
    )

    if has_header and has_datetime_columns and has_staff_columns:
        return True

    return (
        has_sequence_column
        and has_datetime_columns
        and has_staff_columns
        and has_nursing_context
        and narrative_hits >= 2
    )


def _looks_like_otros_servicios_crc_signos_vitales_header(t: str) -> bool:
    if not t:
        return False

    has_header = (
        "REGISTRO SIGNOS VITALES" in t
        or "REGISTRO DE SIGNOS VITALES" in t
    )
    has_patient_block = (
        "DOCUMENTO" in t
        and "NOMBRE" in t
        and ("ERP" in t or "REGIMEN CONTRIBUTIVO" in t or "REGIMEN SUBSIDIADO" in t)
    )
    has_datetime_columns = "FECHA" in t and "HORA" in t
    has_pressure_block = (
        "TA" in t
        and "SISTOLICA" in t
        and "DIASTOLICA" in t
    )
    has_vital_signs_columns = sum(
        [
            1 if "F.C" in t or "FC" in t else 0,
            1 if "F.R" in t or "FR" in t else 0,
            1 if "SPO2" in t else 0,
            1 if "GLUCOMETRIA" in t else 0,
            1 if "T " in t or " T." in t or " T°" in t else 0,
        ]
    ) >= 3
    has_sampling_context = "LUGAR DE TOMA" in t or "MSD BRAZO" in t or "MSI BRAZO" in t

    score = sum(
        [
            1 if has_header else 0,
            1 if has_patient_block else 0,
            1 if has_datetime_columns else 0,
            1 if has_pressure_block else 0,
            1 if has_vital_signs_columns else 0,
            1 if has_sampling_context else 0,
        ]
    )

    if has_header and has_datetime_columns and has_pressure_block and has_vital_signs_columns:
        return True
    return score >= 5


def _looks_like_otros_servicios_crc_signos_vitales_continuation(t: str) -> bool:
    if not t:
        return False

    has_no_column = "NO." in t or "NO " in t or " N0 " in t or "NO\t" in t
    has_table_columns = (
        "FECHA" in t
        and "HORA" in t
        and "LUGAR DE TOMA" in t
    )
    has_pressure_block = "SISTOLICA" in t and "DIASTOLICA" in t
    has_vital_signs_columns = sum(
        [
            1 if "F.C" in t or "FC" in t else 0,
            1 if "F.R" in t or "FR" in t else 0,
            1 if "SPO2" in t else 0,
            1 if "GLUCOMETRIA" in t else 0,
            1 if "T " in t or " T." in t or " T°" in t else 0,
        ]
    ) >= 3
    has_measurement_context = (
        "MSD BRAZO" in t
        or "MSI BRAZO" in t
        or "BRAZO DERECHO" in t
        or "BRAZO IZQUIERDO" in t
        or "CONVENCIONES" in t
    )
    has_sequence_rows = bool(
        re.search(r"\b(?:[1-9]|[1-4][0-9]|5[0-9])\b", t)
    )

    score = sum(
        [
            1 if has_no_column else 0,
            1 if has_table_columns else 0,
            1 if has_pressure_block else 0,
            1 if has_vital_signs_columns else 0,
            1 if has_measurement_context else 0,
            1 if has_sequence_rows else 0,
        ]
    )

    return has_table_columns and has_pressure_block and has_vital_signs_columns and score >= 5


def _looks_like_otros_servicios_crc_signos_vitales(t: str) -> bool:
    return (
        _looks_like_otros_servicios_crc_signos_vitales_header(t)
        or _looks_like_otros_servicios_crc_signos_vitales_continuation(t)
    )


def _classify_text(
    text: str,
    allow_crc_table: bool = False,
    service: str = "cuidador",
) -> Optional[str]:
    if not text:
        return None
    service = _normalize_service(service)
    t = _normalize_ocr_text(text)
    has_opf_decisiones = bool(re.search(r"\bORDEN\s+MEDICA\s*\(?DECISIONES\)?\b", t))
    has_hev_social_hint = (
        "REGISTRO DE ACTIVIDADES DE CUIDADO" in t
        or "REGISTRO DE ACTIVIDADES DE CUIDADOR" in t
        or "TRABAJO SOCIAL" in t
    )
    has_historia_hint = "HISTORIA CLINICA" in t or "HISTORIA CLÍNICA" in t
    # Regla de negocio: "ORDEN MEDICA (DECISIONES)" siempre va a OPF.
    if has_opf_decisiones:
        return "OPF"
    # En otros servicios, frases como "TRABAJO SOCIAL" pueden aparecer dentro de
    # facturas, autorizaciones o registros CRC válidos. Dejamos que esas reglas
    # más específicas se evalúen primero para no forzar HEV demasiado pronto.
    if has_hev_social_hint and service != "otros_servicios":
        return "HEV"
    # Excepcion de negocio: "CERTIFICACION DETALLE DE CARGOS" se clasifica como HEV.
    if "CERTIFICACION DETALLE DE CARGOS" in t or "CERTIFICACION DEL DETALLE DE CARGOS" in t:
        return "HEV"
    # Regla de negocio: OPF solo aplica si el texto contiene "ORDEN MEDICA".
    has_opf_phrase = bool(re.search(r"\bORDEN\s+MEDICA\b", t))
    opf_context = (
        "ORDEN MEDICA (DECISIONES)" in t
        or "ORDEN MÉDICA (DECISIONES)" in t
        or "DIAGNOSTICO PRINCIPAL" in t
        or "DIAGNOSTICOS SECUNDARIOS" in t
        or "MES INICIO" in t
    )
    if has_opf_phrase:
        if has_historia_hint and not opf_context:
            return "HEV"
        return "OPF"
    if has_historia_hint:
        return "HEV"
    if service == "otros_servicios":
        if _looks_like_otros_servicios_hev_nota_enfermeria(t):
            return "HEV"
        if (
            _looks_like_otros_servicios_crc_terapias(t)
            or _looks_like_otros_servicios_crc_medicamentos(t)
            or _looks_like_otros_servicios_crc_signos_vitales(t)
        ):
            return "CRC"
        for cat, patterns in _AUTO_RULES_FIXED:
            for p in patterns:
                if p in t:
                    return cat
        return "HEV"
    for cat, patterns in _AUTO_RULES_STRONG:
        for p in patterns:
            if p in t:
                return cat
    if allow_crc_table and _has_crc_table_hint(t):
        return "CRC"
    return None


def _text_is_useful(text: str) -> bool:
    return bool(text and len(text.strip()) >= OCR_MIN_TEXT_LEN)


def _extract_page_text(job_id: str, page_index: int) -> str:
    cache_txt = os.path.join(_job_dir(job_id), "cache", f"text_{page_index}.txt")
    if os.path.exists(cache_txt):
        with open(cache_txt, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    meta = _load_meta(job_id)
    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    try:
        page = doc.load_page(src_page)
        text = page.get_text("text") or ""
    finally:
        doc.close()

    try:
        with open(cache_txt, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    return text


def _ocr_cache_paths(job_id: str, page_index: int, suffix: str = "") -> Tuple[str, str]:
    base = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}{suffix}")
    return f"{base}.txt", f"{base}.png"


def _ocr_page_text(job_id: str, page_index: int, header_only: bool = False) -> str:
    if not OCR_ENABLED:
        return ""
    suffix = "_head" if header_only else ""
    cache_txt, img_path = _ocr_cache_paths(job_id, page_index, suffix)
    if os.path.exists(cache_txt):
        with open(cache_txt, "r", encoding="utf-8") as f:
            return f.read()

    meta = _load_meta(job_id)
    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    try:
        page = doc.load_page(src_page)
        dpi = OCR_HEADER_DPI if header_only else OCR_DPI
        zoom = dpi / 72.0
        if header_only:
            rect = page.rect
            header_h = max(1.0, rect.height * OCR_HEADER_RATIO)
            clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + header_h)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, clip=clip)
        else:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(img_path)
    finally:
        doc.close()

    out_base = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}{suffix}")
    cmd = ["tesseract", img_path, out_base, "-l", OCR_LANG, "--psm", str(OCR_PSM)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=OCR_PAGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"OCR timeout en página {page_index + 1} después de {OCR_PAGE_TIMEOUT_SECONDS}s")
    if res.returncode != 0 and OCR_LANG != "eng":
        cmd = ["tesseract", img_path, out_base, "-l", "eng", "--psm", str(OCR_PSM)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=OCR_PAGE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"OCR timeout en página {page_index + 1} después de {OCR_PAGE_TIMEOUT_SECONDS}s")

    text = ""
    if os.path.exists(cache_txt):
        with open(cache_txt, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

    if not OCR_KEEP_IMAGES and os.path.exists(img_path):
        try:
            os.remove(img_path)
        except OSError:
            pass

    return text

def _page_text_for_classification(
    job_id: str,
    page_index: int,
    cancel_check: Optional[callable] = None,
    service: str = "cuidador",
) -> str:
    if cancel_check and cancel_check():
        raise RuntimeError("batch_cancelled")
    # 1) Texto embebido del PDF (rápido)
    text = _extract_page_text(job_id, page_index)
    if _text_is_useful(text):
        embedded_classification = _classify_text(text, allow_crc_table=True, service=service)
        if embedded_classification and not (
            _normalize_service(service) == "otros_servicios" and embedded_classification == "HEV"
        ):
            return text
        if cancel_check and cancel_check():
            raise RuntimeError("batch_cancelled")
        header_text = _ocr_page_text(job_id, page_index, header_only=True)
        header_classification = _classify_text(header_text, allow_crc_table=False, service=service)
        if header_classification and (embedded_classification != "HEV" or header_classification != "HEV"):
            return header_text
        return text

    # 2) OCR de cabecera (rápido)
    if cancel_check and cancel_check():
        raise RuntimeError("batch_cancelled")
    header_text = _ocr_page_text(job_id, page_index, header_only=True)
    if _classify_text(header_text, allow_crc_table=False, service=service):
        return header_text

    # 3) OCR completo (fallback para tablas / scans)
    if cancel_check and cancel_check():
        raise RuntimeError("batch_cancelled")
    return _ocr_page_text(job_id, page_index, header_only=False)


def _read_cached_text(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _extract_fecha_creacion(text: str) -> Optional[datetime]:
    if not text:
        return None
    m = _FECHA_CREACION_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y")
    except ValueError:
        return None


def _get_fecha_creacion_for_page(job_id: str, page_index: int) -> Optional[datetime]:
    # 1) texto embebido cacheado
    text = _extract_page_text(job_id, page_index)
    date = _extract_fecha_creacion(text)
    if date:
        return date

    # 2) OCR cacheado (si existe, no ejecutar OCR nuevo)
    txt_head, _ = _ocr_cache_paths(job_id, page_index, "_head")
    text = _read_cached_text(txt_head)
    date = _extract_fecha_creacion(text or "")
    if date:
        return date

    txt_full, _ = _ocr_cache_paths(job_id, page_index, "")
    text = _read_cached_text(txt_full)
    date = _extract_fecha_creacion(text or "")
    if date:
        return date

    return None


def _extract_nit_invoice_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extrae NIT base y número de factura desde el texto de la(s) página(s) FEV.
    Reglas:
      - NIT debe estar precedido por la palabra 'NIT'
      - Factura: prefijo letras + dígitos (ej: OCFE5871, ECUC1890)
    """
    nit = None
    invoice = None

    normalized = _strip_accents(text or "")
    upper = normalized.upper()

    # Preferir entorno de FACTURA ELECTRONICA DE VENTA si existe
    fev_idx = upper.find("FACTURA ELECTRONICA DE VENTA")
    if fev_idx != -1:
        window = normalized[max(0, fev_idx - 200) : fev_idx + 2000]
        m_ocfe = _OCFE_RE.search(window)
        if m_ocfe:
            invoice = _normalize_invoice_code(f"OCFE{m_ocfe.group(1)}")
        if not invoice:
            m_inv = _INVOICE_RE.search(window.upper())
            if m_inv:
                invoice = _normalize_invoice_code(m_inv.group(0))

    # Fallback global
    if not invoice:
        m_ocfe = _OCFE_RE.search(normalized)
        if m_ocfe:
            invoice = _normalize_invoice_code(f"OCFE{m_ocfe.group(1)}")
    if not invoice and any(h in upper for h in _INVOICE_HINTS):
        m_inv = _INVOICE_RE.search(upper)
        if m_inv:
            invoice = _normalize_invoice_code(m_inv.group(0))

    # 2) NIT (solo si aparece como NIT:xxxx o NIT xxxx)
    # Captura base y opcional DV. Ej: NIT: 900204617-5
    m_nit = _NIT_RE.search(text)
    if m_nit:
        nit = _normalize_nit(m_nit.group(1))

    return nit, invoice


def _extract_nit_invoice_from_doc(doc: fitz.Document) -> Tuple[Optional[str], Optional[str]]:
    nit_candidates: List[Tuple[int, float, float, str, str]] = []
    inv_candidates: List[Tuple[int, float, float, str, str]] = []

    for i in range(doc.page_count):
        page = doc.load_page(i)
        page_text = page.get_text("text") or ""
        kind = _page_kind(page_text)
        height = page.rect.height or 1.0
        header_y = height * 0.4
        blocks = page.get_text("blocks")

        for block in blocks:
            if len(block) < 5:
                continue
            x0, y0, x1, y1, text = block[:5]
            if not text:
                continue
            t = text.strip()
            if not t:
                continue

            upper = t.upper()
            in_header = y0 <= header_y

            if in_header:
                for m in _NIT_RE.finditer(t):
                    nit = _normalize_nit(m.group(1))
                    if len(nit) >= 6:
                        nit_candidates.append((i, y0, x0, nit, kind))

            # OCFE directo en header
            if in_header:
                m_ocfe = _OCFE_RE.search(t)
                if m_ocfe:
                    inv = _normalize_invoice_code(f"OCFE{m_ocfe.group(1)}")
                    if inv:
                        inv_candidates.append((i, y0, x0, inv, kind))

            # Otros prefijos si hay pistas de factura en el bloque
            if in_header and any(h in upper for h in _INVOICE_HINTS):
                for m in _INVOICE_RE.finditer(upper):
                    inv = _normalize_invoice_code(m.group(0))
                    if inv:
                        inv_candidates.append((i, y0, x0, inv, kind))

    # Preferir página de Factura Electrónica de Venta
    nit_fev = [c for c in nit_candidates if c[4] == "fev"]
    inv_fev = [c for c in inv_candidates if c[4] == "fev"]

    def _pick(cands: List[Tuple[int, float, float, str, str]]) -> Optional[str]:
        return min(cands, key=lambda x: (x[1], x[2]))[3] if cands else None

    nit = _pick(nit_fev) or _pick(nit_candidates)
    invoice = _pick(inv_fev) or _pick(inv_candidates)
    return nit, invoice


def _open_source_pdf(job_id: str, pdf_idx: int) -> fitz.Document:
    path = os.path.join(_job_dir(job_id), "pdfs", f"src_{pdf_idx}.pdf")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="PDF fuente no encontrado.")
    return fitz.open(path)


def _append_page_with_fallback(out: fitz.Document, src_doc: fitz.Document, page_idx: int) -> None:
    try:
        out.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
        return
    except Exception:
        pass

    # Fallback: rasteriza la pagina para evitar errores de estructura interna del PDF fuente.
    page = src_doc.load_page(page_idx)
    rect = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    out_page = out.new_page(width=rect.width, height=rect.height)
    out_page.insert_image(rect, stream=pix.tobytes("png"))


def _build_pdf_from_global_pages(job_id: str, global_pages: List[int]) -> fitz.Document:
    meta = _load_meta(job_id)
    mapping: List[List[int]] = meta["page_map"]  # [[pdf_idx, page_idx], ...]
    out = fitz.open()
    src_docs: Dict[int, fitz.Document] = {}
    try:
        # Insertar páginas por orden dado
        for g in global_pages:
            if g < 0 or g >= len(mapping):
                continue
            pdf_idx, page_idx = mapping[g]
            if pdf_idx not in src_docs:
                src_docs[pdf_idx] = _open_source_pdf(job_id, pdf_idx)
            _append_page_with_fallback(out, src_docs[pdf_idx], page_idx)
    finally:
        for doc in src_docs.values():
            doc.close()
    return out


def _zip_bytes(files: List[Tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, data in files:
            zf.writestr(filename, data)
    return buf.getvalue()


def _build_consolidated_batch_zip(batch_id: str, meta: dict) -> Optional[str]:
    results_dir = os.path.join(_batch_dir(batch_id), "results")
    os.makedirs(results_dir, exist_ok=True)
    done_packages = [p for p in meta.get("packages", []) if p.get("status") == "done" and p.get("resultFile")]
    if not done_packages:
        return None
    all_path = os.path.join(results_dir, "all.zip")
    with zipfile.ZipFile(all_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for pkg in done_packages:
            result_file = pkg.get("resultFile")
            file_path = os.path.join(results_dir, result_file)
            if not os.path.exists(file_path):
                continue
            arcname = pkg.get("downloadName") or result_file
            zf.write(file_path, arcname=arcname)
    meta["allZip"] = "all.zip"
    return all_path


def _log_timing(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


# ----------------------------
# API Models
# ----------------------------
class CreateJobResponse(BaseModel):
    jobId: str
    totalPages: int
    files: int


class ProcessRequest(BaseModel):
    # mapping: globalPageIndex -> category OR null
    classifications: Dict[str, Optional[Category]] = Field(
        ...,
        description="Diccionario con key=str(pageIndex) y value=CRC/FEV/HEV/OPF/PDE o null",
    )
    nitOverride: Optional[str] = None
    ocfeOverride: Optional[str] = None
    # si true, no se borra el job al terminar (debug)
    keepJob: bool = False


class AutoClassifyResponse(BaseModel):
    classifications: Dict[str, Optional[Category]]
    ocrEnabled: bool


class BatchCreateResponse(BaseModel):
    batchId: str
    packages: int


class BatchUploadUrlRequest(BaseModel):
    filename: str = Field(..., description="Nombre del archivo ZIP")
    service: Optional[str] = Field(default="cuidador", description="Servicio de tipificación")


class BatchUploadUrlResponse(BaseModel):
    uploadUrl: str
    gcsPath: str
    objectName: str


class BatchFromGCSRequest(BaseModel):
    gcsPath: str = Field(..., description="Ruta gs://bucket/obj o nombre de objeto")
    service: Optional[str] = Field(default="cuidador", description="Servicio de tipificación")


class CleanupResponse(BaseModel):
    deleted: int
    olderThanMinutes: int


# ----------------------------
# FastAPI app
# ----------------------------
app = FastAPI(title="Tipificador Cloud MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs", response_model=CreateJobResponse)
async def create_job(files: List[UploadFile] = File(...)):
    if not files or len(files) < 1:
        raise HTTPException(status_code=400, detail="Debes subir al menos 1 PDF.")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"Máximo {MAX_FILES} archivos por carga.")

    _cleanup_expired_jobs()

    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    os.makedirs(os.path.join(jdir, "pdfs"), exist_ok=True)
    os.makedirs(os.path.join(jdir, "cache"), exist_ok=True)

    try:
        page_map: List[List[int]] = []
        total_pages = 0

        # Guardar PDFs y construir page_map global
        for i, uf in enumerate(files):
            if not _is_probably_pdf(uf):
                raise HTTPException(status_code=400, detail=f"Archivo no PDF: {uf.filename}")

            src_path = os.path.join(jdir, "pdfs", f"src_{i}.pdf")
            await _save_upload_file_limited(uf, src_path, MAX_FILE_BYTES)
            _rewrite_pdf_structure_inplace(src_path)

            try:
                doc = fitz.open(src_path)
            except Exception:
                raise HTTPException(status_code=400, detail=f"PDF inválido o corrupto: {uf.filename}")

            for p in range(doc.page_count):
                page_map.append([i, p])
            total_pages += doc.page_count
            doc.close()

        meta = {
            "jobId": job_id,
            "files": len(files),
            "totalPages": total_pages,
            "page_map": page_map,  # global index -> [pdf_idx, page_idx]
            "createdAt": time.time(),
        }
        _save_meta(job_id, meta)
    except HTTPException:
        shutil.rmtree(jdir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(jdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Error procesando archivos.")

    return CreateJobResponse(jobId=job_id, totalPages=total_pages, files=len(files))


@app.get("/jobs/{job_id}/pages/{page_index}/thumb.png")
def get_thumb(job_id: str, page_index: int):
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    if page_index < 0 or page_index >= total:
        raise HTTPException(status_code=404, detail="Página fuera de rango.")

    cache_path = os.path.join(_job_dir(job_id), "cache", f"thumb_{page_index}.png")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")

    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    img = _render_page_image(doc, src_page, THUMB_WIDTH)
    doc.close()

    with open(cache_path, "wb") as f:
        f.write(img)

    return Response(content=img, media_type="image/png")


@app.get("/jobs/{job_id}/pages/{page_index}/view.png")
def get_view(job_id: str, page_index: int):
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    if page_index < 0 or page_index >= total:
        raise HTTPException(status_code=404, detail="Página fuera de rango.")

    cache_path = os.path.join(_job_dir(job_id), "cache", f"view_{page_index}.png")
    if CACHE_VIEW and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")

    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    img = _render_page_image(doc, src_page, VIEW_WIDTH)
    doc.close()
    if CACHE_VIEW:
        with open(cache_path, "wb") as f:
            f.write(img)
    return Response(content=img, media_type="image/png")


@app.get("/jobs/{job_id}/pages/{page_index}/ocr.txt")
def get_ocr_text(job_id: str, page_index: int, refresh: bool = False):
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    if page_index < 0 or page_index >= total:
        raise HTTPException(status_code=404, detail="Página fuera de rango.")
    if not OCR_ENABLED:
        raise HTTPException(status_code=503, detail="OCR deshabilitado en el servidor.")
    if refresh:
        txt_path, img_path = _ocr_cache_paths(job_id, page_index)
        for path in (txt_path, img_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    text = _ocr_page_text(job_id, page_index)
    return Response(content=text or "", media_type="text/plain; charset=utf-8")


def _auto_classify_internal(job_id: str, service: str = "cuidador") -> Dict[str, Optional[Category]]:
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    classifications: Dict[str, Optional[Category]] = {}
    service = _normalize_service(service)

    if not OCR_ENABLED:
        raise HTTPException(status_code=503, detail="OCR deshabilitado en el servidor.")

    def _ocr_for_index(idx: int) -> Tuple[int, str]:
        return idx, _page_text_for_classification(job_id, idx, service=service)

    texts: Dict[int, str] = {}
    if OCR_WORKERS > 1 and total > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
            for idx, text in executor.map(_ocr_for_index, range(total)):
                texts[idx] = text
    else:
        for i in range(total):
            texts[i] = _page_text_for_classification(job_id, i, service=service)

    # Primera pasada: solo reglas fuertes (sin estructura de tabla)
    strong: Dict[int, Optional[str]] = {}
    for i in range(total):
        strong[i] = _classify_text(texts.get(i, ""), allow_crc_table=False, service=service)

    # Determinar en qué PDFs hay encabezado CRC real
    page_map: List[List[int]] = meta["page_map"]
    per_pdf: Dict[int, List[int]] = {}
    for g, pair in enumerate(page_map):
        pdf_idx = pair[0]
        per_pdf.setdefault(pdf_idx, []).append(g)

    crc_pdf: Dict[int, bool] = {}
    for pdf_idx, pages in per_pdf.items():
        crc_pdf[pdf_idx] = any(strong.get(p) == "CRC" for p in pages)

    # Segunda pasada: permitir tabla CRC solo si el PDF tiene encabezado CRC
    for i in range(total):
        pdf_idx = page_map[i][0]
        if strong.get(i):
            classifications[str(i)] = strong[i]
        else:
            allow_crc = crc_pdf.get(pdf_idx, False)
            classifications[str(i)] = (
                _classify_text(texts.get(i, ""), allow_crc_table=allow_crc, service=service) or "HEV"
            )

    # Propagar clasificación dentro del mismo PDF fuente si existe un encabezado fuerte unico
    for pdf_idx, pages in per_pdf.items():
        strong_hits = set()
        for p in pages:
            cat = strong.get(p)
            if cat:
                strong_hits.add(cat)
        if len(strong_hits) == 1:
            chosen = next(iter(strong_hits))
            if chosen in {"FEV", "CRC", "PDE", "OPF"}:
                for p in pages:
                    # Solo propagar a páginas sin encabezado fuerte propio
                    if not strong.get(p):
                        classifications[str(p)] = chosen

    return classifications


def _auto_classify_internal_with_cancel(
    job_id: str,
    cancel_check: callable,
    service: str = "cuidador",
) -> Dict[str, Optional[Category]]:
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    classifications: Dict[str, Optional[Category]] = {}
    service = _normalize_service(service)

    if not OCR_ENABLED:
        raise HTTPException(status_code=503, detail="OCR deshabilitado en el servidor.")

    def _text_for_index(idx: int) -> Tuple[int, str]:
        if cancel_check and cancel_check():
            raise RuntimeError("batch_cancelled")
        return idx, _page_text_for_classification(
            job_id,
            idx,
            cancel_check=cancel_check,
            service=service,
        )

    texts: Dict[int, str] = {}
    if OCR_WORKERS > 1 and total > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
            for idx, text in executor.map(_text_for_index, range(total)):
                texts[idx] = text
    else:
        for i in range(total):
            idx, text = _text_for_index(i)
            texts[idx] = text

    # Primera pasada: solo reglas fuertes (sin estructura de tabla)
    strong: Dict[int, Optional[str]] = {}
    for i in range(total):
        strong[i] = _classify_text(texts.get(i, ""), allow_crc_table=False, service=service)

    # Determinar en qué PDFs hay encabezado CRC real
    page_map: List[List[int]] = meta["page_map"]
    per_pdf: Dict[int, List[int]] = {}
    for g, pair in enumerate(page_map):
        pdf_idx = pair[0]
        per_pdf.setdefault(pdf_idx, []).append(g)

    crc_pdf: Dict[int, bool] = {}
    for pdf_idx, pages in per_pdf.items():
        crc_pdf[pdf_idx] = any(strong.get(p) == "CRC" for p in pages)

    # Segunda pasada: permitir tabla CRC solo si el PDF tiene encabezado CRC
    for i in range(total):
        pdf_idx = page_map[i][0]
        if strong.get(i):
            classifications[str(i)] = strong[i]
        else:
            allow_crc = crc_pdf.get(pdf_idx, False)
            classifications[str(i)] = (
                _classify_text(texts.get(i, ""), allow_crc_table=allow_crc, service=service) or "HEV"
            )

    # Propagar clasificación dentro del mismo PDF fuente si existe un encabezado fuerte unico
    for pdf_idx, pages in per_pdf.items():
        strong_hits = set()
        for p in pages:
            cat = strong.get(p)
            if cat:
                strong_hits.add(cat)
        if len(strong_hits) == 1:
            chosen = next(iter(strong_hits))
            if chosen in {"FEV", "CRC", "PDE", "OPF"}:
                for p in pages:
                    if not strong.get(p):
                        classifications[str(p)] = chosen

    return classifications


@app.post("/jobs/{job_id}/auto-classify", response_model=AutoClassifyResponse)
def auto_classify(job_id: str, service: str = "cuidador"):
    classifications = _auto_classify_internal(job_id, service=service)
    return AutoClassifyResponse(classifications=classifications, ocrEnabled=OCR_ENABLED)


def _process_job_bytes(job_id: str, req: ProcessRequest) -> Tuple[str, bytes]:
    meta = _load_meta(job_id)
    total = meta["totalPages"]

    # Construir listas por categoría
    pages_by_cat: Dict[str, List[int]] = {c: [] for c in CATEGORIES}
    for k, v in req.classifications.items():
        try:
            idx = int(k)
        except ValueError:
            continue
        if idx < 0 or idx >= total:
            continue
        if v is None:
            continue
        pages_by_cat[v].append(idx)

    # Validación: FEV obligatorio
    if len(pages_by_cat["FEV"]) == 0:
        raise HTTPException(status_code=400, detail="FEV es obligatorio: tipifica al menos una página como FEV.")

    # Extraer NIT y OCFE (o usar override)
    nit = _normalize_nit(req.nitOverride) if req.nitOverride else None
    ocfe = _normalize_invoice_code(req.ocfeOverride) if req.ocfeOverride else None

    if not nit or not ocfe:
        fev_doc = _build_pdf_from_global_pages(job_id, pages_by_cat["FEV"])
        nit_found, ocfe_found = _extract_nit_invoice_from_doc(fev_doc)

        if not nit_found or not ocfe_found:
            all_text = []
            for i in range(fev_doc.page_count):
                all_text.append(fev_doc.load_page(i).get_text("text") or "")
            text = "\n".join(all_text)
            fallback_nit, fallback_ocfe = _extract_nit_invoice_from_text(text)
            nit_found = nit_found or fallback_nit
            ocfe_found = ocfe_found or fallback_ocfe

        fev_doc.close()
        if not nit:
            nit = nit_found
        if not ocfe:
            ocfe = ocfe_found

    if not nit or not ocfe:
        # MVP sin OCR: devolvemos 422 para que el frontend pida dato manual
        raise HTTPException(
            status_code=422,
            detail={
                "message": "No pude detectar NIT y/o número de factura desde FEV. Ingresa NIT y número de factura manualmente para continuar.",
                "nitDetected": nit,
                "ocfeDetected": ocfe,
            },
        )

    # Generar PDFs por categoría con páginas asignadas
    output_files: List[Tuple[str, bytes]] = []

    for cat in CATEGORIES:
        pages = pages_by_cat[cat]
        if not pages:
            continue
        if cat == "HEV":
            keyed = []
            for idx in pages:
                fecha = _get_fecha_creacion_for_page(job_id, idx)
                keyed.append((1 if fecha is None else 0, fecha or datetime.max, idx))
            pages = [k[2] for k in sorted(keyed)]
        doc_out = _build_pdf_from_global_pages(job_id, pages)
        pdf_bytes = doc_out.tobytes()
        doc_out.close()

        filename = f"{cat}_{nit}_{ocfe}.pdf"
        output_files.append((filename, pdf_bytes))

    zip_data = _zip_bytes(output_files)

    # Borrar temporales si no keep
    if not req.keepJob:
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)

    filename = f"{ocfe}.zip"
    return filename, zip_data


@app.post("/jobs/{job_id}/process")
def process_job(job_id: str, req: ProcessRequest):
    filename, zip_data = _process_job_bytes(job_id, req)
    return StreamingResponse(
        BytesIO(zip_data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _find_batch_package(meta: dict, package_name: str) -> Optional[dict]:
    return next((p for p in meta.get("packages", []) if p.get("name") == package_name), None)


def _set_package_audit(batch_id: str, package_name: str, stage: str, **fields) -> None:
    try:
        meta = _load_batch_meta(batch_id)
        pkg = _find_batch_package(meta, package_name)
        if not pkg:
            return
        now = time.time()
        pkg["currentStage"] = stage
        pkg["lastHeartbeatAt"] = now
        pkg["audit"] = {"stage": stage, "updatedAt": now, **fields}
        _save_batch_meta(batch_id, meta)
    except Exception:
        return


def _batch_package_result_path(batch_id: str, package_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", package_name or "package")
    return os.path.join(_batch_dir(batch_id), f"worker_{safe_name}.json")


def _process_batch_package_local(batch_id: str, package_name: str, service: str) -> dict:
    meta = _load_batch_meta(batch_id)
    pkg = _find_batch_package(meta, package_name)
    if not pkg:
        raise RuntimeError(f"Paquete no encontrado: {package_name}")

    batch_dir = _batch_dir(batch_id)
    input_dir = os.path.join(batch_dir, "input")
    results_dir = os.path.join(batch_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    pkg_dir = os.path.join(input_dir, pkg["folder"])
    _set_package_audit(batch_id, package_name, "collect_pdfs")
    stage_t0 = time.perf_counter()
    pdfs = _collect_pdf_paths(pkg_dir)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        package=package_name,
        stage="collect_pdfs",
        seconds=round(time.perf_counter() - stage_t0, 3),
        pdfs=len(pdfs),
    )

    _set_package_audit(batch_id, package_name, "create_job", pdfs=len(pdfs))
    stage_t0 = time.perf_counter()
    job_id, _ = _create_job_from_pdf_paths(pdfs)
    meta_pages = _load_meta(job_id).get("totalPages", 0)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        package=package_name,
        jobId=job_id,
        stage="create_job",
        seconds=round(time.perf_counter() - stage_t0, 3),
        pages=meta_pages,
    )

    def _cancel_requested() -> bool:
        return _load_batch_meta(batch_id).get("cancelRequested", False)

    _set_package_audit(batch_id, package_name, "auto_classify", jobId=job_id, pages=meta_pages)
    stage_t0 = time.perf_counter()
    classifications = _auto_classify_internal_with_cancel(
        job_id,
        cancel_check=_cancel_requested,
        service=service,
    )
    counts = {c: 0 for c in CATEGORIES}
    for value in classifications.values():
        if value in counts:
            counts[value] += 1
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        package=package_name,
        jobId=job_id,
        stage="auto_classify",
        seconds=round(time.perf_counter() - stage_t0, 3),
        pages=meta_pages,
        counts=counts,
    )

    req = ProcessRequest(classifications=classifications, keepJob=False)
    _set_package_audit(batch_id, package_name, "process_job", jobId=job_id, counts=counts)
    stage_t0 = time.perf_counter()
    download_name, zip_bytes = _process_job_bytes(job_id, req)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        package=package_name,
        jobId=job_id,
        stage="process_job",
        seconds=round(time.perf_counter() - stage_t0, 3),
        zipBytes=len(zip_bytes),
    )

    result_filename = f"{package_name}.zip"
    result_path = os.path.join(results_dir, result_filename)
    _set_package_audit(batch_id, package_name, "write_result", jobId=job_id, zipBytes=len(zip_bytes))
    stage_t0 = time.perf_counter()
    with open(result_path, "wb") as f:
        f.write(zip_bytes)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        package=package_name,
        stage="write_result",
        seconds=round(time.perf_counter() - stage_t0, 3),
        zipBytes=len(zip_bytes),
    )

    return {
        "jobId": job_id,
        "resultFile": result_filename,
        "downloadName": download_name,
    }


def _package_worker_error_message(returncode: int, result: Optional[dict]) -> str:
    if result and result.get("error"):
        return str(result.get("error"))
    if returncode < 0:
        return f"Proceso de tipificación terminó por señal {-returncode}. Posible PDF/OCR con fallo nativo."
    return f"Proceso de tipificación falló con código {returncode}."


def _run_batch_package_worker(batch_id: str, package_name: str, service: str) -> dict:
    result_path = _batch_package_result_path(batch_id, package_name)
    try:
        if os.path.exists(result_path):
            os.remove(result_path)
    except OSError:
        pass

    cmd = [
        sys.executable,
        "-m",
        "app.batch_worker",
        batch_id,
        package_name,
        service,
        result_path,
    ]
    try:
        completed = subprocess.run(cmd, timeout=BATCH_PACKAGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Paquete excedió el tiempo máximo de {BATCH_PACKAGE_TIMEOUT_SECONDS}s."
        )

    result = None
    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            result = None

    if completed.returncode != 0:
        raise RuntimeError(_package_worker_error_message(completed.returncode, result))
    if not result or not result.get("ok"):
        raise RuntimeError(_package_worker_error_message(completed.returncode, result))
    return result.get("result") or {}


def _process_batch(batch_id: str, target_names: Optional[List[str]] = None) -> None:
    batch_t0 = time.perf_counter()
    meta = _load_batch_meta(batch_id)
    service = _normalize_service(meta.get("service"))
    meta["startedAt"] = meta.get("startedAt") or time.time()
    meta["finishedAt"] = None
    meta["elapsedSeconds"] = None
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="batch_start",
        service=service,
        packages=len(meta.get("packages", [])),
    )
    meta["status"] = "processing"
    meta = _save_batch_meta(batch_id, meta)

    batch_dir = _batch_dir(batch_id)
    input_dir = os.path.join(batch_dir, "input")
    results_dir = os.path.join(batch_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    target_set = set(target_names or [])
    package_names = [pkg.get("name") for pkg in meta.get("packages", []) if pkg.get("name")]
    done = 0
    errors = 0

    def _require_package(package_name: str) -> dict:
        pkg = _find_batch_package(meta, package_name)
        if not pkg:
            raise RuntimeError(f"Paquete no encontrado: {package_name}")
        return pkg

    cancelled = False
    for package_name in package_names:
        pkg_t0 = time.perf_counter()
        if meta.get("cancelRequested"):
            cancelled = True
            break
        if target_set and package_name not in target_set:
            continue
        pkg = _require_package(package_name)
        pkg["startedAt"] = time.time()
        pkg["finishedAt"] = None
        pkg["elapsedSeconds"] = None
        pkg["status"] = "processing"
        pkg["error"] = None
        pkg["currentStage"] = "worker_start"
        pkg["lastHeartbeatAt"] = time.time()
        pkg["audit"] = {"stage": "worker_start", "updatedAt": pkg["lastHeartbeatAt"]}
        meta = _save_batch_meta(batch_id, meta)
        try:
            result = _run_batch_package_worker(batch_id, package_name, service)
            pkg = _require_package(package_name)
            pkg["jobId"] = result.get("jobId")
            pkg["resultFile"] = result.get("resultFile")
            pkg["downloadName"] = result.get("downloadName")
            pkg["finishedAt"] = time.time()
            pkg["elapsedSeconds"] = round(pkg["finishedAt"] - (pkg.get("startedAt") or pkg["finishedAt"]), 3)
            pkg["status"] = "done"
            pkg["currentStage"] = "done"
            pkg["lastHeartbeatAt"] = time.time()
            done += 1
            _log_timing(
                "batch_timing",
                batchId=batch_id,
                package=pkg.get("name"),
                jobId=pkg.get("jobId"),
                stage="package_done",
                seconds=round(time.perf_counter() - pkg_t0, 3),
            )
        except RuntimeError as e:
            pkg = _require_package(package_name)
            if str(e) == "batch_cancelled":
                pkg["finishedAt"] = time.time()
                pkg["elapsedSeconds"] = round(pkg["finishedAt"] - (pkg.get("startedAt") or pkg["finishedAt"]), 3)
                pkg["status"] = "cancelled"
                pkg["error"] = "cancelled"
                pkg["currentStage"] = "cancelled"
                pkg["lastHeartbeatAt"] = time.time()
                cancelled = True
            else:
                pkg["finishedAt"] = time.time()
                pkg["elapsedSeconds"] = round(pkg["finishedAt"] - (pkg.get("startedAt") or pkg["finishedAt"]), 3)
                pkg["status"] = "error"
                pkg["error"] = str(e)
                pkg["currentStage"] = "error"
                pkg["lastHeartbeatAt"] = time.time()
                errors += 1
        except HTTPException as e:
            pkg = _require_package(package_name)
            pkg["finishedAt"] = time.time()
            pkg["elapsedSeconds"] = round(pkg["finishedAt"] - (pkg.get("startedAt") or pkg["finishedAt"]), 3)
            pkg["status"] = "error"
            if isinstance(e.detail, dict) and "message" in e.detail:
                pkg["error"] = e.detail.get("message")
            else:
                pkg["error"] = str(e.detail)
            pkg["currentStage"] = "error"
            pkg["lastHeartbeatAt"] = time.time()
            errors += 1
        except Exception as e:
            pkg = _require_package(package_name)
            pkg["finishedAt"] = time.time()
            pkg["elapsedSeconds"] = round(pkg["finishedAt"] - (pkg.get("startedAt") or pkg["finishedAt"]), 3)
            pkg["status"] = "error"
            pkg["error"] = str(e)
            pkg["currentStage"] = "error"
            pkg["lastHeartbeatAt"] = time.time()
            errors += 1
        if pkg.get("status") == "error":
            _log_timing(
                "batch_timing",
                batchId=batch_id,
                package=pkg.get("name"),
                stage="package_error",
                seconds=round(time.perf_counter() - pkg_t0, 3),
                error=pkg.get("error"),
            )
        meta = _save_batch_meta(batch_id, meta)

    # Build consolidated ZIP
    stage_t0 = time.perf_counter()
    all_path = _build_consolidated_batch_zip(batch_id, meta)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="build_all_zip",
        seconds=round(time.perf_counter() - stage_t0, 3),
    )

    if _gcs_enabled():
        stage_t0 = time.perf_counter()
        try:
            client = _gcs_client()
            bucket = client.bucket(GCS_BUCKET)
            result_prefix = f"{_normalize_prefix(GCS_RESULTS_PREFIX)}{batch_id}/"
            for package_name in package_names:
                pkg = _find_batch_package(meta, package_name)
                if not pkg or pkg.get("status") != "done":
                    continue
                result_file = pkg.get("resultFile")
                if not result_file:
                    continue
                local_path = os.path.join(results_dir, result_file)
                if not os.path.exists(local_path):
                    continue
                object_name = f"{result_prefix}{result_file}"
                blob = bucket.blob(object_name)
                blob.upload_from_filename(local_path, content_type="application/zip")
                pkg["gcsResult"] = f"gs://{GCS_BUCKET}/{object_name}"

            if all_path and os.path.exists(all_path):
                all_object = f"{result_prefix}all.zip"
                blob = bucket.blob(all_object)
                blob.upload_from_filename(all_path, content_type="application/zip")
                meta["gcsAllZip"] = f"gs://{GCS_BUCKET}/{all_object}"
        except Exception as e:
            meta["gcsError"] = str(e)
        _log_timing(
            "batch_timing",
            batchId=batch_id,
            stage="upload_results_gcs",
            seconds=round(time.perf_counter() - stage_t0, 3),
            gcsError=meta.get("gcsError"),
        )

    done_count = sum(1 for p in meta.get("packages", []) if p.get("status") == "done")
    error_count = sum(1 for p in meta.get("packages", []) if p.get("status") == "error")
    pending_count = sum(1 for p in meta.get("packages", []) if p.get("status") in {"pending", "processing"})

    if cancelled:
        meta["status"] = "cancelled"
        meta["cancelRequested"] = False
        for p in meta.get("packages", []):
            if p.get("status") in {"pending", "processing"}:
                p["status"] = "cancelled"
    elif pending_count:
        meta["status"] = "processing"
    elif error_count and done_count:
        meta["status"] = "partial"
    elif error_count and not done_count:
        meta["status"] = "error"
    elif done_count:
        meta["status"] = "done"
    else:
        meta["status"] = "pending"
    meta["finishedAt"] = time.time()
    meta["elapsedSeconds"] = round(meta["finishedAt"] - (meta.get("startedAt") or meta["finishedAt"]), 3)
    try:
        meta = _save_batch_meta(batch_id, meta, final=True, verify=True)
    except BatchMetaPersistenceError as exc:
        meta[BATCH_META_SYNC_ERROR_FIELD] = str(exc)
        _persist_batch_meta_to_disk(batch_id, meta)
        logger.exception("Final metadata persistence failed for batch %s", batch_id)
        _log_timing(
            "batch_timing",
            batchId=batch_id,
            stage="batch_finalization_error",
            seconds=round(time.perf_counter() - batch_t0, 3),
            status=meta.get("status"),
            done=done_count,
            errors=error_count,
            syncError=meta.get(BATCH_META_SYNC_ERROR_FIELD),
        )
        return
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="batch_done",
        seconds=round(time.perf_counter() - batch_t0, 3),
        status=meta.get("status"),
        done=done_count,
        errors=error_count,
        syncError=meta.get(BATCH_META_SYNC_ERROR_FIELD),
    )


def _build_batch_from_zip(
    batch_id: str,
    zip_path: str,
    bdir: str,
    source_gcs_path: Optional[str] = None,
    service: str = "cuidador",
) -> BatchCreateResponse:
    service = _normalize_service(service)
    input_dir = os.path.join(bdir, "input")
    os.makedirs(input_dir, exist_ok=True)

    stage_t0 = time.perf_counter()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract_zip(zf, input_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="ZIP inválido o corrupto.")
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="extract_zip",
        service=service,
        seconds=round(time.perf_counter() - stage_t0, 3),
        zipBytes=os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
        source="gcs" if source_gcs_path else "direct",
    )

    pkg_folders = [
        name for name in os.listdir(input_dir)
        if os.path.isdir(os.path.join(input_dir, name)) and not name.startswith("__")
    ]
    if not pkg_folders:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="ZIP sin carpetas de paquetes.")
    if len(pkg_folders) > MAX_BATCH_PACKAGES:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(status_code=413, detail=f"Máximo {MAX_BATCH_PACKAGES} paquetes por lote.")

    empty_packages: List[str] = []
    for folder in sorted(pkg_folders):
        pkg_dir = os.path.join(input_dir, folder)
        if not _collect_pdf_paths(pkg_dir):
            empty_packages.append(folder)
    if empty_packages:
        shutil.rmtree(bdir, ignore_errors=True)
        if len(empty_packages) == 1:
            raise HTTPException(
                status_code=400,
                detail=f"El paquete '{empty_packages[0]}' no contiene archivos PDF.",
            )
        raise HTTPException(
            status_code=400,
            detail=(
                "Los siguientes paquetes no contienen archivos PDF: "
                + ", ".join(empty_packages)
            ),
        )

    packages = []
    for folder in sorted(pkg_folders):
        packages.append({
            "name": folder,
            "folder": folder,
            "status": "pending",
            "startedAt": None,
            "finishedAt": None,
            "elapsedSeconds": None,
            "jobId": None,
            "resultFile": None,
            "downloadName": None,
            "error": None,
            "gcsResult": None,
            "currentStage": None,
            "lastHeartbeatAt": None,
            "audit": None,
        })

    stable_source_gcs_path = _persist_batch_source_zip(batch_id, zip_path) or source_gcs_path

    meta = {
        "batchId": batch_id,
        "service": service,
        "createdAt": time.time(),
        "startedAt": None,
        "finishedAt": None,
        "elapsedSeconds": None,
        "status": "ready",
        "cancelRequested": False,
        "packages": packages,
        "allZip": None,
        "gcsAllZip": None,
        "sourceGcsPath": stable_source_gcs_path,
    }
    _save_batch_meta(batch_id, meta)

    return BatchCreateResponse(batchId=batch_id, packages=len(packages))


@app.post("/batch", response_model=BatchCreateResponse)
async def create_batch(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    service: str = Form("cuidador"),
):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Debes subir un archivo .zip")

    batch_id = uuid.uuid4().hex
    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)
    input_dir = os.path.join(bdir, "input")
    os.makedirs(input_dir, exist_ok=True)

    zip_path = os.path.join(bdir, "batch.zip")
    stage_t0 = time.perf_counter()
    await _save_upload_file_limited(file, zip_path, MAX_BATCH_BYTES)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="upload_zip",
        service=_normalize_service(service),
        seconds=round(time.perf_counter() - stage_t0, 3),
        zipBytes=os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
        source="direct",
    )
    return _build_batch_from_zip(
        batch_id,
        zip_path,
        bdir,
        source_gcs_path=None,
        service=service,
    )


@app.post("/batch/upload-url", response_model=BatchUploadUrlResponse)
def batch_upload_url(req: BatchUploadUrlRequest):
    if not _gcs_enabled():
        raise HTTPException(status_code=400, detail="GCS no está configurado en el servidor.")
    prefix = _normalize_prefix(GCS_UPLOAD_PREFIX)
    safe_name = _safe_object_name(req.filename)
    object_name = f"{prefix}{uuid.uuid4().hex}_{safe_name}"
    upload_url = _generate_upload_url(object_name)
    gcs_path = f"gs://{GCS_BUCKET}/{object_name}"
    return BatchUploadUrlResponse(uploadUrl=upload_url, gcsPath=gcs_path, objectName=object_name)


@app.post("/batch/from-gcs", response_model=BatchCreateResponse)
def create_batch_from_gcs(req: BatchFromGCSRequest):
    if not _gcs_enabled():
        raise HTTPException(status_code=400, detail="GCS no está configurado en el servidor.")
    bucket_name, object_name = _parse_gcs_path(req.gcsPath)
    if bucket_name != GCS_BUCKET:
        raise HTTPException(status_code=400, detail="Bucket no permitido.")
    if not object_name:
        raise HTTPException(status_code=400, detail="Objeto GCS inválido.")

    client = _gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    if not blob.exists():
        raise HTTPException(status_code=404, detail="Objeto no encontrado en GCS.")
    blob.reload()
    if blob.size and blob.size > MAX_BATCH_BYTES:
        raise HTTPException(status_code=413, detail=f"Máximo {MAX_BATCH_BYTES // (1024*1024)}MB por lote.")

    batch_id = uuid.uuid4().hex
    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)
    zip_path = os.path.join(bdir, "batch.zip")
    stage_t0 = time.perf_counter()
    blob.download_to_filename(zip_path)
    _log_timing(
        "batch_timing",
        batchId=batch_id,
        stage="download_zip_gcs",
        service=_normalize_service(req.service),
        seconds=round(time.perf_counter() - stage_t0, 3),
        zipBytes=os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
        source="gcs",
    )

    resp = _build_batch_from_zip(
        batch_id,
        zip_path,
        bdir,
        source_gcs_path=req.gcsPath,
        service=req.service or "cuidador",
    )
    try:
        meta = _load_batch_meta(batch_id)
        persisted_source = meta.get("sourceGcsPath")
        if req.gcsPath and persisted_source and persisted_source != req.gcsPath:
            _delete_gcs_object(req.gcsPath)
    except Exception:
        pass
    return resp


@app.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta_latest(batch_id, cache_local=False), persist=False)
    batch_status = meta.get("status")
    return {
        "batchId": meta.get("batchId"),
        "service": meta.get("service", "cuidador"),
        "createdAt": meta.get("createdAt"),
        "startedAt": meta.get("startedAt"),
        "finishedAt": meta.get("finishedAt"),
        "elapsedSeconds": _live_elapsed_seconds(meta.get("startedAt"), meta.get("elapsedSeconds"), batch_status),
        "status": batch_status,
        "cancelRequested": meta.get("cancelRequested", False),
        "packages": [
            {
                "name": p.get("name"),
                "status": p.get("status"),
                "startedAt": p.get("startedAt"),
                "finishedAt": p.get("finishedAt"),
                "elapsedSeconds": _live_elapsed_seconds(p.get("startedAt"), p.get("elapsedSeconds"), p.get("status")),
                "jobId": p.get("jobId"),
                "downloadName": p.get("downloadName"),
                "error": p.get("error"),
                "currentStage": p.get("currentStage"),
                "lastHeartbeatAt": p.get("lastHeartbeatAt"),
                "audit": p.get("audit"),
            }
            for p in meta.get("packages", [])
        ],
    }


@app.post("/batch/{batch_id}/start")
def start_batch(batch_id: str):
    meta = _load_batch_meta_latest(batch_id)
    if meta.get("status") in {"processing"}:
        return {"batchId": batch_id, "status": meta.get("status")}
    if meta.get("status") in {"done"}:
        return {"batchId": batch_id, "status": meta.get("status")}
    source_gcs = meta.get("sourceGcsPath")
    if source_gcs:
        input_dir = os.path.join(_batch_dir(batch_id), "input")
        if not os.path.isdir(input_dir) or not os.listdir(input_dir):
            _restore_batch_input_from_gcs(batch_id, source_gcs)
    meta["cancelRequested"] = False
    if not meta.get("startedAt"):
        meta["startedAt"] = time.time()
    meta["finishedAt"] = None
    meta["elapsedSeconds"] = None
    meta["status"] = "processing"
    _save_batch_meta(batch_id, meta)
    threading.Thread(target=_process_batch, args=(batch_id,), daemon=True).start()
    return {"batchId": batch_id, "status": "processing"}


@app.post("/batch/{batch_id}/cancel")
def cancel_batch(batch_id: str):
    meta = _load_batch_meta_latest(batch_id)
    if meta.get("status") in {"ready", "pending"}:
        meta["cancelRequested"] = False
        meta["status"] = "cancelled"
        _save_batch_meta(batch_id, meta)
        return {"batchId": batch_id, "status": meta.get("status")}
    meta["cancelRequested"] = True
    meta["status"] = "cancelling"
    _save_batch_meta(batch_id, meta)
    return {"batchId": batch_id, "status": "cancelling"}


@app.post("/batch/{batch_id}/retry-errors")
def retry_batch_errors(batch_id: str):
    meta = _load_batch_meta_latest(batch_id)
    error_pkgs = [p.get("name") for p in meta.get("packages", []) if p.get("status") == "error"]
    if not error_pkgs:
        return {"batchId": batch_id, "retried": 0}
    if not meta.get("startedAt"):
        meta["startedAt"] = time.time()
    meta["finishedAt"] = None
    meta["elapsedSeconds"] = None
    meta["status"] = "processing"
    meta["cancelRequested"] = False
    _save_batch_meta(batch_id, meta)
    threading.Thread(target=_process_batch, args=(batch_id, error_pkgs), daemon=True).start()
    return {"batchId": batch_id, "retried": len(error_pkgs)}


@app.post("/admin/cleanup", response_model=CleanupResponse)
def cleanup_results(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    minutes: Optional[int] = None,
):
    if CLEANUP_TOKEN and x_admin_token != CLEANUP_TOKEN:
        raise HTTPException(status_code=403, detail="No autorizado.")
    if not _gcs_enabled():
        raise HTTPException(status_code=400, detail="GCS no está configurado en el servidor.")
    age = minutes if minutes is not None else CLEANUP_AGE_MINUTES
    deleted = _cleanup_gcs_results(age)
    return CleanupResponse(deleted=deleted, olderThanMinutes=int(age))


@app.get("/batch/{batch_id}/download/all.zip")
def download_batch_all(batch_id: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta_latest(batch_id, cache_local=False), persist=False)
    if _gcs_enabled() and meta.get("gcsAllZip"):
        bucket, obj = _parse_gcs_path(meta["gcsAllZip"])
        if bucket == GCS_BUCKET and obj:
            url = _generate_download_url(obj, "TIPIFICADO_LOTE.zip")
            return RedirectResponse(url)
    results_dir = os.path.join(_batch_dir(batch_id), "results")
    all_path = os.path.join(results_dir, meta["allZip"]) if meta.get("allZip") else None
    if not all_path or not os.path.exists(all_path):
        rebuilt = _build_consolidated_batch_zip(batch_id, meta)
        if rebuilt:
            all_path = rebuilt
    if not all_path or not os.path.exists(all_path):
        raise HTTPException(status_code=404, detail="ZIP consolidado no disponible.")
    return StreamingResponse(
        open(all_path, "rb"),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="TIPIFICADO_LOTE.zip"'},
    )


@app.get("/batch/{batch_id}/download/{package_name}.zip")
def download_batch_package(batch_id: str, package_name: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta_latest(batch_id, cache_local=False), persist=False)
    pkg = next((p for p in meta.get("packages", []) if p.get("name") == package_name), None)
    if not pkg or pkg.get("status") != "done":
        raise HTTPException(status_code=404, detail="Paquete no disponible.")
    if _gcs_enabled() and pkg.get("gcsResult"):
        bucket, obj = _parse_gcs_path(pkg["gcsResult"])
        if bucket == GCS_BUCKET and obj:
            download_name = pkg.get("downloadName") or f"{package_name}.zip"
            url = _generate_download_url(obj, download_name)
            return RedirectResponse(url)
    result_file = pkg.get("resultFile")
    if not result_file:
        raise HTTPException(status_code=404, detail="Paquete no disponible.")
    results_dir = os.path.join(_batch_dir(batch_id), "results")
    file_path = os.path.join(results_dir, result_file)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Paquete no disponible.")
    download_name = pkg.get("downloadName") or f"{package_name}.zip"
    return StreamingResponse(
        open(file_path, "rb"),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )
