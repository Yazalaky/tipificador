# Guía de Migración a Windows 11 (Tipificador Cloud)

Esta guía deja una referencia estándar para migrar el proyecto a Windows 11 sin perder productividad en desarrollo local ni despliegues a la nube.

## 0. Plantilla recomendada para cualquier proyecto

Usa esta estructura mínima como base:

```md
# Nombre del proyecto

## 1. Descripción
Breve explicación de qué hace el proyecto.

## 2. Requisitos del entorno
- Node.js vXX
- Python vX.X (si aplica)
- Firebase CLI
- Docker (si aplica)

## 3. Variables de entorno
Archivo .env requerido:
- VAR_1=
- VAR_2=

## 4. Instalación
```bash
npm install
# o
pip install -r requirements.txt
```

## 5. Ejecución
```bash
npm run dev
# o
python app.py
```

## 6. Build / Deploy
```bash
npm run build
firebase deploy
```

## 7. Notas importantes
- Rutas usadas
- Puertos
- Advertencias
```

## 1. Descripción

**Tipificador Cloud** permite tipificar soportes PDF de facturación domiciliaria por categorías (`CRC`, `FEV`, `HEV`, `OPF`, `PDE`) y generar ZIPs por paquete/factura.

Stack:
- Backend: FastAPI + OCR (Tesseract) en `backend/`
- Frontend: React + Vite en `frontend/`
- Operación/Deploy: scripts en `tools/`

## 2. Requisitos del entorno (Windows 11)

Recomendación: trabajar con **WSL2 + Ubuntu**, no con shell Windows nativo.

### 2.1 Windows
- Windows 11 (ideal LTSC actualizado)
- Virtualización habilitada en BIOS
- WSL2 habilitado

Instalar WSL2 (PowerShell como administrador):

```powershell
wsl --install -d Ubuntu
```

### 2.2 Dentro de WSL (Ubuntu)

```bash
sudo apt update
sudo apt install -y \
  git curl unzip ca-certificates gnupg \
  python3 python3-venv python3-pip \
  tesseract-ocr tesseract-ocr-spa
```

Node.js 20+ (ejemplo con NodeSource):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v
npm -v
```

Firebase CLI:

```bash
npm install -g firebase-tools
firebase --version
```

Google Cloud CLI:

```bash
cd ~
curl -LO https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-556.0.0-linux-x86_64.tar.gz
tar -xzf google-cloud-cli-556.0.0-linux-x86_64.tar.gz
./google-cloud-sdk/install.sh --quiet
echo 'source ~/google-cloud-sdk/path.bash.inc' >> ~/.bashrc
source ~/.bashrc
gcloud --version
```

## 3. Variables de entorno

Referencia base: `.env.example`

Variables más importantes:
- `VITE_API_BASE=http://127.0.0.1:8000` (frontend local)
- `TIPIFICADOR_OCR_ENABLED=1`
- `TIPIFICADOR_OCR_LANG=spa+eng` (o `spa`)
- `TIPIFICADOR_OCR_WORKERS=1` (ajustable según CPU)

Nota: para producción, las variables del backend se gestionan en Cloud Run.

## 4. Instalación del proyecto

Clona en **filesystem Linux de WSL** (ejemplo: `~/tipificador`), no en `/mnt/c/...`.

```bash
cd ~
git clone <URL_DEL_REPO> tipificador
cd tipificador
git config --global core.autocrlf input
bash scripts/bootstrap_dev.sh
```

## 5. Ejecución local

Backend:

```bash
cd ~/tipificador/backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Frontend (otra terminal):

```bash
cd ~/tipificador/frontend
VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

URLs:
- Frontend: `http://127.0.0.1:5173`
- Backend health: `http://127.0.0.1:8000/health`

## 6. Build / Deploy (nube)

Autenticación (primera vez):

```bash
firebase login
gcloud auth login
gcloud auth application-default login
gcloud config set project tipificador-cloud-prod
```

Deploy backend:

```bash
cd ~/tipificador
bash tools/deploy_backend.sh
```

Deploy frontend:

```bash
cd ~/tipificador
BACKEND_URL="$(gcloud run services describe tipificador-api --region us-central1 --project tipificador-cloud-prod --format='value(status.url)')"
VITE_API_BASE="$BACKEND_URL" FIREBASE_PROJECT=tipificador-cloud bash tools/deploy_frontend.sh
```

Deploy completo:

```bash
cd ~/tipificador
bash tools/deploy_all.sh
```

## 7. Notas importantes

- Rutas:
  - Backend: `backend/`
  - Frontend: `frontend/`
  - Deploy/ops: `tools/`
- Puertos locales:
  - `8000` backend
  - `5173` frontend
- No versionar secretos ni `.env` privados.
- Si un `.sh` falla por formato de línea, convertir a LF:

```bash
sed -i 's/\r$//' tools/*.sh scripts/*.sh
```

- Si no funciona OCR:
  - Validar `tesseract --version`
  - Verificar idioma `spa` instalado.

## 8. Checklist rápido de migración

1. WSL2 instalado y operativo.
2. Dependencias instaladas (Python, Node, Tesseract, gcloud, firebase).
3. Repo clonado en `~` (Linux).
4. `bash scripts/bootstrap_dev.sh` ejecutado sin error.
5. Backend y frontend levantan localmente.
6. `firebase login` y `gcloud auth login` completados.
7. Deploy de prueba ejecutado correctamente.
