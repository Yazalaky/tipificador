import React, { useMemo, useRef, useState } from "react";

type Category = "CRC" | "FEV" | "HEV" | "OPF" | "PDE";
const CATEGORIES: Category[] = ["CRC", "FEV", "HEV", "OPF", "PDE"];

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

type CreateJobResponse = {
  jobId: string;
  totalPages: number;
  files: number;
};

type DetectError = {
  message: string;
  nitDetected?: string | null;
  ocfeDetected?: string | null;
};

export default function App() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [totalPages, setTotalPages] = useState<number>(0);
  const [uploading, setUploading] = useState(false);

  // classifications: pageIndex -> Category | null
  const [cls, setCls] = useState<Record<number, Category | null>>({});
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const lastClicked = useRef<number | null>(null);

  const [previewPage, setPreviewPage] = useState<number | null>(null);

  // manual override modal
  const [needOverride, setNeedOverride] = useState<DetectError | null>(null);
  const [nitOverride, setNitOverride] = useState("");
  const [ocfeOverride, setOcfeOverride] = useState("");
  const [processing, setProcessing] = useState(false);

  const counts = useMemo(() => {
    const c: Record<string, number> = { SIN: 0 };
    for (const k of CATEGORIES) c[k] = 0;
    for (let i = 0; i < totalPages; i++) {
      const v = cls[i] ?? null;
      if (!v) c.SIN++;
      else c[v]++;
    }
    return c;
  }, [cls, totalPages]);

  const hasFEV = counts["FEV"] > 0;

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setJobId(null);
    setTotalPages(0);
    setCls({});
    setSelected(new Set());
    setPreviewPage(null);

    const form = new FormData();
    for (const f of Array.from(files)) {
      form.append("files", f);
    }

    const res = await fetch(`${API_BASE}/jobs`, {
      method: "POST",
      body: form,
    });

    if (!res.ok) {
      const t = await res.text();
      setUploading(false);
      alert(`Error subiendo PDFs: ${t}`);
      return;
    }

    const data = (await res.json()) as CreateJobResponse;
    setJobId(data.jobId);
    setTotalPages(data.totalPages);
    setUploading(false);
  }

  function toggleSelect(page: number, e: React.MouseEvent) {
    const isCtrl = e.ctrlKey || e.metaKey;
    const isShift = e.shiftKey;

    setSelected((prev) => {
      const next = new Set(prev);

      if (isShift && lastClicked.current !== null) {
        const a = lastClicked.current;
        const b = page;
        const [start, end] = a < b ? [a, b] : [b, a];
        for (let i = start; i <= end; i++) next.add(i);
      } else if (isCtrl) {
        if (next.has(page)) next.delete(page);
        else next.add(page);
        lastClicked.current = page;
      } else {
        // single select
        next.clear();
        next.add(page);
        lastClicked.current = page;
      }

      return next;
    });

    setPreviewPage(page);
  }

  function assignCategory(cat: Category) {
    if (selected.size === 0) return;
    setCls((prev) => {
      const next = { ...prev };
      selected.forEach((p) => (next[p] = cat));
      return next;
    });
  }

  function clearCategory() {
    if (selected.size === 0) return;
    setCls((prev) => {
      const next = { ...prev };
      selected.forEach((p) => (next[p] = null));
      return next;
    });
  }

  async function processJob(withOverride: boolean) {
    if (!jobId) return;
    if (!hasFEV) {
      alert("FEV es obligatorio. Tipifica al menos una página como FEV.");
      return;
    }

    setProcessing(true);
    setNeedOverride(null);

    const payload: any = {
      classifications: Object.fromEntries(
        Array.from({ length: totalPages }, (_, i) => [String(i), cls[i] ?? null])
      ),
      keepJob: false,
    };

    if (withOverride) {
      payload.nitOverride = nitOverride.trim() || null;
      payload.ocfeOverride = ocfeOverride.trim() || null;
    }

    const res = await fetch(`${API_BASE}/jobs/${jobId}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (res.status === 422) {
      const data = await res.json();
      const detail = data?.detail as DetectError;
      setNeedOverride(detail);
      setNitOverride(detail?.nitDetected ? String(detail.nitDetected) : "");
      setOcfeOverride(detail?.ocfeDetected ? String(detail.ocfeDetected) : "");
      setProcessing(false);
      return;
    }

    if (!res.ok) {
      const t = await res.text();
      setProcessing(false);
      alert(`Error procesando: ${t}`);
      return;
    }

    // download zip
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "TIPIFICADO.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);

    setProcessing(false);
    // job is deleted server-side after process; reset UI optionally
    // setJobId(null);
  }

  return (
    <div className="container">
      <h2>Tipificador Cloud (MVP)</h2>
      <div className="card">
        <div className="row">
          <input
            type="file"
            multiple
            accept="application/pdf"
            onChange={(e) => onUpload(e.target.files)}
            disabled={uploading}
          />
          <span className="badge">
            API: {API_BASE.replace("http://", "").replace("https://", "")}
          </span>
          {uploading && <span className="badge">Subiendo…</span>}
          {jobId && <span className="badge">Job: {jobId}</span>}
          {totalPages > 0 && <span className="badge">Páginas: {totalPages}</span>}
        </div>

        <hr />

        <div className="row">
          <span className="badge">SIN TIPIFICAR: {counts.SIN}</span>
          {CATEGORIES.map((c) => (
            <span key={c} className="badge">
              {c}: {counts[c]}
            </span>
          ))}
        </div>

        <p className="small">
          Selección: click (única), Ctrl (multi), Shift (rango). Luego asigna CRC/FEV/HEV/OPF/PDE.
          <br />
          Requisito: FEV obligatorio para detectar NIT+OCFE y generar nombres.
        </p>
      </div>

      {jobId && totalPages > 0 && (
        <div className="grid" style={{ marginTop: 14 }}>
          <div className="card">
            <div className="toolbar row">
              {CATEGORIES.map((c) => (
                <button key={c} onClick={() => assignCategory(c)} disabled={selected.size === 0}>
                  {c}
                </button>
              ))}
              <button className="danger" onClick={clearCategory} disabled={selected.size === 0}>
                Limpiar
              </button>
              <button
                className="primary"
                onClick={() => processJob(false)}
                disabled={!hasFEV || processing}
                title={!hasFEV ? "FEV es obligatorio" : "Procesar y descargar ZIP"}
              >
                {processing ? "Procesando…" : "Procesar"}
              </button>
            </div>

            <hr />

            <div className="thumbGrid">
              {Array.from({ length: totalPages }, (_, i) => {
                const isSel = selected.has(i);
                const label = cls[i] ?? "SIN";
                return (
                  <div
                    key={i}
                    className={`thumb ${isSel ? "selected" : ""}`}
                    onClick={(e) => toggleSelect(i, e)}
                    title={`Página ${i}`}
                  >
                    <img src={`${API_BASE}/jobs/${jobId}/pages/${i}/thumb.png`} alt={`p${i}`} />
                    <div className="thumbLabel">
                      #{i} · {label}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="card">
            <h3>Vista previa</h3>
            {previewPage === null ? (
              <p className="small">Haz click en una miniatura para ver la página en grande.</p>
            ) : (
              <>
                <div className="row">
                  <span className="badge">Página #{previewPage}</span>
                  <span className="badge">Tipo: {cls[previewPage] ?? "SIN TIPIFICAR"}</span>
                </div>
                <hr />
                <img
                  src={`${API_BASE}/jobs/${jobId}/pages/${previewPage}/view.png`}
                  alt={`view${previewPage}`}
                  style={{ width: "100%", borderRadius: 12, border: "1px solid #22304a" }}
                />
              </>
            )}
          </div>
        </div>
      )}

      {needOverride && (
        <div className="modalBackdrop">
          <div className="modal">
            <h3>No pude detectar NIT/OCFE</h3>
            <p className="small">{needOverride.message}</p>
            <div className="row" style={{ marginTop: 8 }}>
              <input
                type="text"
                placeholder="NIT (ej: 900204617)"
                value={nitOverride}
                onChange={(e) => setNitOverride(e.target.value)}
              />
              <input
                type="text"
                placeholder="OCFE (ej: OCFE5871)"
                value={ocfeOverride}
                onChange={(e) => setOcfeOverride(e.target.value)}
              />
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="primary" onClick={() => processJob(true)} disabled={processing}>
                Reintentar con datos
              </button>
              <button
                onClick={() => {
                  setNeedOverride(null);
                }}
              >
                Cancelar
              </button>
            </div>
            <p className="small" style={{ marginTop: 8, opacity: 0.85 }}>
              Nota: este MVP intenta leer NIT y OCFE desde texto embebido en la factura. OCR lo añadimos después.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

