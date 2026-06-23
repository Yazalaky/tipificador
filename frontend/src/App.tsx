import React, { useMemo, useRef, useState } from "react";

type Category = "CRC" | "FEV" | "HEV" | "OPF" | "PDE";
const CATEGORIES: Category[] = ["CRC", "FEV", "HEV", "OPF", "PDE"];

const API_BASE = (import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const APP_HOST =
  typeof window !== "undefined" ? window.location.hostname : "";
const APP_IS_LOCAL = APP_HOST === "127.0.0.1" || APP_HOST === "localhost";
const API_BASE_IS_LOCAL = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(API_BASE);

function getApiConnectionMessage() {
  if (!APP_IS_LOCAL && API_BASE_IS_LOCAL) {
    return `La aplicacion esta apuntando a ${API_BASE}. Esa URL es local y no funcionara fuera de tu equipo. Vuelve a desplegar el frontend con VITE_API_BASE apuntando al backend publicado.`;
  }
  return `No se pudo conectar con la API (${API_BASE}). Verifica que el backend este disponible y que VITE_API_BASE este bien configurado.`;
}

type DetectError = {
  message: string;
  nitDetected?: string | null;
  ocfeDetected?: string | null;
};

type ServiceId = "cuidador" | "otros_servicios";
type ModeId = "single" | "batch";

type BatchPackage = {
  name: string;
  status: string;
  startedAt?: number | null;
  finishedAt?: number | null;
  elapsedSeconds?: number | null;
  jobId?: string | null;
  downloadName?: string | null;
  error?: string | Record<string, unknown> | null;
  currentStage?: string | null;
  lastHeartbeatAt?: number | null;
  audit?: { stage?: string | null; updatedAt?: number | null } | null;
};

type ProcessPayload = {
  classifications: Record<string, Category | null>;
  keepJob: boolean;
  nitOverride?: string | null;
  ocfeOverride?: string | null;
};

type BatchCreateData = {
  batchId: string;
  packages: number;
};

function formatElapsedSeconds(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value) || value < 0) return null;
  if (value < 60) return `${value.toFixed(1)} s`;
  const totalSeconds = Math.round(value);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function resolveElapsedSeconds(elapsedSeconds?: number | null) {
  if (elapsedSeconds != null && Number.isFinite(elapsedSeconds)) return elapsedSeconds;
  return null;
}

function inferBatchStatus(status: string | null | undefined, packages: BatchPackage[]) {
  if (status && status !== "processing") return status;
  if (!packages.length) return status ?? null;

  const done = packages.filter((p) => p.status === "done").length;
  const error = packages.filter((p) => p.status === "error").length;
  const cancelled = packages.filter((p) => p.status === "cancelled").length;
  const pending = packages.filter((p) => p.status === "pending").length;
  const processing = packages.filter((p) => p.status === "processing").length;

  if (cancelled) return "cancelled";
  if (pending || processing) return status ?? "processing";
  if (error && done) return "partial";
  if (error && !done) return "error";
  if (done) return "done";
  return status ?? null;
}

function formatBatchError(err: BatchPackage["error"]) {
  if (!err) return "";
  if (typeof err === "string") return err;
  const msg = (err as Record<string, unknown>)["message"];
  if (typeof msg === "string") return msg;
  try {
    return JSON.stringify(err);
  } catch {
    return "Error desconocido";
  }
}

function formatStage(stage: string | null | undefined) {
  if (!stage) return "";
  return stage.replace(/_/g, " ");
}

const SERVICES: {
  id: ServiceId;
  label: string;
  desc: string;
  icon: string;
}[] = [
  {
    id: "cuidador",
    label: "Cuidador",
    desc: "",
    icon: "C",
  },
  {
    id: "otros_servicios",
    label: "Otros Servicios",
    desc: "",
    icon: "O",
  },
];

