import os
import re
import json
import uuid
import time
import unicodedata
import shutil
import zipfile
from io import BytesIO
from typing import Dict, List, Literal, Optional, Tuple

import fitz  # PyMuPDF
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ----------------------------
# Config
# ----------------------------
JOB_ROOT = os.environ.get("TIPIFICADOR_JOB_ROOT", "/tmp/tipificador_jobs")
os.makedirs(JOB_ROOT, exist_ok=True)

CATEGORIES = ["CRC", "FEV", "HEV", "OPF", "PDE"]
Category = Literal["CRC", "FEV", "HEV", "OPF", "PDE"]

THUMB_WIDTH = 240
VIEW_WIDTH = 1100

MAX_FILE_BYTES = int(os.environ.get("TIPIFICADOR_MAX_FILE_BYTES", "104857600"))  # 100MB
MAX_FILES = int(os.environ.get("TIPIFICADOR_MAX_FILES", "20"))
JOB_TTL_SECONDS = int(os.environ.get("TIPIFICADOR_JOB_TTL_SECONDS", "21600"))  # 6 hours
CACHE_VIEW = os.environ.get("TIPIFICADOR_CACHE_VIEW", "1").lower() not in {"0", "false", "no"}

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


# ----------------------------
# Helpers
# ----------------------------
def _job_dir(job_id: str) -> str:
    return os.path.join(JOB_ROOT, job_id)


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


@app.post("/jobs/{job_id}/process")
def process_job(job_id: str, req: ProcessRequest):
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

    return StreamingResponse(
        BytesIO(zip_data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="TIPIFICADO_{nit}_{ocfe}.zip"'},
    )
