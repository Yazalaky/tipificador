# Agent Notes

## Project Shape
- Two apps share one repo: `backend/` is the FastAPI/PyMuPDF/OCR API; `frontend/` is a React + Vite UI that drives both single-job and batch flows.
- Business rules live in `CONTEXT.md` and `TEST_CASES.md`; read them before changing classification, output naming, OCR, or ZIP generation logic.
- Backend state defaults to `/tmp/tipificador_jobs`; batch metadata/results may also mirror to GCS when `TIPIFICADOR_GCS_BUCKET` is set.

## Local Setup And Run
- First-time setup from repo root: `bash scripts/bootstrap_dev.sh`.
- Backend local run: `cd backend`, copy/load `backend/.env.example` as `.env.local`, activate `.venv`, then `uvicorn app.main:app --reload --port 8000`.
- Frontend local run: `cd frontend`, copy `frontend/.env.example` as `.env.local`, then `npm run dev`; `VITE_API_BASE` must point at the backend, usually `http://127.0.0.1:8000`.
- OCR requires system `tesseract` plus Spanish language data; the Dockerfile installs `tesseract-ocr` and `tesseract-ocr-spa`.

## Verification
- Frontend checks available from `frontend/`: `npm run lint` and `npm run build` (`build` runs `tsc -b` before Vite).
- There are no committed automated backend tests or pytest config; use targeted API/manual validation plus `TEST_CASES.md` for functional changes.
- For OCR/classification debugging against a running backend and existing job: `python tools/ocr_debug.py <job_id> --api http://localhost:8000`.
- Firebase Hosting deploys must use Node.js 20 LTS (`nvm use 20`); Node 22 has failed OAuth token fetches in `firebase-tools`.

## Runtime And API Gotchas
- API entrypoint is `backend/app/main.py`; categories are exactly `CRC`, `FEV`, `HEV`, `OPF`, `PDE`.
- The frontend default mode is batch and consumes `VITE_API_BASE` at build/runtime via Vite; deploying with localhost is blocked by `tools/deploy_frontend.sh`.
- Individual flow uses `/jobs`, page image/OCR endpoints, `/jobs/{job_id}/auto-classify`, and `/jobs/{job_id}/process`.
- Batch flow uses `/batch`, `/batch/upload-url`, `/batch/from-gcs`, `/batch/{id}/start`, `/cancel`, `/retry-errors`, and download endpoints.
- Final PDF naming is `{PREFIJO}_{NIT_BASE}_{OCFE}.pdf`; NIT and OCFE are extracted from FEV pages.

## Deploy And Ops
- Backend deploy: `bash tools/deploy_backend.sh`; defaults to project `tipificador-cloud-prod`, region `us-central1`, service `tipificador-api`.
- Frontend deploy: set non-local `VITE_API_BASE`, then `bash tools/deploy_frontend.sh`; Firebase hosting public dir is `frontend/dist`.
- Full deploy: `bash tools/deploy_all.sh`; it deploys backend, reads the Cloud Run URL, health-checks `/health`, then deploys frontend with that URL.
- Maintenance mode scripts are `bash tools/maintenance_on.sh` and `bash tools/maintenance_off.sh`.
