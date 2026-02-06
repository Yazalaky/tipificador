import os
import re
import json
import uuid
import time
import unicodedata
import shutil
import zipfile
import subprocess
import concurrent.futures
import threading
from io import BytesIO
from typing import Dict, List, Literal, Optional, Tuple

import fitz  # PyMuPDF
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ----------------------------
# Config
# ----------------------------
JOB_ROOT = os.environ.get("TIPIFICADOR_JOB_ROOT", "/tmp/tipificador_jobs")
os.makedirs(JOB_ROOT, exist_ok=True)
BATCH_ROOT = os.path.join(JOB_ROOT, "batches")
os.makedirs(BATCH_ROOT, exist_ok=True)

CATEGORIES = ["CRC", "FEV", "HEV", "OPF", "PDE"]
Category = Literal["CRC", "FEV", "HEV", "OPF", "PDE"]

THUMB_WIDTH = 240
VIEW_WIDTH = 1100

MAX_FILE_BYTES = int(os.environ.get("TIPIFICADOR_MAX_FILE_BYTES", "104857600"))  # 100MB
MAX_FILES = int(os.environ.get("TIPIFICADOR_MAX_FILES", "20"))
JOB_TTL_SECONDS = int(os.environ.get("TIPIFICADOR_JOB_TTL_SECONDS", "21600"))  # 6 hours
CACHE_VIEW = os.environ.get("TIPIFICADOR_CACHE_VIEW", "1").lower() not in {"0", "false", "no"}
OCR_ENABLED = os.environ.get("TIPIFICADOR_OCR_ENABLED", "1").lower() not in {"0", "false", "no"}
OCR_LANG = os.environ.get("TIPIFICADOR_OCR_LANG", "spa+eng")
OCR_DPI = int(os.environ.get("TIPIFICADOR_OCR_DPI", "300"))
OCR_PSM = os.environ.get("TIPIFICADOR_OCR_PSM", "4")
OCR_KEEP_IMAGES = os.environ.get("TIPIFICADOR_OCR_KEEP_IMAGES", "0").lower() in {"1", "true", "yes"}
OCR_WORKERS = int(os.environ.get("TIPIFICADOR_OCR_WORKERS", "4"))
MAX_BATCH_PACKAGES = int(os.environ.get("TIPIFICADOR_MAX_BATCH_PACKAGES", "10"))
MAX_BATCH_BYTES = int(os.environ.get("TIPIFICADOR_MAX_BATCH_BYTES", "524288000"))  # 500MB

