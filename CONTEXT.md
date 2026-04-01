# Tipificador Cloud Reminder (Context)

## Objetivo
Aplicación web para tipificar páginas de uno o varios PDFs pertenecientes a una factura.
El usuario clasifica páginas en: CRC, FEV, HEV, OPF, PDE. Luego se genera un ZIP con PDFs por categoría.

## Flujos actuales
- Flujo individual: crea un `job`, clasifica páginas y genera un ZIP final.
- Flujo por lotes: crea un `batch`, procesa multiples paquetes y permite descarga por paquete o consolidada.
- El frontend consume ambos flujos desde una sola aplicación React.

## Naming final
{PREFIJO}_{NIT_BASE}_{OCFE}.pdf
- NIT_BASE se extrae de FEV (NIT: 900204617-5 -> 900204617)
- OCFE se extrae de FEV (OCFE5871)

## Backend
- FastAPI (backend/app/main.py)
- Endpoints principales:
  - POST /jobs (subida de múltiples PDFs)
  - GET /jobs/{id}/pages/{page}/thumb.png
  - GET /jobs/{id}/pages/{page}/view.png
  - POST /jobs/{id}/process (genera ZIP)
- Endpoints de apoyo:
  - GET /jobs/{id}/pages/{page}/ocr.txt
  - POST /jobs/{id}/auto-classify
  - POST /batch
  - POST /batch/upload-url
  - POST /batch/from-gcs
  - GET /batch/{id}
  - POST /batch/{id}/start
  - POST /batch/{id}/cancel
  - POST /batch/{id}/retry-errors
  - GET /batch/{id}/download/all.zip
  - GET /batch/{id}/download/{package_name}.zip
- Procesamiento PDF con PyMuPDF

## Frontend
- React + Vite (frontend/src/App.tsx)
- Permite seleccionar páginas y asignar categorías
- Orquesta flujos individual y por lotes desde la misma UI
- Llama API definida por VITE_API_BASE
