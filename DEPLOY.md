# Deploy

## Variables base esperadas

- Proyecto backend: `tipificador-cloud-prod`
- Servicio Cloud Run: `tipificador-api`
- Region: `us-central1`
- Proyecto Firebase Hosting: `tipificador-cloud`

## Deploy backend

Desde la raiz:

```bash
bash tools/deploy_backend.sh
```

El script:

1. Build de imagen en Artifact Registry.
2. Deploy de imagen a Cloud Run.

## Deploy frontend

Desde la raiz:

```bash
bash tools/deploy_frontend.sh
```

El script:

1. Build de Vite.
2. Deploy de Hosting en Firebase.

## Deploy full (backend + frontend)

```bash
bash tools/deploy_all.sh
```

## Sobrescribir valores en un deploy puntual

```bash
PROJECT_ID=tipificador-cloud-prod \
REGION=us-central1 \
SERVICE=tipificador-api \
REPO=tipificador \
IMAGE_TAG=prod \
bash tools/deploy_backend.sh
```

```bash
FIREBASE_PROJECT=tipificador-cloud \
bash tools/deploy_frontend.sh
```

