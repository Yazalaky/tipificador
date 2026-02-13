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
cd /home/sistemas/Proyectos/tipificador-cloud/backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

API local: `http://127.0.0.1:8000`

## 3) Levantar frontend

En otra terminal:

```bash
cd /home/sistemas/Proyectos/tipificador-cloud/frontend
npm run dev
```

Frontend local: `http://127.0.0.1:5173`

## 4) Problemas comunes

- `ModuleNotFoundError: No module named 'google'`:
  - Ejecutar `pip install -r backend/requirements.txt` dentro de `.venv`.
- `No such file or directory: tesseract`:
  - Instalar tesseract e idioma espanol en el sistema.
- CORS en local:
  - Validar que frontend apunte al backend local (`VITE_API_BASE=http://localhost:8000`).

