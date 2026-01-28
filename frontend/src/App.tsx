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
  const fileInputRef = useRef<HTMLInputElement | null>(null);

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
  const [thumbErrors, setThumbErrors] = useState<Set<number>>(new Set());
  const [autoClassifying, setAutoClassifying] = useState(false);

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

  function resetUI() {
    setJobId(null);
    setTotalPages(0);
    setCls({});
    setSelected(new Set());
    setPreviewPage(null);
    setNeedOverride(null);
    setNitOverride("");
    setOcfeOverride("");
    setThumbErrors(new Set());
    lastClicked.current = null;
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setJobId(null);
    setTotalPages(0);
    setCls({});
    setSelected(new Set());
    setPreviewPage(null);
    setNeedOverride(null);
    setNitOverride("");
    setOcfeOverride("");
    setThumbErrors(new Set());

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
    const cd = res.headers.get("Content-Disposition") || "";
    const filenameMatch = cd.match(/filename="?([^"]+)"?/i);
    const filename = filenameMatch?.[1] || "TIPIFICADO.zip";
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);

    setProcessing(false);
    // job is deleted server-side after process; reset UI to start a new batch
    resetUI();
  }

  async function autoClassify() {
    if (!jobId) return;
    setAutoClassifying(true);
    try {
      const res = await fetch(`${API_BASE}/jobs/${jobId}/auto-classify`, {
        method: "POST",
      });
      if (!res.ok) {
        const t = await res.text();
        alert(`Error auto-tipificando: ${t}`);
        setAutoClassifying(false);
        return;
      }
      const data = await res.json();
      const next: Record<number, Category | null> = {};
      Object.entries(data.classifications || {}).forEach(([k, v]) => {
        const idx = Number(k);
        if (Number.isFinite(idx)) {
          next[idx] = (v as Category) ?? null;
        }
      });
      setCls(next);
    } finally {
      setAutoClassifying(false);
    }
  }

  const typedCount = totalPages - counts.SIN;
  const progress = totalPages > 0 ? Math.round((typedCount / totalPages) * 100) : 0;

  const fileButtonClass = uploading ? "fileButton fileButton--disabled" : "fileButton";

  return (
    <div className="app">
      <header className="topAppBar">
        <div>
          <div className="title">Tipificador Cloud</div>
          <div className="subtitle">Clasifica páginas y genera ZIP por categoría</div>
        </div>
        <div className="row">
          <span className="chip">API: {API_BASE.replace(/^https?:\/\//, "")}</span>
          {uploading && <span className="chip chip--info">Subiendo…</span>}
          {jobId && <span className="chip chip--muted">Job: {jobId}</span>}
          {totalPages > 0 && <span className="chip">Páginas: {totalPages}</span>}
        </div>
      </header>

      <main className="content">
        <section className="card">
          <div className="row">
            <label className={fileButtonClass}>
              <input
                type="file"
                multiple
                accept="application/pdf"
                onChange={(e) => onUpload(e.target.files)}
                disabled={uploading}
                ref={fileInputRef}
              />
              Cargar PDFs
            </label>
            <span className="chip chip--muted">Selección: click, Ctrl, Shift</span>
            <span className="chip chip--muted">FEV obligatorio</span>
          </div>

          <div className="progressBar" aria-label="progreso">
            <div className="progressFill" style={{ width: `${progress}%` }} />
          </div>
          <div className="statsGroup">
            <div className="row">
              <span className="chip">Tipificadas: {typedCount}</span>
              <span className="chip">Sin tipificar: {counts.SIN}</span>
              <span className="chip">Seleccionadas: {selected.size}</span>
            </div>

            <div className="row">
              {CATEGORIES.map((c) => (
                <span key={c} className={`chip chip--cat chip--${c.toLowerCase()}`}>
                  {c}: {counts[c]}
                </span>
              ))}
            </div>
          </div>
        </section>

        {jobId && totalPages > 0 && (
          <section className="grid">
            <div className="card thumbPane">
              <div className="toolbar row">
                {CATEGORIES.map((c) => (
                  <button
                    key={c}
                    className="btn btn--tonal"
                    onClick={() => assignCategory(c)}
                    disabled={selected.size === 0}
                  >
                    {c}
                  </button>
                ))}
                <button className="btn btn--outlined" onClick={clearCategory} disabled={selected.size === 0}>
                  Limpiar
                </button>
                <button
                  className="btn btn--tonal"
                  onClick={autoClassify}
                  disabled={!jobId || autoClassifying}
                  title="Clasificar automáticamente usando OCR"
                >
                  {autoClassifying ? "Auto‑tipificando…" : "Auto‑tipificar"}
                </button>
                <button
                  className="btn btn--filled"
                  onClick={() => processJob(false)}
                  disabled={!hasFEV || processing}
                  title={!hasFEV ? "FEV es obligatorio" : "Procesar y descargar ZIP"}
                >
                  {processing ? "Procesando…" : "Procesar"}
                </button>
              </div>

              <div className="thumbScroller">
                <div className="thumbGrid">
                  {Array.from({ length: totalPages }, (_, i) => {
                    const isSel = selected.has(i);
                    const label = cls[i] ?? "SIN";
                    const labelClass = label === "SIN" ? "cat-none" : `cat-${label.toLowerCase()}`;
                    const hasError = thumbErrors.has(i);
                    const displayIndex = i + 1;
                    return (
                      <div
                        key={i}
                        className={`thumb ${isSel ? "selected" : ""} ${labelClass}`}
                        onClick={(e) => toggleSelect(i, e)}
                        title={`Página ${displayIndex}`}
                      >
                        {!hasError ? (
                          <img
                            src={`${API_BASE}/jobs/${jobId}/pages/${i}/thumb.png`}
                            alt={`p${displayIndex}`}
                            loading="lazy"
                            onError={() =>
                              setThumbErrors((prev) => new Set(prev).add(i))
                            }
                          />
                        ) : (
                          <div className="thumbFallback">Sin vista</div>
                        )}
                        <div className="thumbLabel">
                          <span>#{displayIndex}</span>
                          <span className="dot">·</span>
                          <span>{label}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="card previewCard">
              <div className="row">
                <div className="sectionTitle">Vista previa</div>
                {previewPage !== null && (
                  <>
                    <span className="chip">Página #{previewPage + 1}</span>
                    <span className="chip chip--muted">
                      Tipo: {cls[previewPage] ?? "SIN TIPIFICAR"}
                    </span>
                  </>
                )}
              </div>
              {previewPage === null ? (
                <p className="small">Haz click en una miniatura para ver la página en grande.</p>
              ) : (
                <div className="preview">
                  <img
                    src={`${API_BASE}/jobs/${jobId}/pages/${previewPage}/view.png`}
                    alt={`view${previewPage + 1}`}
                  />
                </div>
              )}
            </div>
          </section>
        )}
      </main>

      {needOverride && (
        <div className="modalBackdrop">
          <div className="modal">
            <h3>No pude detectar NIT o número de factura</h3>
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
                placeholder="Factura (ej: OCFE5871 o ECUC1890)"
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
