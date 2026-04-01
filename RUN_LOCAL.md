# Run Local

## Prerrequisitos

- Linux/macOS con `bash`
- Python 3.11+ (o 3.12)
- Node.js 20+
- Tesseract OCR instalado en el sistema (`tesseract`, idioma `spa`)

## 1) Preparar entorno (primer uso)

Desde la raiz del repo:

```bash
bash scripts/bootstrap_dev.sh
```

## 2) Levantar backend

```bash
cd backend
cp .env.example .env.local
set -a
source .env.local
set +a
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

API local: `http://127.0.0.1:8000`

## 3) Levantar frontend

En otra terminal:

```bash
cd frontend
cp .env.example .env.local
npm run dev
```

Frontend local: `http://127.0.0.1:5173`

Variable esperada en `frontend/.env.local`:

```env
VITE_API_BASE=http://127.0.0.1:8000
```

## 4) Flujos disponibles en local

- Flujo individual: crear `job`, clasificar paginas y ejecutar `POST /jobs/{id}/process`.
- Flujo por lotes: cargar paquetes y procesarlos mediante endpoints `/batch`.
- Auto-clasificacion: disponible desde `POST /jobs/{id}/auto-classify`.

## 5) Problemas comunes

- `ModuleNotFoundError: No module named 'google'`:
  - Ejecutar `pip install -r backend/requirements.txt` dentro de `.venv`.
- `No such file or directory: tesseract`:
  - Instalar tesseract e idioma espanol en el sistema.
- CORS en local:
  - Validar que frontend apunte al backend local (`VITE_API_BASE=http://127.0.0.1:8000`).