export default function App() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [totalPages, setTotalPages] = useState<number>(0);
  const batchInputRef = useRef<HTMLInputElement | null>(null);
  const [service, setService] = useState<ServiceId | null>(null);
  const [mode, setMode] = useState<ModeId>("batch");

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
  const [batchUploading, setBatchUploading] = useState(false);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [batchStatus, setBatchStatus] = useState<string | null>(null);
  const [batchPackages, setBatchPackages] = useState<BatchPackage[]>([]);
  const [batchElapsedSeconds, setBatchElapsedSeconds] = useState<number | null>(null);
  const [batchRetrying, setBatchRetrying] = useState(false);
  const [batchNotice, setBatchNotice] = useState<string | null>(null);
  const [batchActive, setBatchActive] = useState(false);
  const serviceLabel = useMemo(() => {
    if (!service) return "";
    return service === "cuidador" ? "Cuidador" : "Otros Servicios";
  }, [service]);

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
  const hasJob = Boolean(jobId && totalPages > 0);
  const batchStatusRef = useRef<string | null>(null);
  const batchPackagesRef = useRef<BatchPackage[]>([]);

  React.useEffect(() => {
    batchStatusRef.current = batchStatus;
    batchPackagesRef.current = batchPackages;
  }, [batchStatus, batchPackages]);

  function resetJobState() {
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
  }

  function resetBatchState() {
    setBatchUploading(false);
    setBatchId(null);
    setBatchStatus(null);
    setBatchPackages([]);
    setBatchElapsedSeconds(null);
    setBatchNotice(null);
    setBatchActive(false);
    if (batchInputRef.current) {
      batchInputRef.current.value = "";
    }
  }

  function resetUI() {
    resetJobState();
    resetBatchState();
  }

  function selectService(id: ServiceId) {
    resetUI();
    setMode("batch");
    setService(id);
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

    const payload: ProcessPayload = {
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
    resetJobState();
  }

  async function autoClassify() {
    if (!jobId) return;
    setAutoClassifying(true);
    try {
      const svc = encodeURIComponent(service ?? "cuidador");
      const res = await fetch(`${API_BASE}/jobs/${jobId}/auto-classify?service=${svc}`, {
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

  async function onBatchUpload(file: File | null) {
    if (!file) return;
    if (!APP_IS_LOCAL && API_BASE_IS_LOCAL) {
      alert(getApiConnectionMessage());
      return;
    }
    resetBatchState();
    setBatchUploading(true);

    const tryGcs = async () => {
      try {
        const res = await fetch(`${API_BASE}/batch/upload-url`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, service: service ?? "cuidador" }),
        });
        if (!res.ok) {
          return null;
        }
        const data = await res.json();
        if (!data?.uploadUrl || !data?.gcsPath) {
          return null;
        }
        const putRes = await fetch(data.uploadUrl, {
          method: "PUT",
          headers: { "Content-Type": "application/zip" },
          body: file,
        });
        if (!putRes.ok) {
          throw new Error("No se pudo subir el ZIP a GCS.");
        }
        const batchRes = await fetch(`${API_BASE}/batch/from-gcs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ gcsPath: data.gcsPath, service: service ?? "cuidador" }),
        });
        if (!batchRes.ok) {
          const t = await batchRes.text();
          throw new Error(t || "No se pudo crear el lote desde GCS.");
        }
        return batchRes.json();
      } catch {
        // Si la ruta GCS no esta disponible o la API no responde aqui, intentamos
        // el upload directo al endpoint /batch antes de fallar por completo.
        return null;
      }
    };

    const uploadBatchDirect = async () => {
      const form = new FormData();
      form.append("file", file);
      form.append("service", service ?? "cuidador");

      const res = await fetch(`${API_BASE}/batch`, {
        method: "POST",
        body: form,
      });

      if (!res.ok) {
        const t = await res.text();
        throw new Error(t || "Error subiendo lote.");
      }

      return res.json();
    };

    let data: BatchCreateData | null = null;
    try {
      data = await tryGcs();
      if (!data) {
        data = await uploadBatchDirect();
      }
    } catch (err: unknown) {
      setBatchUploading(false);
      if (err instanceof TypeError) {
        alert(getApiConnectionMessage());
      } else if (err instanceof Error) {
        alert(err.message || "Error subiendo lote.");
      } else {
        alert("Error subiendo lote.");
      }
      return;
    }

    if (!data) {
      setBatchUploading(false);
      alert("No se pudo crear el lote.");
      return;
    }

    setBatchId(data.batchId);
    setBatchStatus("ready");
    setBatchActive(false);
    setBatchUploading(false);
    await refreshBatch();
  }

  const refreshBatch = React.useCallback(async () => {
    if (!batchId) return null;
    try {
      const res = await fetch(`${API_BASE}/batch/${batchId}?ts=${Date.now()}`, {
        cache: "no-store",
      });
      if (!res.ok) {
        if (res.status === 404) {
          const knownStatus = inferBatchStatus(batchStatusRef.current, batchPackagesRef.current);
          if (knownStatus && ["done", "partial", "error", "cancelled"].includes(knownStatus)) {
            return knownStatus;
          }
          setBatchActive(false);
          setBatchNotice("No se pudo seguir consultando el lote. Recarga la página o intenta de nuevo.");
          return "missing";
        }
        return null;
      }
      const data = await res.json();
      const packages = (data.packages || []) as BatchPackage[];
      const status = inferBatchStatus(data.status || "pending", packages);
      setBatchStatus(status);
      setBatchPackages(packages);
      setBatchElapsedSeconds(data.elapsedSeconds ?? null);
      if (status && ["done", "partial", "error", "cancelled"].includes(status)) {
        setBatchActive(false);
      }
      return status;
    } catch {
      return null;
    }
  }, [batchId]);

  async function retryBatchErrors() {
    if (!batchId) return;
    setBatchRetrying(true);
    const res = await fetch(`${API_BASE}/batch/${batchId}/retry-errors`, { method: "POST" });
    if (!res.ok) {
      const t = await res.text();
      alert(`Error reintentando: ${t}`);
      setBatchRetrying(false);
      return;
    }
    setBatchStatus("processing");
    setBatchActive(true);
    setBatchRetrying(false);
    await refreshBatch();
  }

  async function startBatch() {
    if (!batchId) return;
    const res = await fetch(`${API_BASE}/batch/${batchId}/start`, { method: "POST" });
    if (!res.ok) {
      const t = await res.text();
      alert(`Error iniciando lote: ${t}`);
      return;
    }
    setBatchStatus("processing");
    setBatchActive(true);
    await refreshBatch();
  }

  async function cancelBatch() {
    if (!batchId) return;
    const res = await fetch(`${API_BASE}/batch/${batchId}/cancel`, { method: "POST" });
    if (!res.ok) {
      const t = await res.text();
      alert(`Error cancelando lote: ${t}`);
      return;
    }
    setBatchStatus("cancelling");
    setBatchActive(true);
    const status = await refreshBatch();
    if (status === "cancelled") {
      setBatchNotice("Lote cancelado. Vuelve a cargar los archivos.");
      resetBatchState();
    }
  }

  React.useEffect(() => {
    if (!batchId) return;
    let timer: number | null = null;
    let active = true;

    const tick = async () => {
      if (!active) return;
      const status = await refreshBatch();
      if (!status) {
        timer = window.setTimeout(tick, 3000);
        return;
      }
      if (status === "missing") {
        return;
      }
      if (status === "cancelled") {
        setBatchNotice("Lote cancelado. Vuelve a cargar los archivos.");
        resetBatchState();
        return;
      }
      if (
        batchActive ||
        status === "processing" ||
        status === "cancelling" ||
        status === "ready" ||
        status === "pending"
      ) {
        timer = window.setTimeout(tick, 3000);
      }
    };

    tick();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [batchId, batchActive, refreshBatch]);

  const typedCount = totalPages - counts.SIN;
  const progress = totalPages > 0 ? Math.round((typedCount / totalPages) * 100) : 0;

  const dropzoneClass = batchUploading ? "dropzone dropzone--disabled" : "dropzone";
  const showHome = !service;
  const showUpload = Boolean(service) && mode === "batch";
  const showWork = hasJob && mode === "single";
  const showBatch = Boolean(service) && mode === "batch";
  const batchTotal = batchPackages.length;
  const batchDone = batchPackages.filter((p) => p.status === "done").length;
  const batchError = batchPackages.filter((p) => p.status === "error").length;
  const batchProgress = batchTotal > 0 ? Math.round(((batchDone + batchError) / batchTotal) * 100) : 0;
  const effectiveBatchStatus = inferBatchStatus(batchStatus, batchPackages);
  const batchBusy = effectiveBatchStatus === "processing" || effectiveBatchStatus === "cancelling";
  const batchLiveSeconds = resolveElapsedSeconds(
    batchElapsedSeconds
  );
  const batchElapsedLabel = formatElapsedSeconds(batchLiveSeconds);
  const getPackageElapsedLabel = (pkg: BatchPackage) =>
    formatElapsedSeconds(resolveElapsedSeconds(pkg.elapsedSeconds));

  return (
    <div className="app">
      <header className="appHeader">
        <div className="brand">
          <span className="brandTitle">Tipificador</span>
          <span className="brandAccent">Cloud</span>
        </div>
        <div className="row headerMeta">
          {service && <span className="chip chip--info">Servicio: {serviceLabel}</span>}
          {showWork && (
            <>
              <span className="chip">API: {API_BASE.replace(/^https?:\/\//, "")}</span>
              {jobId && <span className="chip chip--muted">Job: {jobId}</span>}
              {totalPages > 0 && <span className="chip">Páginas: {totalPages}</span>}
            </>
          )}
        </div>
      </header>

      <main className="content">
        {showHome && (
          <section className="panel heroPanel">
            <div className="hero">
              <h1 className="heroTitle">Selecciona el servicio a tipificar</h1>
              <p className="heroSub">
                Elige el módulo correspondiente para comenzar la carga y procesamiento de documentos.
              </p>
            </div>
            <div className="serviceGrid">
              {SERVICES.map((s, idx) => (
                <button
                  key={`${s.id}-${idx}`}
                  className="serviceTile"
                  onClick={() => selectService(s.id)}
                >
                  <div className="serviceIcon">{s.icon}</div>
                  <div className="serviceInfo">
                    <div className="serviceLabel">{s.label}</div>
                    {s.desc ? <div className="serviceDesc">{s.desc}</div> : null}
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}

        {showUpload && (
          <section className="panel uploadPanel">
            <div className="panelHeader">
              <button className="linkButton" onClick={() => setService(null)} disabled={batchBusy}>
                ← Volver a servicios
              </button>
              <div className="panelMeta">
                <span className="chip chip--muted">Modo: Masivo</span>
                <span className="chip chip--info">{serviceLabel}</span>
              </div>
            </div>
            <div className="panelTitleRow">
              <div>
                <div className="panelEyebrow">Tipificador Cloud</div>
                <div className="panelTitle">Carga de Documentos</div>
              </div>
            </div>

            <div className="requirements">
              <div className="requirementsTitle">Requisitos del archivo:</div>
              <ul>
                <li>Formato aceptado: ZIP únicamente.</li>
                <li>Estructura interna: ZIP → carpeta → PDFs.</li>
                <li>Máximo 20 paquetes por carga.</li>
                <li>Máximo 80 PDFs por paquete.</li>
              </ul>
            </div>

            <label className={dropzoneClass}>
              <input
                type="file"
                accept="application/zip"
                onChange={(e) => onBatchUpload(e.target.files?.[0] ?? null)}
                disabled={batchUploading}
                ref={batchInputRef}
              />
              <div className="dropzoneIcon">ZIP</div>
              <div className="dropzoneTitle">Arrastra tu archivo ZIP aquí</div>
              <div className="small">O haz clic en cualquier parte para buscar en tu ordenador.</div>
              <div className="dropzoneAction">
                {batchUploading ? "Subiendo…" : "Seleccionar ZIP"}
              </div>
            </label>

            <div className="panelActions">
              <button className="btn btn--outlined" onClick={() => setService(null)} disabled={batchBusy}>
                Cambiar servicio
              </button>
            </div>

            {batchNotice && <p className="notice successText">{batchNotice}</p>}

            {showBatch && batchId && (
              <div className="batchCard">
                <div className="batchHeader">
                  <div className="row">
                    <span className="chip">Batch: {batchId}</span>
                    <span className={`chip chip--status chip--${effectiveBatchStatus || "ready"}`}>
                      {effectiveBatchStatus || "ready"}
                    </span>
                    {batchElapsedLabel && <span className="chip chip--muted">Tiempo total: {batchElapsedLabel}</span>}
                  </div>
                  <div className="row">
                    {effectiveBatchStatus === "ready" && (
                      <button className="btn btn--filled" onClick={startBatch}>
                        Iniciar tipificación
                      </button>
                    )}
                    {(effectiveBatchStatus === "processing" || effectiveBatchStatus === "cancelling") && (
                      <button className="btn btn--outlined" onClick={cancelBatch}>
                        {effectiveBatchStatus === "cancelling" ? "Deteniendo…" : "Detener"}
                      </button>
                    )}
                    {(effectiveBatchStatus === "done" || effectiveBatchStatus === "partial") && (
                      <a className="btn btn--filled" href={`${API_BASE}/batch/${batchId}/download/all.zip`}>
                        Descargar todo
                      </a>
                    )}
                    {batchError > 0 && (
                      <button
                        className="btn btn--tonal"
                        onClick={retryBatchErrors}
                        disabled={batchRetrying}
                      >
                        {batchRetrying ? "Reintentando…" : "Reintentar errores"}
                      </button>
                    )}
                  </div>
                </div>
                <div className="batchProgress">
                  <div className="progressBar" aria-label="progreso lote">
                    <div className="progressFill" style={{ width: `${batchProgress}%` }} />
                  </div>
                  <div className="row statsRow">
                    <span className="chip">Completados: {batchDone}</span>
                    <span className="chip">Errores: {batchError}</span>
                    <span className="chip">Total: {batchTotal}</span>
                  </div>
                </div>
                <div className="batchList">
                  {batchPackages.map((p) => {
                    const packageElapsedLabel = getPackageElapsedLabel(p);
                    const stageLabel = formatStage(p.currentStage || p.audit?.stage);
                    return (
                      <div key={p.name} className="batchItem">
                        <div>
                          <div className="batchName">{p.name}</div>
                          {packageElapsedLabel && <div className="small">Tiempo: {packageElapsedLabel}</div>}
                          {p.status === "processing" && stageLabel && <div className="small">Etapa: {stageLabel}</div>}
                        </div>
                        <span className={`chip chip--status chip--${p.status}`}>{p.status}</span>
                        {p.status === "done" && (
                          <a
                            className="btn btn--tonal btn--sm"
                            href={`${API_BASE}/batch/${batchId}/download/${p.name}.zip`}
                          >
                            Descargar
                          </a>
                        )}
                        {p.status === "error" && (
                          <span className="small errorText">{formatBatchError(p.error)}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </section>
        )}

        {showWork && (
          <>
            <section className="card statsCard">
              <div className="progressBar" aria-label="progreso">
                <div className="progressFill" style={{ width: `${progress}%` }} />
              </div>
              <div className="statsGroup statsGroup--center">
                <div className="row statsRow">
                  <span className="chip">Tipificadas: {typedCount}</span>
                  <span className="chip">Sin tipificar: {counts.SIN}</span>
                  <span className="chip">Seleccionadas: {selected.size}</span>
                </div>

                <div className="row statsRow">
                  {CATEGORIES.map((c) => (
                    <span key={c} className={`chip chip--cat chip--${c.toLowerCase()}`}>
                      {c}: {counts[c]}
                    </span>
                  ))}
                </div>
              </div>
            </section>

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
          </>
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