_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_NIT_RE = re.compile(
    r"\bNIT\b\s*[:\-]?\s*([0-9\.\, ]{6,15}(?:\s*-\s*\d)?)",
    flags=re.IGNORECASE,
)
_OCFE_RE = re.compile(r"\bOCFE\s*(\d{3,})\b", flags=re.IGNORECASE)
_INVOICE_RE = re.compile(r"\b([A-Z]{3,6})\s*(\d{3,})\b")
_INVOICE_HINTS = ("FACTURA", "ELECTR", "VENTA", "N°", "NO.", "NRO")
_FEV_HINTS = ("FACTURA ELECTRONICA DE VENTA", "FACTURA ELECTRÓNICA DE VENTA")
_NC_HINTS = ("NOTA DE CREDITO ELECTRONICA", "NOTA DE CRÉDITO ELECTRONICA")
_AUTO_RULES_STRONG: List[Tuple[str, Tuple[str, ...]]] = [
    ("PDE", ("AUTORIZACION SERVICIOS", "AUTORIZACION SERVICIOS ")),
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
    ("FEV", ("FACTURA ELECTRONICA DE VENTA", "NOTA DE CREDITO ELECTRONICA", "NOTA DE CRÉDITO ELECTRONICA", "DETALLE DE CARGOS", "FACTURA OCFE")),
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


def _load_batch_meta(batch_id: str) -> dict:
    path = _batch_meta_path(batch_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Batch no existe o expiró.")
    for _ in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            time.sleep(0.05)
    raise HTTPException(status_code=503, detail="Batch temporalmente ocupado, intenta de nuevo.")


def _save_batch_meta(batch_id: str, meta: dict) -> None:
    path = _batch_meta_path(batch_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _reconcile_batch_meta(batch_id: str, meta: dict) -> dict:
    results_dir = os.path.join(_batch_dir(batch_id), "results")
    changed = False
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
        if pending_count:
            meta["status"] = "processing"
        elif error_count and done_count:
            meta["status"] = "partial"
        elif error_count and not done_count:
            meta["status"] = "error"
        elif done_count:
            meta["status"] = "done"
        else:
            meta["status"] = meta.get("status") or "pending"
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
    if prefix in {"NIT", "CUFE", "CUDE"}:
        return None
    digits = re.sub(r"\D", "", m.group(2))
    if not digits:
        return None
    return f"{prefix}{digits}"


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
    if "SERVICIO" not in t or "PRESTADOR" not in t:
        return False
    if not ("TUTOR" in t or "TUTOR/PACIENTE" in t or "FIRMA" in t):
        return False
    if not ("N." in t or "N°" in t or "NO." in t or "NRO" in t):
        return False
    if "ATENCION CUIDADOR" not in t and "CUIDADOR" not in t:
        return False
    return True


def _classify_text(text: str, allow_crc_table: bool = False) -> Optional[str]:
    if not text:
        return None
    t = _normalize_ocr_text(text)
    for cat, patterns in _AUTO_RULES_STRONG:
        for p in patterns:
            if p in t:
                return cat
    if allow_crc_table and _has_crc_table_hint(t):
        return "CRC"
    return None


def _ocr_page_text(job_id: str, page_index: int) -> str:
    if not OCR_ENABLED:
        return ""
    cache_txt = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}.txt")
    if os.path.exists(cache_txt):
        with open(cache_txt, "r", encoding="utf-8") as f:
            return f.read()

    meta = _load_meta(job_id)
    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    try:
        page = doc.load_page(src_page)
        zoom = OCR_DPI / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img_path = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}.png")
        pix.save(img_path)
    finally:
        doc.close()

    out_base = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}")
    cmd = ["tesseract", img_path, out_base, "-l", OCR_LANG, "--psm", str(OCR_PSM)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 and OCR_LANG != "eng":
        cmd = ["tesseract", img_path, out_base, "-l", "eng", "--psm", str(OCR_PSM)]
        subprocess.run(cmd, capture_output=True, text=True)

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


def _ocr_cache_paths(job_id: str, page_index: int) -> Tuple[str, str]:
    base = os.path.join(_job_dir(job_id), "cache", f"ocr_{page_index}")
    return f"{base}.txt", f"{base}.png"


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
            out.insert_pdf(src_docs[pdf_idx], from_page=page_idx, to_page=page_idx)
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


def _auto_classify_internal(job_id: str) -> Dict[str, Optional[Category]]:
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    classifications: Dict[str, Optional[Category]] = {}

    if not OCR_ENABLED:
        raise HTTPException(status_code=503, detail="OCR deshabilitado en el servidor.")

    def _ocr_for_index(idx: int) -> Tuple[int, str]:
        return idx, _ocr_page_text(job_id, idx)

    texts: Dict[int, str] = {}
    if OCR_WORKERS > 1 and total > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
            for idx, text in executor.map(_ocr_for_index, range(total)):
                texts[idx] = text
    else:
        for i in range(total):
            texts[i] = _ocr_page_text(job_id, i)

    # Primera pasada: solo reglas fuertes (sin estructura de tabla)
    strong: Dict[int, Optional[str]] = {}
    for i in range(total):
        strong[i] = _classify_text(texts.get(i, ""), allow_crc_table=False)

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
            classifications[str(i)] = _classify_text(texts.get(i, ""), allow_crc_table=allow_crc) or "HEV"

    # Propagar clasificación dentro del mismo PDF fuente si existe un encabezado fuerte unico
    for pdf_idx, pages in per_pdf.items():
        strong_hits = set()
        for p in pages:
            cat = classifications[str(p)]
            if cat in {"FEV", "CRC", "OPF", "PDE"}:
                strong_hits.add(cat)
        if len(strong_hits) == 1:
            chosen = next(iter(strong_hits))
            for p in pages:
                classifications[str(p)] = chosen

    return classifications


def _auto_classify_internal_with_cancel(
    job_id: str,
    cancel_check: callable,
) -> Dict[str, Optional[Category]]:
    meta = _load_meta(job_id)
    total = meta["totalPages"]
    classifications: Dict[str, Optional[Category]] = {}

    if not OCR_ENABLED:
        raise HTTPException(status_code=503, detail="OCR deshabilitado en el servidor.")

    texts: Dict[int, str] = {}
    for i in range(total):
        if cancel_check():
            raise RuntimeError("batch_cancelled")
        texts[i] = _ocr_page_text(job_id, i)

    # Primera pasada: solo reglas fuertes (sin estructura de tabla)
    strong: Dict[int, Optional[str]] = {}
    for i in range(total):
        strong[i] = _classify_text(texts.get(i, ""), allow_crc_table=False)

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
            classifications[str(i)] = _classify_text(texts.get(i, ""), allow_crc_table=allow_crc) or "HEV"

    # Propagar clasificación dentro del mismo PDF fuente si existe un encabezado fuerte unico
    for pdf_idx, pages in per_pdf.items():
        strong_hits = set()
        for p in pages:
            cat = classifications[str(p)]
            if cat in {"FEV", "CRC", "OPF", "PDE"}:
                strong_hits.add(cat)
        if len(strong_hits) == 1:
            chosen = next(iter(strong_hits))
            for p in pages:
                classifications[str(p)] = chosen

    return classifications


@app.post("/jobs/{job_id}/auto-classify", response_model=AutoClassifyResponse)
def auto_classify(job_id: str):
    classifications = _auto_classify_internal(job_id)
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
        doc_out = _build_pdf_from_global_pages(job_id, pages)
        pdf_bytes = doc_out.tobytes()
        doc_out.close()

        filename = f"{cat}_{nit}_{ocfe}.pdf"
        output_files.append((filename, pdf_bytes))

    zip_data = _zip_bytes(output_files)

    # Borrar temporales si no keep
    if not req.keepJob:
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)

    filename = f"TIPIFICADO_{nit}_{ocfe}.zip"
    return filename, zip_data


