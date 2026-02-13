# Operations

## Modo mantenimiento

Encender:

```bash
bash tools/maintenance_on.sh
```

Apagar:

```bash
bash tools/maintenance_off.sh
```

Nota: los scripts despliegan hosting y bloquean/habilitan invocacion publica de Cloud Run.

## Limpieza automatica de resultados (GCS)

Endpoint backend: `POST /admin/cleanup`

Variables usadas:

- `TIPIFICADOR_CLEANUP_TOKEN`
- `TIPIFICADOR_CLEANUP_AGE_MINUTES` (actual: 30)

Cloud Scheduler (actual):

- Job: `tipificador-cleanup`
- Location: `us-central1`
- Frecuencia: cada 15 min

Ver jobs:

```bash
gcloud scheduler jobs list --location us-central1
```

Ejecutar manual:

```bash
gcloud scheduler jobs run tipificador-cleanup --location us-central1
```

## Revisar logs backend

```bash
gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name="tipificador-api"' \
  --limit 50 \
  --format "value(textPayload)"
```

Tiempo real:

```bash
gcloud logging tail \
  'resource.type=cloud_run_revision AND resource.labels.service_name="tipificador-api"'
```

## Revisar costos

En consola GCP:

1. Facturacion -> Informes
2. Agrupar por Servicio
3. Revisar neto por Cloud Run / Cloud Storage / Cloud Build

## Buckets actuales

- `gs://tipificador-zips-prod/` (ZIPs app)
- `gs://tipificador-cloud-prod_cloudbuild/` (Cloud Build)

Inspeccion rapida:

```bash
gsutil ls -L -b gs://tipificador-zips-prod | rg -n "Location|Storage class|Lifecycle"
gsutil versioning get gs://tipificador-zips-prod
```

