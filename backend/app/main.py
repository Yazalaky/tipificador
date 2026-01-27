import os
import re
import json
import uuid
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


# ----------------------------
# Helpers
# ----------------------------
def _job_dir(job_id: str) -> str:
    return os.path.join(JOB_ROOT, job_id)


def _meta_path(job_id: str) -> str:
    return os.path.join(_job_dir(job_id), "meta.json")


def _load_meta(job_id: str) -> dict:
    path = _meta_path(job_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Job no existe o expiró.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_meta(job_id: str, meta: dict) -> None:
    with open(_meta_path(job_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


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


def _extract_nit_ocfe_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extrae NIT base y OCFE desde el texto de la(s) página(s) FEV.
    Reglas:
      - NIT debe estar precedido por la palabra 'NIT'
      - OCFE debe tener el formato OCFE + dígitos
    """
    nit = None
    ocfe = None

    # 1) OCFE (alta precisión)
    # Captura OCFE5871 o OCFE 5871
    m_ocfe = re.search(r"\bOCFE\s*(\d{3,})\b", text, flags=re.IGNORECASE)
    if m_ocfe:
        ocfe = f"OCFE{m_ocfe.group(1)}".upper()

    # 2) NIT (solo si aparece como NIT:xxxx o NIT xxxx)
    # Captura base y opcional DV. Ej: NIT: 900204617-5
    m_nit = re.search(
        r"\bNIT\b\s*[:\-]?\s*([0-9\.\, ]{6,15}(?:\s*-\s*\d)?)",
        text,
        flags=re.IGNORECASE
    )
    if m_nit:
        nit = _normalize_nit(m_nit.group(1))

    return nit, ocfe


def _open_source_pdf(job_id: str, pdf_idx: int) -> fitz.Document:
    path = os.path.join(_job_dir(job_id), "pdfs", f"src_{pdf_idx}.pdf")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="PDF fuente no encontrado.")
    return fitz.open(path)


def _build_pdf_from_global_pages(job_id: str, global_pages: List[int]) -> fitz.Document:
    meta = _load_meta(job_id)
    mapping: List[List[int]] = meta["page_map"]  # [[pdf_idx, page_idx], ...]
    out = fitz.open()
    # Insertar páginas por orden dado
    for g in global_pages:
        if g < 0 or g >= len(mapping):
            continue
        pdf_idx, page_idx = mapping[g]
        src = _open_source_pdf(job_id, pdf_idx)
        out.insert_pdf(src, from_page=page_idx, to_page=page_idx)
        src.close()
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

    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    os.makedirs(os.path.join(jdir, "pdfs"), exist_ok=True)
    os.makedirs(os.path.join(jdir, "cache"), exist_ok=True)

    page_map: List[List[int]] = []
    total_pages = 0

    # Guardar PDFs y construir page_map global
    for i, uf in enumerate(files):
        if not uf.filename.lower().endswith(".pdf"):
            shutil.rmtree(jdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Archivo no PDF: {uf.filename}")

        content = await uf.read()
        src_path = os.path.join(jdir, "pdfs", f"src_{i}.pdf")
        with open(src_path, "wb") as f:
            f.write(content)

        doc = fitz.open(src_path)
        for p in range(doc.page_count):
            page_map.append([i, p])
        total_pages += doc.page_count
        doc.close()

    meta = {
        "jobId": job_id,
        "files": len(files),
        "totalPages": total_pages,
        "page_map": page_map,  # global index -> [pdf_idx, page_idx]
        "createdAt": uuid.uuid1().time,  # simple marker
    }
    _save_meta(job_id, meta)

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

    pdf_idx, src_page = meta["page_map"][page_index]
    doc = _open_source_pdf(job_id, pdf_idx)
    img = _render_page_image(doc, src_page, VIEW_WIDTH)
    doc.close()
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
    ocfe = req.ocfeOverride.strip().upper().replace(" ", "") if req.ocfeOverride else None

    if not nit or not ocfe:
        fev_doc = _build_pdf_from_global_pages(job_id, pages_by_cat["FEV"])
        all_text = []
        for i in range(fev_doc.page_count):
            all_text.append(fev_doc.load_page(i).get_text("text") or "")
        fev_doc.close()
        text = "\n".join(all_text)

        nit_found, ocfe_found = _extract_nit_ocfe_from_text(text)
        if not nit:
            nit = nit_found
        if not ocfe:
            ocfe = ocfe_found

    if not nit or not ocfe:
        # MVP sin OCR: devolvemos 422 para que el frontend pida dato manual
        raise HTTPException(
            status_code=422,
            detail={
                "message": "No pude detectar NIT y/o OCFE desde FEV. Ingresa NIT y OCFE manualmente para continuar.",
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