@app.post("/jobs/{job_id}/process")
def process_job(job_id: str, req: ProcessRequest):
    filename, zip_data = _process_job_bytes(job_id, req)
    return StreamingResponse(
        BytesIO(zip_data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _process_batch(batch_id: str, target_names: Optional[List[str]] = None) -> None:
    meta = _load_batch_meta(batch_id)
    meta["status"] = "processing"
    _save_batch_meta(batch_id, meta)

    batch_dir = _batch_dir(batch_id)
    input_dir = os.path.join(batch_dir, "input")
    results_dir = os.path.join(batch_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    target_set = set(target_names or [])
    done = 0
    errors = 0

    cancelled = False
    for pkg in meta.get("packages", []):
        if meta.get("cancelRequested"):
            cancelled = True
            break
        if target_set and pkg.get("name") not in target_set:
            continue
        pkg["status"] = "processing"
        pkg["error"] = None
        _save_batch_meta(batch_id, meta)
        try:
            pkg_dir = os.path.join(input_dir, pkg["folder"])
            pdfs = _collect_pdf_paths(pkg_dir)
            job_id, _ = _create_job_from_pdf_paths(pdfs)
            pkg["jobId"] = job_id

            classifications = _auto_classify_internal_with_cancel(
                job_id,
                cancel_check=lambda: _load_batch_meta(batch_id).get("cancelRequested", False),
            )
            req = ProcessRequest(classifications=classifications, keepJob=False)
            download_name, zip_bytes = _process_job_bytes(job_id, req)

            result_filename = f"{pkg['name']}.zip"
            result_path = os.path.join(results_dir, result_filename)
            with open(result_path, "wb") as f:
                f.write(zip_bytes)

            pkg["resultFile"] = result_filename
            pkg["downloadName"] = download_name
            pkg["status"] = "done"
            done += 1
        except RuntimeError as e:
            if str(e) == "batch_cancelled":
                pkg["status"] = "cancelled"
                pkg["error"] = "cancelled"
                cancelled = True
            else:
                pkg["status"] = "error"
                pkg["error"] = str(e)
                errors += 1
        except HTTPException as e:
            pkg["status"] = "error"
            pkg["error"] = e.detail
            errors += 1
        except Exception as e:
            pkg["status"] = "error"
            pkg["error"] = str(e)
            errors += 1
        _save_batch_meta(batch_id, meta)

    # Build consolidated ZIP
    all_path = os.path.join(results_dir, "all.zip")
    with zipfile.ZipFile(all_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for pkg in meta.get("packages", []):
            if pkg.get("status") != "done":
                continue
            result_file = pkg.get("resultFile")
            if not result_file:
                continue
            file_path = os.path.join(results_dir, result_file)
            arcname = pkg.get("downloadName") or result_file
            zf.write(file_path, arcname=arcname)

    meta["allZip"] = "all.zip"
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
    _save_batch_meta(batch_id, meta)


@app.post("/batch", response_model=BatchCreateResponse)
async def create_batch(background: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Debes subir un archivo .zip")

    batch_id = uuid.uuid4().hex
    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)
    input_dir = os.path.join(bdir, "input")
    os.makedirs(input_dir, exist_ok=True)

    zip_path = os.path.join(bdir, "batch.zip")
    await _save_upload_file_limited(file, zip_path, MAX_BATCH_BYTES)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract_zip(zf, input_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="ZIP inválido o corrupto.")

    # Find package folders (top-level)
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

    packages = []
    for folder in sorted(pkg_folders):
        packages.append({
            "name": folder,
            "folder": folder,
            "status": "pending",
            "jobId": None,
            "resultFile": None,
            "downloadName": None,
            "error": None,
        })

    meta = {
        "batchId": batch_id,
        "createdAt": time.time(),
        "status": "ready",
        "cancelRequested": False,
        "packages": packages,
        "allZip": None,
    }
    _save_batch_meta(batch_id, meta)

    return BatchCreateResponse(batchId=batch_id, packages=len(packages))


@app.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta(batch_id))
    return {
        "batchId": meta.get("batchId"),
        "createdAt": meta.get("createdAt"),
        "status": meta.get("status"),
        "cancelRequested": meta.get("cancelRequested", False),
        "packages": [
            {
                "name": p.get("name"),
                "status": p.get("status"),
                "jobId": p.get("jobId"),
                "downloadName": p.get("downloadName"),
                "error": p.get("error"),
            }
            for p in meta.get("packages", [])
        ],
    }


@app.post("/batch/{batch_id}/start")
def start_batch(batch_id: str):
    meta = _load_batch_meta(batch_id)
    if meta.get("status") in {"processing"}:
        return {"batchId": batch_id, "status": meta.get("status")}
    if meta.get("status") in {"done"}:
        return {"batchId": batch_id, "status": meta.get("status")}
    meta["cancelRequested"] = False
    meta["status"] = "processing"
    _save_batch_meta(batch_id, meta)
    threading.Thread(target=_process_batch, args=(batch_id,), daemon=True).start()
    return {"batchId": batch_id, "status": "processing"}


@app.post("/batch/{batch_id}/cancel")
def cancel_batch(batch_id: str):
    meta = _load_batch_meta(batch_id)
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
    meta = _load_batch_meta(batch_id)
    error_pkgs = [p.get("name") for p in meta.get("packages", []) if p.get("status") == "error"]
    if not error_pkgs:
        return {"batchId": batch_id, "retried": 0}
    meta["status"] = "processing"
    meta["cancelRequested"] = False
    _save_batch_meta(batch_id, meta)
    threading.Thread(target=_process_batch, args=(batch_id, error_pkgs), daemon=True).start()
    return {"batchId": batch_id, "retried": len(error_pkgs)}


@app.get("/batch/{batch_id}/download/all.zip")
def download_batch_all(batch_id: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta(batch_id))
    if not meta.get("allZip"):
        raise HTTPException(status_code=404, detail="ZIP consolidado no disponible.")
    results_dir = os.path.join(_batch_dir(batch_id), "results")
    all_path = os.path.join(results_dir, meta["allZip"])
    if not os.path.exists(all_path):
        raise HTTPException(status_code=404, detail="ZIP consolidado no disponible.")
    return StreamingResponse(
        open(all_path, "rb"),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="TIPIFICADO_LOTE.zip"'},
    )


@app.get("/batch/{batch_id}/download/{package_name}.zip")
def download_batch_package(batch_id: str, package_name: str):
    meta = _reconcile_batch_meta(batch_id, _load_batch_meta(batch_id))
    pkg = next((p for p in meta.get("packages", []) if p.get("name") == package_name), None)
    if not pkg or pkg.get("status") != "done":
        raise HTTPException(status_code=404, detail="Paquete no disponible.")
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
